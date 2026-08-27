"""Dataclasses describing remote encode progress and job monitoring state."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EncodeProgress:
    """Progress of the single ABR-encode FFmpeg process.

    One shared NVDEC decode feeds all four renditions (split + scale_cuda on
    the GPU), so frame/out_time/speed are inherently identical across all of
    them -- FFmpeg's `-progress` protocol reports one aggregate set of these
    fields for a multi-output process, not one per output, which happens to
    match reality here rather than losing information.
    """

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
    encode: EncodeProgress = field(default_factory=EncodeProgress)
    remote_log_tail: str = ""


@dataclass
class JobContext:
    """Identifies which instance/job a monitoring log line is about."""

    instance_id: int
    gpu_name: str
    hourly_price: float
    host: str
    port: int
    expected_input_bytes: int | None
    started_at: float
