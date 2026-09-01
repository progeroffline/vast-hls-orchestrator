"""Tests for validate_inputs()'s checks on the new upload-related CLI args:
--origin-ssh-host, --origin-ssh-port, --upload-workers, --upload-worker-stagger.
"""

from __future__ import annotations

import argparse

import pytest
from loguru import logger

from vast_hls_orchestrator.core.errors import VastError
from vast_hls_orchestrator.core.validation import validate_inputs


def _valid_args(**overrides) -> argparse.Namespace:
    base = {
        "video_id": "test",
        "source_url": "https://origin.example.com/video/test.mp4",
        "disk_gb": 150,
        "boot_timeout": 600,
        "job_timeout": 3600,
        "failsafe_seconds": 14400,
        "ssh_reconnect_timeout": 180,
        "rsync_retries": 4,
        "origin_ssh_port": 22,
        "upload_workers": 16,
        "upload_worker_stagger": 0.5,
        "monitor_interval": 1.5,
        "max_hourly": 0.8,
        "origin_ssh_host": "203.0.113.7",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_valid_args_pass():
    validate_inputs(_valid_args())  # must not raise


def test_rejects_empty_origin_ssh_host():
    with pytest.raises(VastError, match="origin-ssh-host"):
        validate_inputs(_valid_args(origin_ssh_host=""))


def test_rejects_control_characters_in_origin_ssh_host():
    with pytest.raises(VastError, match="origin-ssh-host"):
        validate_inputs(_valid_args(origin_ssh_host="evil\nhost"))


def test_rejects_non_positive_origin_ssh_port():
    with pytest.raises(VastError, match="origin-ssh-port"):
        validate_inputs(_valid_args(origin_ssh_port=0))


@pytest.mark.parametrize("value", [0, -1, 65])
def test_rejects_upload_workers_out_of_range(value):
    with pytest.raises(VastError, match="upload-workers"):
        validate_inputs(_valid_args(upload_workers=value))


@pytest.mark.parametrize("value", [1, 16, 64])
def test_accepts_upload_workers_in_range(value):
    validate_inputs(_valid_args(upload_workers=value))  # must not raise


def test_rejects_negative_upload_worker_stagger():
    with pytest.raises(VastError, match="upload-worker-stagger"):
        validate_inputs(_valid_args(upload_worker_stagger=-0.1))


def test_zero_upload_worker_stagger_warns_but_does_not_raise():
    captured: list[str] = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="WARNING", format="{message}")
    try:
        validate_inputs(_valid_args(upload_worker_stagger=0))  # must not raise
    finally:
        logger.remove(sink_id)
    assert any("upload-worker-stagger" in msg for msg in captured)
