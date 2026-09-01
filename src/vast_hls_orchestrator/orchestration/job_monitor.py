"""Polls the remote job over SSH and logs periodic progress lines."""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

from loguru import logger

from ..core.constants import BAD_STATES
from ..core.errors import VastError
from ..core.models import JobContext, RemoteSnapshot, SshEndpoint
from ..orchestration.publish import staging_bytes_on_disk
from ..remote.snapshot import fetch_remote_snapshot
from ..remote.ssh import ssh_run
from ..ui.formatting import format_bytes, format_cost, format_duration
from ..vast_api.client import VastClient
from .provisioning import ssh_endpoint_candidates

# Print a progress summary at most this often, regardless of --monitor-interval,
# so a fast poll cadence doesn't turn into log spam.
PROGRESS_LOG_INTERVAL_S = 10.0


def _append_upload_progress(
    parts: list[str], ctx: JobContext, snap: RemoteSnapshot, upload_state: dict
) -> None:
    # Bytes-transferred-so-far is *not* polled from the remote job -- origin
    # already owns the staging directory Vast is pushing into, so it's
    # cheaper and just as accurate to inspect its real on-disk usage
    # directly (same "actual disk blocks" reasoning as the download-progress
    # fix). Only the total (what the push is aiming for) and worker-done
    # count come from the remote poll, since origin has no way to know
    # those on its own.
    uploaded = staging_bytes_on_disk(ctx.upload_staging_dir) if ctx.upload_staging_dir else 0
    total = snap.upload_total_bytes

    now = time.time()
    prev_bytes = upload_state.get("bytes")
    prev_at = upload_state.get("at")
    speed_bps = 0.0
    if prev_bytes is not None and prev_at is not None and now > prev_at:
        speed_bps = max(0.0, (uploaded - prev_bytes) / (now - prev_at))
    upload_state["bytes"] = uploaded
    upload_state["at"] = now

    if total:
        pct = min(100.0, uploaded * 100.0 / total)
        parts.append(f"uploaded={pct:4.1f}% ({format_bytes(uploaded)}/{format_bytes(total)})")
    else:
        parts.append(f"uploaded={format_bytes(uploaded)}")
    if speed_bps > 0:
        parts.append(f"speed={speed_bps / (1024 * 1024):.1f} MiB/s")
        if total and total > uploaded:
            eta_seconds = (total - uploaded) / speed_bps
            parts.append(f"eta={format_duration(eta_seconds)}")
    parts.append(f"workers={snap.upload_workers_done}/{ctx.upload_workers_total}")


def _log_progress(
    ctx: JobContext, snap: RemoteSnapshot, upload_state: dict | None = None
) -> None:
    elapsed = max(0.0, time.time() - ctx.started_at)
    parts = [f"stage={snap.stage}"]

    if snap.stage == "uploading":
        _append_upload_progress(parts, ctx, snap, upload_state if upload_state is not None else {})
    else:
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
    upload_staging_dir: Path | None = None,
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
        upload_staging_dir=upload_staging_dir,
        upload_workers_total=args.upload_workers,
    )
    last_stage: str | None = None
    last_status: str | None = None
    last_log_tail = ""
    last_progress_logged_at = 0.0
    ssh_failure_started: float | None = None
    upload_speed_state: dict = {}

    while time.time() < deadline:
        info = client.show_instance(instance_id)
        if info is None:
            raise VastError("Vast instance disappeared before result transfer")
        state = info.get("actual_status")
        if state in BAD_STATES:
            raise VastError(f"Instance entered bad state during encoding: {state}")

        endpoints = ssh_endpoint_candidates(info)
        # Keep a healthy current route first, but retain the other route as an
        # immediate fallback. Crucially, never synthesize public_ipaddr:ssh_port.
        if not any(
            (endpoint.host, endpoint.port) == (host, port) for endpoint in endpoints
        ):
            endpoints.insert(0, SshEndpoint("current", host, port))
        endpoints.sort(
            key=lambda endpoint: (endpoint.host, endpoint.port) != (host, port)
        )
        snapshot_error: Exception | None = None
        snap: RemoteSnapshot | None = None
        for endpoint in endpoints:
            try:
                snap = fetch_remote_snapshot(args, endpoint.host, endpoint.port)
            except (subprocess.TimeoutExpired, VastError, OSError) as exc:
                snapshot_error = exc
                logger.warning(
                    "SSH monitoring via {} endpoint {}:{} failed; trying next route",
                    endpoint.kind,
                    endpoint.host,
                    endpoint.port,
                )
                continue
            if (endpoint.host, endpoint.port) != (host, port):
                logger.warning(
                    "Switching SSH monitoring endpoint from {}:{} to {} {}:{}",
                    host,
                    port,
                    endpoint.kind,
                    endpoint.host,
                    endpoint.port,
                )
                host, port = endpoint.host, endpoint.port
            break

        if snap is None:
            now = time.monotonic()
            if ssh_failure_started is None:
                ssh_failure_started = now
            disconnected_for = now - ssh_failure_started
            logger.warning(
                "Remote monitoring unavailable for {:.0f}s: {}",
                disconnected_for,
                snapshot_error or "Vast returned no SSH endpoint",
            )
            if disconnected_for >= args.ssh_reconnect_timeout:
                raise VastError(
                    f"SSH did not recover within {args.ssh_reconnect_timeout}s"
                ) from snapshot_error
            time.sleep(args.monitor_interval)
            continue
        if ssh_failure_started is not None:
            logger.success("SSH monitoring connection recovered")
        ssh_failure_started = None

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
            _log_progress(ctx, snap, upload_speed_state)
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
            # Not the final word: this is the remote script (encode + direct
            # push to origin) exiting 0, not the pipeline being done -- origin
            # still has to verify and atomically publish the staging
            # directory locally. The real end-to-end "SUCCESS" is logged by
            # pipeline.run() only after that step also succeeds.
            logger.info("Remote job finished (encode + upload); verifying and publishing locally...")
            return host, port

        time.sleep(args.monitor_interval)

    raise VastError("Encoding job timeout exceeded")
