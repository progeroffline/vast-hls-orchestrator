"""Regression tests for orchestration/job_monitor.py's progress logging."""

from __future__ import annotations

import time

from loguru import logger

from vast_hls_orchestrator.core.models import JobContext, RemoteSnapshot
from vast_hls_orchestrator.orchestration.job_monitor import _log_progress


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
