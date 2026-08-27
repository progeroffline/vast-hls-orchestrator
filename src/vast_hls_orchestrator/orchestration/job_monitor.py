"""Polls the remote job over SSH and drives the live Rich dashboard until done."""

from __future__ import annotations

import argparse
import subprocess
import time

from loguru import logger
from rich.live import Live

from ..core.console import console
from ..core.constants import BAD_STATES
from ..core.errors import VastError
from ..core.models import DashboardContext, RemoteSnapshot
from ..remote.snapshot import fetch_remote_snapshot
from ..remote.ssh import ssh_run
from ..ui.dashboard import render_dashboard
from ..vast_api.client import VastClient


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
) -> tuple[str, int]:
    deadline = time.time() + args.job_timeout
    ctx = DashboardContext(
        instance_id=instance_id,
        gpu_name=gpu_name,
        hourly_price=hourly_price,
        host=host,
        port=port,
        expected_input_bytes=expected_input_bytes,
        started_at=time.time(),
    )
    last_stage: str | None = None
    last_status: str | None = None
    last_log_tail = ""
    ssh_failure_started: float | None = None

    placeholder = RemoteSnapshot(stage="connecting")
    with Live(
        render_dashboard(ctx, placeholder),
        console=console,
        refresh_per_second=4,
        transient=False,
        vertical_overflow="visible",
    ) as live:
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
                ctx.host, ctx.port = host, port

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

            live.update(render_dashboard(ctx, snap), refresh=True)

            if snap.stage != last_stage:
                logger.info("Remote stage -> {}", snap.stage)
                last_stage = snap.stage
            if snap.status != last_status:
                logger.info("Remote status -> {}", snap.status)
                last_status = snap.status

            # Print meaningful remote log changes above the dashboard, while the dashboard
            # itself always shows the latest tail.
            if snap.remote_log_tail and snap.remote_log_tail != last_log_tail:
                new_lines = [
                    line for line in snap.remote_log_tail.splitlines() if line.strip()
                ]
                if new_lines:
                    logger.debug("Remote log: {}", " | ".join(new_lines[-3:]))
                last_log_tail = snap.remote_log_tail

            if snap.status.startswith("DONE:"):
                rc_text = snap.status.split(":", 1)[1].strip()
                rc = int(rc_text or "1")
                if rc != 0:
                    logs = ssh_run(
                        args,
                        host,
                        port,
                        "echo '=== job.log ==='; tail -n 200 /workspace/job.log || true; "
                        'for q in 1080p 720p 480p 360p; do echo "=== $q ==="; tail -n 100 /workspace/out/$q/ffmpeg.log 2>/dev/null || true; done',
                        timeout=40,
                    )
                    raise VastError(
                        f"Encoder job failed with exit code {rc}\n{logs.stdout}"
                    )
                live.update(render_dashboard(ctx, snap), refresh=True)
                logger.success("Encoder job completed successfully")
                return host, port

            time.sleep(args.monitor_interval)

    raise VastError("Encoding job timeout exceeded")
