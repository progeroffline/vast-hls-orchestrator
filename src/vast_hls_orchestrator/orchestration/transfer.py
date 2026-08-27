"""Streams a subprocess's output into the TUI app's body panel (used for rsync)."""

from __future__ import annotations

import re
import subprocess

from loguru import logger
from rich.panel import Panel
from rich.text import Text

from ..core.errors import VastError
from ..ui.app import TuiApp


def stream_process(cmd: list[str], *, label: str, app: TuiApp) -> None:
    logger.info("{}", label)
    app.set_body(Panel("Waiting for rsync...", title="Transfer", border_style="cyan"))
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
                    app.set_body(
                        Panel(
                            Text(line, overflow="ellipsis"),
                            title="rsync live progress",
                            border_style="cyan",
                        )
                    )
                    last_line = line
        if pending.strip():
            last_line = pending.decode("utf-8", "replace").strip()
            app.set_body(Panel(Text(last_line), title="rsync result", border_style="cyan"))
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
