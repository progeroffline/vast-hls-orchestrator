"""Loguru configuration: renders log records to the console, or into the
active full-screen TUI app's log panel when one is running.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from rich.markup import escape

from . import tui_state
from .console import console


class RichLogSink:
    LEVEL_STYLES = {
        "TRACE": "dim",
        "DEBUG": "dim cyan",
        "INFO": "cyan",
        "SUCCESS": "bold green",
        "WARNING": "yellow",
        "ERROR": "bold red",
        "CRITICAL": "bold white on red",
    }

    def __call__(self, message: Any) -> None:
        record = message.record
        style = self.LEVEL_STYLES.get(record["level"].name, "white")
        timestamp = record["time"].strftime("%H:%M:%S")
        level = record["level"].name.ljust(7)
        text = escape(record["message"])
        line = f"[dim]{timestamp}[/] [{style}]{level}[/] {text}"
        if record.get("exception"):
            line += "\n" + escape(str(record["exception"]))

        app = tui_state.active()
        if app is not None:
            app.append_log(line)
        else:
            console.print(line, highlight=False)


def configure_logging(verbose: bool = False) -> None:
    logger.remove()
    logger.add(RichLogSink(), level="DEBUG" if verbose else "INFO", enqueue=False)
