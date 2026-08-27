"""Polls Vast.ai's own container log endpoint and forwards new lines into the TUI.

This works even before SSH access is available -- no SSH needed at all -- which
is exactly the window (instance created, still booting, SSH not up yet) where
our own SSH-based remote log tail (remote/snapshot.py) cannot be used. Useful
to see why sshd/key injection failed on the rented machine itself.
"""

from __future__ import annotations

import time

from rich.markup import escape

from ..ui.app import TuiApp
from ..vast_api.client import VastClient


class RemoteLogTailer:
    def __init__(
        self,
        client: VastClient,
        instance_id: int,
        app: TuiApp,
        *,
        tail: str = "1000",
        min_interval: float = 5.0,
    ):
        self._client = client
        self._instance_id = instance_id
        self._app = app
        self._tail = tail
        self._min_interval = min_interval
        self._last_poll_at = 0.0
        self._last_line: str | None = None

    def poll(self) -> None:
        """Best-effort tick; safe to call from a tight loop, self-throttles."""
        now = time.monotonic()
        if now - self._last_poll_at < self._min_interval:
            return
        self._last_poll_at = now

        text = self._client.request_logs(self._instance_id, tail=self._tail)
        if not text:
            return
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return
        if self._last_line is not None and self._last_line in lines:
            new_lines = lines[lines.index(self._last_line) + 1 :]
        else:
            new_lines = lines
        for line in new_lines:
            self._app.append_log(f"[dim]\\[vast][/] {escape(line)}")
        self._last_line = lines[-1]
