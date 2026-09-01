"""Tests for local_state.prepare_staging_dir and its interplay with
recover_local_publish_state's existing abr.staging.*/abr.backup.* recovery
globs, now that staging/backup directories are named by job_token instead
of instance_id (see local_state.prepare_staging_dir's docstring for why)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from vast_hls_orchestrator.core.errors import VastError
from vast_hls_orchestrator.orchestration.local_state import (
    prepare_staging_dir,
    recover_local_publish_state,
)


def test_prepare_staging_dir_creates_fresh_empty_dir(tmp_path: Path):
    video_dir = tmp_path / "myvideo"
    video_dir.mkdir()

    staging = prepare_staging_dir(video_dir, "job-token-1")

    assert staging == video_dir / "abr.staging.job-token-1"
    assert staging.is_dir()
    assert list(staging.iterdir()) == []


def test_prepare_staging_dir_wipes_leftover_content(tmp_path: Path):
    video_dir = tmp_path / "myvideo"
    staging = video_dir / "abr.staging.job-token-1"
    staging.mkdir(parents=True)
    (staging / "stale.txt").write_text("leftover from a crashed run")

    prepare_staging_dir(video_dir, "job-token-1")

    assert list(staging.iterdir()) == []


def test_prepare_staging_dir_rejects_symlink(tmp_path: Path):
    video_dir = tmp_path / "myvideo"
    video_dir.mkdir()
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    (video_dir / "abr.staging.job-token-1").symlink_to(real_dir)

    with pytest.raises(VastError, match="symlink"):
        prepare_staging_dir(video_dir, "job-token-1")


def test_recovery_globs_still_match_job_token_named_staging(tmp_path: Path):
    """recover_local_publish_state's abr.staging.* glob needs no code change
    for the job_token rename (it already matches any suffix) -- this locks
    that in as a regression test."""
    video_dir = tmp_path / "myvideo"
    video_dir.mkdir()
    stale_staging = video_dir / "abr.staging.myvideo-1700000000-4242"
    stale_staging.mkdir()
    (stale_staging / "partial.ts").write_text("x")

    args = argparse.Namespace(origin_root=tmp_path, video_id="myvideo")
    recover_local_publish_state(args)

    assert not stale_staging.exists()


def test_recovery_globs_still_match_job_token_named_backup(tmp_path: Path):
    video_dir = tmp_path / "myvideo"
    video_dir.mkdir()
    backup = video_dir / "abr.backup.myvideo-1700000000-4242"
    backup.mkdir()
    (backup / "master.m3u8").write_text("#EXTM3U")

    args = argparse.Namespace(origin_root=tmp_path, video_id="myvideo")
    recover_local_publish_state(args)

    # No existing "abr" dir, so the newest backup gets recovered into place.
    assert (video_dir / "abr" / "master.m3u8").exists()
    assert not backup.exists()
