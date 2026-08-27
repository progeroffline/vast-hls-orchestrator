"""Fetches remote log tails and appends them to the TUI app's log buffer on failure."""

from __future__ import annotations

import argparse

from loguru import logger
from rich.markup import escape

from ..remote.ssh import ssh_run
from ..ui.app import TuiApp


def collect_remote_diagnostics(
    args: argparse.Namespace, host: str, port: int, app: TuiApp
) -> None:
    command = (
        "echo '=== bootstrap.log ==='; tail -n 120 /workspace/bootstrap.log 2>/dev/null || true; "
        "echo '=== job.log ==='; tail -n 200 /workspace/job.log 2>/dev/null || true; "
        "echo '=== ffmpeg.log ==='; tail -n 200 /workspace/out/ffmpeg.log 2>/dev/null || true"
    )
    try:
        result = ssh_run(args, host, port, command, timeout=25)
        output = result.stdout.strip()
        if output:
            app.append_log("[bold red]--- Remote failure diagnostics ---[/]")
            for line in output.splitlines():
                app.append_log(escape(line))
        elif result.stderr.strip():
            logger.warning("Could not retrieve remote diagnostics: {}", result.stderr.strip())
    except Exception as exc:
        logger.warning("Could not retrieve remote diagnostics: {}", exc)
