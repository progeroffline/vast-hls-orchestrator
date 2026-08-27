"""Fetches remote log tails and prints them on failure."""

from __future__ import annotations

import argparse

from loguru import logger
from rich.panel import Panel
from rich.text import Text

from ..core.console import console
from ..remote.ssh import ssh_run


def collect_remote_diagnostics(args: argparse.Namespace, host: str, port: int) -> None:
    command = (
        "echo '=== bootstrap.log ==='; tail -n 120 /workspace/bootstrap.log 2>/dev/null || true; "
        "echo '=== job.log ==='; tail -n 200 /workspace/job.log 2>/dev/null || true; "
        "echo '=== ffmpeg.log ==='; tail -n 200 /workspace/out/ffmpeg.log 2>/dev/null || true"
    )
    try:
        result = ssh_run(args, host, port, command, timeout=25)
        output = result.stdout.strip()
        if output:
            console.print(Panel(Text(output), title="Remote failure diagnostics", border_style="red"))
        elif result.stderr.strip():
            logger.warning("Could not retrieve remote diagnostics: {}", result.stderr.strip())
    except Exception as exc:
        logger.warning("Could not retrieve remote diagnostics: {}", exc)
