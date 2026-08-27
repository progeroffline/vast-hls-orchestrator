"""Shared Rich console used for both log output and interactive widgets.

Kept as a single instance so log lines, status spinners, tables, and Live
dashboards never fight over stderr.
"""

from rich.console import Console

console = Console(stderr=True)
