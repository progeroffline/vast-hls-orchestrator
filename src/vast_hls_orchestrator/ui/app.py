"""Persistent full-screen TUI shell: one alternate-screen Live for the whole run.

Every phase of the pipeline (offer search, provisioning, encoding, transfer)
swaps the body panel of this single Live instead of opening its own
Live/console.status, so the whole program renders as one continuous
full-screen application, like htop or vim. Log records are captured into a
ring buffer instead of being printed directly while this app is active (see
core.logging_setup), rendered as the bottom "Log" panel, and replayed to the
normal screen on exit -- the alternate screen buffer is discarded the moment
the process leaves it, so nothing would otherwise survive for the operator
to read afterwards.
"""

from __future__ import annotations

from collections import deque

from rich.align import Align
from rich.console import Console, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from ..core import tui_state

LOG_PANEL_LINES = 10


class TuiApp:
    def __init__(self, console: Console, title: str):
        self.console = console
        self.title = title
        self.log_buffer: deque[str] = deque(maxlen=5000)
        self._active = False
        self.layout = Layout()
        self.layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="log", size=LOG_PANEL_LINES + 2),
        )
        self.layout["header"].update(self._header_panel())
        self.layout["body"].update(Text("Starting...", style="dim"))
        self._render_log_panel()
        self.live = Live(self.layout, console=console, screen=True, refresh_per_second=4)

    def _header_panel(self, subtitle: str = "") -> Panel:
        text = Text(self.title, style="bold cyan")
        if subtitle:
            text.append("  " + subtitle, style="dim")
        return Panel(Align.center(text), border_style="cyan")

    def set_header_subtitle(self, subtitle: str) -> None:
        self.layout["header"].update(self._header_panel(subtitle))

    def set_body(self, renderable: RenderableType) -> None:
        self.layout["body"].update(renderable)

    def append_log(self, line: str) -> None:
        self.log_buffer.append(line)
        self._render_log_panel()

    def _render_log_panel(self) -> None:
        tail = "\n".join(list(self.log_buffer)[-LOG_PANEL_LINES:]) or "[dim]...[/]"
        self.layout["log"].update(Panel(tail, title="Log", border_style="magenta"))

    def __enter__(self) -> TuiApp:
        self.live.start(refresh=True)
        self._active = True
        tui_state.set_active(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._active = False
        tui_state.set_active(None)
        self.live.stop()

    def print_recap(self) -> None:
        """Replay every captured log line to the normal screen after exit.

        The alternate screen buffer is gone by the time this runs, so this is
        the operator's only remaining record of what happened. Printed as one
        joined block rather than one console.print() call per line -- with a
        few thousand buffered lines, per-line printing is itself slow enough
        to look like the program hung right at the moment it should be
        handing control back.
        """
        if self.log_buffer:
            self.console.print("\n".join(self.log_buffer), highlight=False)
