"""Parses FFmpeg `-progress` records and nvidia-smi output polled over SSH."""

from __future__ import annotations

import argparse
import re

from ..core.constants import RENDITIONS
from ..core.errors import VastError
from ..core.models import RemoteSnapshot, VariantProgress
from .ssh import ssh_run


def parse_time_value(value: str) -> float:
    value = value.strip()
    if not value or value == "N/A":
        return 0.0
    if value.isdigit():
        return float(value) / 1_000_000.0
    match = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", value)
    if match:
        h, m, s = match.groups()
        return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


def parse_speed(value: str) -> float:
    value = value.strip().lower().rstrip("x")
    try:
        return float(value)
    except ValueError:
        return 0.0


def parse_progress_block(name: str, block: str) -> VariantProgress:
    # Native -progress is record based. Use only the last complete record if a
    # non-atomic/older remote script is encountered while it is writing.
    lines = block.splitlines()
    complete_at = [i for i, line in enumerate(lines) if line.startswith("progress=")]
    if complete_at:
        previous = complete_at[-2] + 1 if len(complete_at) > 1 else 0
        lines = lines[previous : complete_at[-1] + 1]
    values: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    out_seconds = 0.0
    if values.get("out_time_us", "").isdigit():
        out_seconds = int(values["out_time_us"]) / 1_000_000.0
    elif values.get("out_time_ms", "").isdigit():
        # FFmpeg historically labels this field ms, but its value is microseconds.
        out_seconds = int(values["out_time_ms"]) / 1_000_000.0
    elif values.get("out_time"):
        out_seconds = parse_time_value(values["out_time"])

    return VariantProgress(
        name=name,
        frame=int(float(values.get("frame", "0") or 0)),
        fps=float(values.get("fps", "0") or 0),
        out_time_seconds=out_seconds,
        speed=parse_speed(values.get("speed", "0")),
        bitrate=values.get("bitrate", "-"),
        progress=values.get("progress", "waiting"),
    )


def fetch_remote_snapshot(
    args: argparse.Namespace,
    host: str,
    port: int,
) -> RemoteSnapshot:
    command = r"""
printf 'META_STAGE='; cat /workspace/JOB_STAGE 2>/dev/null || echo bootstrap
printf 'META_STATUS='; if [ -f /workspace/JOB_DONE ]; then printf 'DONE:'; cat /workspace/JOB_EXIT 2>/dev/null || echo 1; else echo RUNNING; fi
printf 'META_DOWNLOADED='; stat -c %s /workspace/input/source.mp4 2>/dev/null || echo 0
printf 'META_DURATION='; cat /workspace/input/duration.txt 2>/dev/null || echo 0
printf 'META_GPU='; nvidia-smi --query-gpu=utilization.gpu,utilization.encoder,utilization.decoder,memory.used,memory.total,power.draw --format=csv,noheader,nounits 2>/dev/null || true
for q in 1080p 720p 480p 360p; do
  echo "PROGRESS_BEGIN:$q"
  cat "/workspace/out/$q/progress.txt" 2>/dev/null || true
  echo "PROGRESS_END:$q"
done
echo 'REMOTE_LOG_BEGIN'
{{ tail -c 4000 /workspace/bootstrap.log 2>/dev/null; tail -c 4000 /workspace/job.log 2>/dev/null; }} | tr '\r' '\n' | tail -n 8 || true
echo 'REMOTE_LOG_END'
"""
    result = ssh_run(args, host, port, command, timeout=20)
    if result.returncode != 0 and not result.stdout:
        raise VastError(f"Remote status query failed: {result.stderr.strip()}")

    snapshot = RemoteSnapshot()
    lines = result.stdout.splitlines()
    progress_blocks: dict[str, list[str]] = {q: [] for q in RENDITIONS}
    current_q: str | None = None
    in_log = False
    remote_log: list[str] = []

    for line in lines:
        if in_log:
            if line == "REMOTE_LOG_END":
                in_log = False
            else:
                remote_log.append(line)
        elif line.startswith("META_STAGE="):
            snapshot.stage = line.split("=", 1)[1].strip() or "unknown"
        elif line.startswith("META_STATUS="):
            snapshot.status = line.split("=", 1)[1].strip() or "RUNNING"
        elif line.startswith("META_DOWNLOADED="):
            try:
                snapshot.downloaded_bytes = int(line.split("=", 1)[1].strip() or 0)
            except ValueError:
                pass
        elif line.startswith("META_DURATION="):
            try:
                snapshot.duration_seconds = float(line.split("=", 1)[1].strip() or 0)
            except ValueError:
                pass
        elif line.startswith("META_GPU="):
            payload = line.split("=", 1)[1].strip()
            if payload:
                parts = [x.strip() for x in payload.split(",")]
                try:
                    values = [
                        float(x) if x not in {"", "N/A", "[N/A]"} else None
                        for x in parts
                    ]
                    while len(values) < 6:
                        values.append(None)
                    (
                        snapshot.gpu_util,
                        snapshot.enc_util,
                        snapshot.dec_util,
                        snapshot.memory_used_mb,
                        snapshot.memory_total_mb,
                        snapshot.power_w,
                    ) = values[:6]
                except ValueError:
                    pass
        elif line.startswith("PROGRESS_BEGIN:"):
            current_q = line.split(":", 1)[1]
        elif line.startswith("PROGRESS_END:"):
            current_q = None
        elif line == "REMOTE_LOG_BEGIN":
            in_log = True
        elif current_q in progress_blocks:
            progress_blocks[current_q].append(line)

    for q in RENDITIONS:
        snapshot.variants[q] = parse_progress_block(q, "\n".join(progress_blocks[q]))
    snapshot.remote_log_tail = "\n".join(remote_log).strip()
    return snapshot
