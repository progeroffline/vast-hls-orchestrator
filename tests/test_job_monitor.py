"""Regression tests for orchestration/job_monitor.py's progress logging."""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from vast_hls_orchestrator.core.models import JobContext, RemoteSnapshot
from vast_hls_orchestrator.orchestration.job_monitor import _log_progress


def _captured_log(ctx: JobContext, snap: RemoteSnapshot, upload_state: dict | None = None) -> str:
    captured: list[str] = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), format="{message}")
    try:
        _log_progress(ctx, snap, upload_state if upload_state is not None else {})
    finally:
        logger.remove(sink_id)
    assert captured, "expected a log line"
    return captured[-1]


def _log_progress_message(downloaded_bytes: int, expected_input_bytes: int) -> str:
    ctx = JobContext(
        instance_id=1,
        gpu_name="RTX 4080",
        hourly_price=0.2,
        host="host",
        port=22,
        expected_input_bytes=expected_input_bytes,
        started_at=time.time(),
    )
    snap = RemoteSnapshot(downloaded_bytes=downloaded_bytes)

    captured: list[str] = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), format="{message}")
    try:
        _log_progress(ctx, snap)
    finally:
        logger.remove(sink_id)

    assert captured, "expected a log line"
    return captured[-1]


def test_download_percentage_never_exceeds_100_percent():
    # downloaded_bytes is real disk blocks allocated (stat %b, see
    # remote/snapshot.py) while expected_input_bytes is the source URL's
    # logical Content-Length -- different units that can legitimately
    # diverge by a few percent from filesystem block-alignment overhead
    # across aria2's 16 parallel segments (observed live: 104.7%). The
    # displayed percentage must still cap at 100%.
    message = _log_progress_message(
        downloaded_bytes=9_879_000_000, expected_input_bytes=9_430_000_000
    )
    assert "100.0%" in message
    assert "104" not in message


def test_download_percentage_normal_case_unaffected():
    message = _log_progress_message(
        downloaded_bytes=4_715_000_000, expected_input_bytes=9_430_000_000
    )
    assert "50.0%" in message


def _upload_ctx(staging: Path, workers_total: int = 16) -> JobContext:
    return JobContext(
        instance_id=1,
        gpu_name="RTX 4080",
        hourly_price=0.2,
        host="host",
        port=22,
        expected_input_bytes=None,
        started_at=time.time(),
        upload_staging_dir=staging,
        upload_workers_total=workers_total,
    )


def test_uploading_stage_shows_progress_from_local_staging_disk_usage(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "segment_00000.ts").write_bytes(b"x" * 1000)

    snap = RemoteSnapshot(stage="uploading", upload_total_bytes=10_000, upload_workers_done=5)
    message = _captured_log(_upload_ctx(staging), snap)

    assert "stage=uploading" in message
    assert "uploaded=" in message
    assert "workers=5/16" in message
    # No stale encode-stage fields should leak into the uploading line.
    assert "fps=" not in message
    assert "download=" not in message


def test_uploading_stage_percentage_clamped_to_100(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "segment_00000.ts").write_bytes(b"x" * 20_000)

    # A tiny declared total next to real (block-rounded) disk usage mirrors
    # the same over-100% scenario already fixed for download progress.
    snap = RemoteSnapshot(stage="uploading", upload_total_bytes=1000, upload_workers_done=1)
    message = _captured_log(_upload_ctx(staging), snap)

    assert "100.0%" in message


def test_uploading_stage_speed_computed_from_two_samples(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    state: dict = {}

    (staging / "a.ts").write_bytes(b"x" * 1000)
    snap = RemoteSnapshot(stage="uploading", upload_total_bytes=1_000_000, upload_workers_done=1)
    first = _captured_log(_upload_ctx(staging), snap, state)
    assert "speed=" not in first  # no prior sample yet

    state["at"] = state["at"] - 1.0  # simulate a second having elapsed
    (staging / "b.ts").write_bytes(b"x" * 500_000)
    second = _captured_log(_upload_ctx(staging), snap, state)
    assert "speed=" in second


def test_non_uploading_stage_does_not_show_upload_fields(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    snap = RemoteSnapshot(stage="encoding", upload_total_bytes=1000, upload_workers_done=0)

    message = _captured_log(_upload_ctx(staging), snap)

    assert "uploaded=" not in message
    assert "workers=" not in message
