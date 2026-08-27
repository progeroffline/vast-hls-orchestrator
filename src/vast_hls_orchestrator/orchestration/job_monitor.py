"""Polls the remote job over SSH and pushes dashboard frames into the TUI app."""

from __future__ import annotations

import argparse
import subprocess
import time

from loguru import logger

from ..core.constants import BAD_STATES
from ..core.errors import VastError
from ..core.models import DashboardContext, RemoteSnapshot
from ..remote.snapshot import fetch_remote_snapshot
from ..remote.ssh import ssh_run
from ..ui.app import TuiApp
from ..ui.dashboard import render_dashboard
from ..vast_api.client import VastClient


def wait_for_job(
    args: argparse.Namespace,
    client: VastClient,
    instance_id: int,
    host: str,
    port: int,
    *,
    app: TuiApp,
    gpu_name: str,
    hourly_price: float,
    expected_input_bytes: int | None,
    rental_started_at: float,
    using_proxy: bool = False,
) -> tuple[str, int]:
    deadline = time.time() + args.job_timeout
    ctx = DashboardContext(
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
    ssh_failure_started: float | None = None

    app.set_body(render_dashboard(ctx, RemoteSnapshot(stage="connecting")))
    while time.time() < deadline:
        info = client.show_instance(instance_id)
        if info is None:
            raise VastError("Vast instance disappeared before result transfer")
        state = info.get("actual_status")
        if state in BAD_STATES:
            raise VastError(f"Instance entered bad state during encoding: {state}")

        # Reconcile against whichever endpoint kind we're actually using --
        # comparing a proxy connection against the direct fields (or vice
        # versa) would look like Vast "changed" it and bounce us back to an
        # endpoint that may not even be reachable.
        host_key, port_key = ("ssh_proxy_addr", "ssh_proxy_port") if using_proxy else ("ssh_host", "ssh_port")
        new_host = str(info.get(host_key) or host)
        try:
            new_port = int(info.get(port_key) or port)
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

        app.set_body(render_dashboard(ctx, snap))

        if snap.stage != last_stage:
            logger.info("Remote stage -> {}", snap.stage)
            last_stage = snap.stage
        if snap.status != last_status:
            logger.info("Remote status -> {}", snap.status)
            last_status = snap.status

        # Send meaningful remote log changes through the shared log panel, while
        # the dashboard itself always shows the latest remote log tail too.
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
            app.set_body(render_dashboard(ctx, snap))
            logger.success("Encoder job completed successfully")
            return host, port

        time.sleep(args.monitor_interval)

    raise VastError("Encoding job timeout exceeded")
