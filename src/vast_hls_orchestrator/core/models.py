"""Dataclasses describing remote encode progress and dashboard rendering state."""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import RENDITIONS


@dataclass
class VariantProgress:
    name: str
    frame: int = 0
    fps: float = 0.0
    out_time_seconds: float = 0.0
    speed: float = 0.0
    bitrate: str = "-"
    progress: str = "waiting"


@dataclass
class RemoteSnapshot:
    stage: str = "starting"
    status: str = "RUNNING"
    downloaded_bytes: int = 0
    duration_seconds: float = 0.0
    gpu_util: float | None = None
    enc_util: float | None = None
    dec_util: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    power_w: float | None = None
    variants: dict[str, VariantProgress] = field(
        default_factory=lambda: {q: VariantProgress(q) for q in RENDITIONS}
    )
    remote_log_tail: str = ""


@dataclass
class DashboardContext:
    instance_id: int
    gpu_name: str
    hourly_price: float
    host: str
    port: int
    expected_input_bytes: int | None
    started_at: float
