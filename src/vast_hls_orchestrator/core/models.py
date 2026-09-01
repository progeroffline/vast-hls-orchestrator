"""Dataclasses describing remote encode progress and job monitoring state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SshEndpoint:
    """One Vast SSH route; direct and proxy fields must never be mixed."""

    kind: str
    host: str
    port: int


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
    # Set once the remote job enters the uploading stage: total bytes it
    # intends to push (segments + playlists + master), and how many of the
    # parallel upload workers have finished. Actual bytes-transferred-so-far
    # is *not* polled remotely -- origin computes that itself from the local
    # staging directory's real disk usage (see orchestration/publish.py).
    upload_total_bytes: int = 0
    upload_workers_done: int = 0


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
    # Local staging directory Vast is pushing the result into -- lets
    # _log_progress compute upload progress from real local disk usage
    # instead of needing new remote byte-level reporting.
    upload_staging_dir: Path | None = None
    upload_workers_total: int = 0
