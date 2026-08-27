"""Renders the four-panel Rich Live dashboard from a remote telemetry snapshot."""

from __future__ import annotations

import time

from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..core.models import DashboardContext, RemoteSnapshot
from .formatting import bar, format_bytes, format_cost, format_duration

LADDER_SUMMARY = "1080p 6500k  ·  720p 3500k  ·  480p 1800k  ·  360p 900k"


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

    # One shared NVDEC decode feeds all four renditions (GPU-side split), so
    # they share a single progress feed by construction -- FFmpeg's
    # `-progress` protocol reports one frame/out_time/speed for the whole
    # multi-output process, not one per rendition.
    encode = snap.encode
    duration = snap.duration_seconds
    pct = (encode.out_time_seconds / duration * 100.0) if duration > 0 else 0.0
    if encode.progress == "end":
        pct = 100.0
    pct = max(0.0, min(100.0, pct))
    remaining_media = max(0.0, duration - encode.out_time_seconds) if duration > 0 else 0.0
    eta = remaining_media / encode.speed if encode.speed > 0 else None
    state_style = (
        "green"
        if encode.progress == "end"
        else ("cyan" if encode.out_time_seconds > 0 else "dim")
    )

    progress_table = Table(expand=True, header_style="bold cyan")
    progress_table.add_column("Progress", ratio=2)
    progress_table.add_column("Media time", justify="right")
    progress_table.add_column("FPS", justify="right")
    progress_table.add_column("Speed", justify="right")
    progress_table.add_column("ETA", justify="right")
    progress_table.add_column("State", justify="right")
    progress_table.add_row(
        Text.assemble(bar(pct), f" {pct:5.1f}%"),
        f"{format_duration(encode.out_time_seconds)} / {format_duration(duration)}",
        f"{encode.fps:.1f}" if encode.fps else "-",
        f"{encode.speed:.2f}x" if encode.speed else "-",
        format_duration(eta),
        Text(encode.progress, style=state_style),
    )

    remote_text = Text(
        snap.remote_log_tail or "Waiting for remote log output...", style="dim"
    )

    return Group(
        Panel(summary, title="Vast.ai encoder", border_style="cyan"),
        Panel(Group(download_text, gpu), title="Resources", border_style="blue"),
        Panel(
            Group(progress_table, Text(LADDER_SUMMARY, style="dim")),
            title="ABR encode (1 decode -> 4x GPU split -> NVENC)",
            border_style="green",
        ),
        Panel(remote_text, title="Remote log tail", border_style="magenta"),
    )
