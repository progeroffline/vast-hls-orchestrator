"""Streams a subprocess's output into a live Rich panel (used for rsync)."""

from __future__ import annotations

import re
import subprocess

from loguru import logger
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from ..core.console import console
from ..core.errors import VastError


def stream_process(cmd: list[str], *, label: str) -> None:
    logger.info("{}", label)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    assert proc.stdout is not None
    last_line = ""
    pending = b""
    try:
        with Live(
            Panel("Waiting for rsync...", title="Transfer", border_style="cyan"),
            console=console,
            refresh_per_second=4,
            transient=False,
        ) as live:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                pending += chunk
                parts = re.split(br"[\r\n]+", pending)
                pending = parts.pop() if parts else b""
                for raw in parts:
                    line = raw.decode("utf-8", "replace").strip()
                    if line and line != last_line:
                        live.update(
                            Panel(
                                Text(line, overflow="ellipsis"),
                                title="rsync live progress",
                                border_style="cyan",
                            ),
                            refresh=True,
                        )
                        last_line = line
            if pending.strip():
                last_line = pending.decode("utf-8", "replace").strip()
                live.update(Panel(Text(last_line), title="rsync result"), refresh=True)
        rc = proc.wait()
    except BaseException:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    if rc != 0:
        raise VastError(f"rsync failed with exit code {rc}: {last_line}")
