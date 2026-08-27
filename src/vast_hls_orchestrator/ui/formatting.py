"""Small display-value formatters shared by the dashboard."""

from __future__ import annotations

from rich.text import Text


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "--:--:--"
    seconds_i = int(seconds)
    h, rem = divmod(seconds_i, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_bytes(n: int | None) -> str:
    if not n or n <= 0:
        return "0 B"
    value = float(n)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def bar(percent: float, width: int = 22) -> Text:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100.0))
    text = Text()
    text.append("█" * filled, style="green")
    text.append("░" * (width - filled), style="grey37")
    return text
