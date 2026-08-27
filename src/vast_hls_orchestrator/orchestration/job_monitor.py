"""Polls the remote job over SSH and logs periodic progress lines."""

from __future__ import annotations

import argparse
import subprocess
import time

from loguru import logger

from ..core.constants import BAD_STATES
from ..core.errors import VastError
from ..core.models import JobContext, RemoteSnapshot
from ..remote.snapshot import fetch_remote_snapshot
from ..remote.ssh import ssh_run
from ..ui.formatting import format_bytes, format_cost, format_duration
from ..vast_api.client import VastClient

# Print a progress summary at most this often, regardless of --monitor-interval,
# so a fast poll cadence doesn't turn into log spam.
PROGRESS_LOG_INTERVAL_S = 10.0


def _log_progress(ctx: JobContext, snap: RemoteSnapshot) -> None:
    elapsed = max(0.0, time.time() - ctx.started_at)
    parts = [f"stage={snap.stage}"]
    if ctx.expected_input_bytes:
        # downloaded_bytes is real disk blocks allocated (stat %b -- see
        # remote/snapshot.py), expected_input_bytes is the source URL's
        # logical Content-Length: different units that can legitimately
        # diverge by a few percent (filesystem block-alignment overhead
        # across aria2's 16 parallel segments), so this can exceed 100%
        # without anything actually being wrong. Clamp only the displayed
        # percentage; the raw byte counts alongside it stay honest.
        pct = min(100.0, snap.downloaded_bytes * 100.0 / ctx.expected_input_bytes)
        parts.append(
            f"download={pct:4.1f}% ({format_bytes(snap.downloaded_bytes)}/{format_bytes(ctx.expected_input_bytes)})"
        )
    if snap.duration_seconds > 0:
        parts.append(
            f"media={format_duration(snap.encode.out_time_seconds)}/{format_duration(snap.duration_seconds)}"
        )
    if snap.encode.fps:
        parts.append(f"fps={snap.encode.fps:.1f}")
    if snap.encode.speed:
        parts.append(f"speed={snap.encode.speed:.2f}x")
    if snap.duration_seconds > 0 and snap.encode.speed > 0:
        # All four renditions share one decode/encode pass, so one ETA
        # covers the whole ABR ladder -- they finish together.
        remaining_media = snap.duration_seconds - snap.encode.out_time_seconds
        eta_seconds = max(0.0, remaining_media) / snap.encode.speed
        parts.append(f"eta={format_duration(eta_seconds)}")
    parts.append(f"cost={format_cost(ctx.hourly_price, elapsed)}")
    logger.info("Progress: {}", "  ".join(parts))


def wait_for_job(
    args: argparse.Namespace,
    client: VastClient,
    instance_id: int,
    host: str,
    port: int,
    *,
    gpu_name: str,
    hourly_price: float,
    expected_input_bytes: int | None,
    rental_started_at: float,
) -> tuple[str, int]:
    deadline = time.time() + args.job_timeout
    ctx = JobContext(
        instance_id=instance_id,
        gpu_name=gpu_name,
        hourly_price=hourly_price,
        host=host,
        port=port,
        expected_input_bytes=expected_input_bytes,
        # From when Vast actually started billing (instance creation), not
        # from when this monitoring loop happens to start -- provisioning,
        # bootstrap and download all run (and cost money) before this point.
        started_at=rental_started_at,
    )
    last_stage: str | None = None
    last_status: str | None = None
    last_log_tail = ""
    last_progress_logged_at = 0.0
    ssh_failure_started: float | None = None

    while time.time() < deadline:
        info = client.show_instance(instance_id)
        if info is None:
            raise VastError("Vast instance disappeared before result transfer")
        state = info.get("actual_status")
        if state in BAD_STATES:
            raise VastError(f"Instance entered bad state during encoding: {state}")

        new_host = str(info.get("ssh_host") or host)
        try:
            new_port = int(info.get("ssh_port") or port)
        except (TypeError, ValueError):
            new_port = port
        if (new_host, new_port) != (host, port):
            logger.warning(
                "Vast changed SSH endpoint from {}:{} to {}:{}",
                host,
                port,
                new_host,
                new_port,
            )
            host, port = new_host, new_port

        try:
            snap = fetch_remote_snapshot(args, host, port)
            if ssh_failure_started is not None:
                logger.success("SSH monitoring connection recovered")
            ssh_failure_started = None
        except (subprocess.TimeoutExpired, VastError, OSError) as exc:
            now = time.monotonic()
            if ssh_failure_started is None:
                ssh_failure_started = now
            disconnected_for = now - ssh_failure_started
            logger.warning(
                "Remote monitoring unavailable for {:.0f}s: {}",
                disconnected_for,
                exc,
            )
            if disconnected_for >= args.ssh_reconnect_timeout:
                raise VastError(
                    f"SSH did not recover within {args.ssh_reconnect_timeout}s"
                ) from exc
            time.sleep(args.monitor_interval)
            continue

        if snap.stage != last_stage:
            logger.info("Remote stage -> {}", snap.stage)
            last_stage = snap.stage
        if snap.status != last_status:
            logger.info("Remote status -> {}", snap.status)
            last_status = snap.status

        if snap.remote_log_tail and snap.remote_log_tail != last_log_tail:
            new_lines = [
                line for line in snap.remote_log_tail.splitlines() if line.strip()
            ]
            if new_lines:
                logger.debug("Remote log: {}", " | ".join(new_lines[-3:]))
            last_log_tail = snap.remote_log_tail

        now_monotonic = time.monotonic()
        if now_monotonic - last_progress_logged_at >= PROGRESS_LOG_INTERVAL_S:
            _log_progress(ctx, snap)
            last_progress_logged_at = now_monotonic

        if snap.status.startswith("DONE:"):
            rc_text = snap.status.split(":", 1)[1].strip()
            rc = int(rc_text or "1")
            if rc != 0:
                logs = ssh_run(
                    args,
                    host,
                    port,
                    "echo '=== job.log ==='; tail -n 200 /workspace/job.log || true; "
                    "echo '=== ffmpeg.log ==='; "
                    "grep -v \"Opening '.*\\.ts' for writing\" /workspace/out/ffmpeg.log 2>/dev/null | tail -n 200 || true",
                    timeout=40,
                )
                raise VastError(
                    f"Encoder job failed with exit code {rc}\n{logs.stdout}"
                )
            logger.success("Encoder job completed successfully")
            return host, port

        time.sleep(args.monitor_interval)

    raise VastError("Encoding job timeout exceeded")
