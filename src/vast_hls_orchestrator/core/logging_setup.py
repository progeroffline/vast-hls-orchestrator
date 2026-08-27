"""Loguru configuration that renders log records through the shared Rich console."""

from __future__ import annotations

from typing import Any

from loguru import logger
from rich.markup import escape

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
        console.print(
            f"[dim]{timestamp}[/] [{style}]{level}[/] {text}",
            highlight=False,
        )
        if record.get("exception"):
            console.print(str(record["exception"]), style="red")


def configure_logging(verbose: bool = False) -> None:
    logger.remove()
    logger.add(RichLogSink(), level="DEBUG" if verbose else "INFO", enqueue=False)
