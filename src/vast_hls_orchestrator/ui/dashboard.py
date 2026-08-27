"""Renders the four-panel Rich Live dashboard from a remote telemetry snapshot."""

from __future__ import annotations

import time

from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..core.constants import RENDITIONS
from ..core.models import DashboardContext, RemoteSnapshot
from .formatting import bar, format_bytes, format_cost, format_duration


def render_dashboard(ctx: DashboardContext, snap: RemoteSnapshot) -> Group:
    elapsed = max(0.0, time.time() - ctx.started_at)

    summary = Table.grid(expand=True)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_row(
        f"[bold]Instance[/]: {ctx.instance_id}  [bold]GPU[/]: {escape(ctx.gpu_name)}",
        f"[bold]Price[/]: ${ctx.hourly_price:.4f}/h  [bold]Elapsed[/]: {format_duration(elapsed)}",
    )
    summary.add_row(
        "",
        f"[bold]Cost so far[/]: {format_cost(ctx.hourly_price, elapsed)}",
    )
    summary.add_row(
        f"[bold]SSH[/]: {escape(ctx.host)}:{ctx.port}",
        f"[bold]Stage[/]: [cyan]{escape(snap.stage)}[/]  [bold]Status[/]: {escape(snap.status)}",
    )

    if ctx.expected_input_bytes:
        download_pct = snap.downloaded_bytes * 100.0 / ctx.expected_input_bytes
        download_text = Text.assemble(
            ("Download  ", "bold"),
            bar(download_pct),
            (
                f" {download_pct:5.1f}%  {format_bytes(snap.downloaded_bytes)} / {format_bytes(ctx.expected_input_bytes)}",
                "",
            ),
        )
    else:
        download_text = Text(f"Download: {format_bytes(snap.downloaded_bytes)}")

    gpu = Table.grid(expand=True)
    gpu.add_column()
    gpu.add_column()
    gpu.add_column()
    gpu.add_column()
    gpu.add_row(
        f"GPU: {snap.gpu_util:.0f}%" if snap.gpu_util is not None else "GPU: -",
        f"NVENC: {snap.enc_util:.0f}%" if snap.enc_util is not None else "NVENC: -",
        f"NVDEC: {snap.dec_util:.0f}%" if snap.dec_util is not None else "NVDEC: -",
        (
            f"VRAM: {snap.memory_used_mb:.0f}/{snap.memory_total_mb:.0f} MiB"
            if snap.memory_used_mb is not None and snap.memory_total_mb is not None
            else "VRAM: -"
        ),
    )

    progress_table = Table(expand=True, header_style="bold cyan")
    progress_table.add_column("Quality", no_wrap=True)
    progress_table.add_column("Progress", ratio=2)
    progress_table.add_column("Media time", justify="right")
    progress_table.add_column("FPS", justify="right")
    progress_table.add_column("Speed", justify="right")
    progress_table.add_column("ETA", justify="right")
    progress_table.add_column("State", justify="right")

    duration = snap.duration_seconds
    for q in RENDITIONS:
        vp = snap.variants[q]
        pct = (vp.out_time_seconds / duration * 100.0) if duration > 0 else 0.0
        if vp.progress == "end":
            pct = 100.0
        pct = max(0.0, min(100.0, pct))
        remaining_media = (
            max(0.0, duration - vp.out_time_seconds) if duration > 0 else 0.0
        )
        eta = remaining_media / vp.speed if vp.speed > 0 else None
        state_style = (
            "green"
            if vp.progress == "end"
            else ("cyan" if vp.out_time_seconds > 0 else "dim")
        )
        progress_table.add_row(
            q,
            Text.assemble(bar(pct), f" {pct:5.1f}%"),
            f"{format_duration(vp.out_time_seconds)} / {format_duration(duration)}",
            f"{vp.fps:.1f}" if vp.fps else "-",
            f"{vp.speed:.2f}x" if vp.speed else "-",
            format_duration(eta),
            Text(vp.progress, style=state_style),
        )

    remote_text = Text(
        snap.remote_log_tail or "Waiting for remote log output...", style="dim"
    )

    return Group(
        Panel(summary, title="Vast.ai encoder", border_style="cyan"),
        Panel(Group(download_text, gpu), title="Resources", border_style="blue"),
        Panel(progress_table, title="ABR encode", border_style="green"),
        Panel(remote_text, title="Remote log tail", border_style="magenta"),
    )
