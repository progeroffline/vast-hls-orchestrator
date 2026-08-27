"""Registry for the currently active full-screen TUI app, if any.

Lets `core.logging_setup` route log lines into the TUI's log panel instead of
printing directly to the console, without `core` importing the `ui` package.
"""

from __future__ import annotations

from typing import Protocol


class LogSink(Protocol):
    def append_log(self, line: str) -> None: ...


_active: LogSink | None = None


def set_active(app: LogSink | None) -> None:
    global _active
    _active = app


def active() -> LogSink | None:
    return _active
