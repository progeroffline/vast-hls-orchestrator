"""Small renderables for the non-dashboard phases of the pipeline (search, rent, done)."""

from __future__ import annotations

from rich.align import Align
from rich.console import RenderableType
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text


def spinner_panel(message: str, *, title: str = "") -> RenderableType:
    grid = Table.grid(padding=(0, 1))
    grid.add_row(Spinner("dots"), Text(message, style="cyan"))
    return Panel(Align.center(grid, vertical="middle"), title=title or None, border_style="cyan")


def summary_panel(lines: list[str], *, style: str = "green", title: str = "Result") -> RenderableType:
    text = Text("\n".join(lines), style=style)
    return Panel(Align.center(text, vertical="middle"), title=title, border_style=style)
