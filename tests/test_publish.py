"""Tests for the local atomic-publish step: finalize_push_publish() and
atomic_exchange_dirs(). The Vast instance has already pushed everything into
`staging` by the time these run (see remote/job_script.py's uploading
stage) -- no network/SSH involved here.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from vast_hls_orchestrator.core.errors import VastError
from vast_hls_orchestrator.orchestration import publish as publish_module
from vast_hls_orchestrator.orchestration.publish import (
    atomic_exchange_dirs,
    finalize_push_publish,
    staging_bytes_on_disk,
)

RENDITIONS = ["1080p", "720p", "480p", "360p"]


def _populate_valid_staging(staging: Path) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    for q in RENDITIONS:
        (staging / q).mkdir(parents=True, exist_ok=True)
        (staging / q / "index.m3u8").write_text("#EXTM3U\n#EXT-X-ENDLIST\n")
        (staging / q / "segment_00000.ts").write_bytes(b"x" * 100)
    (staging / "master.m3u8.tmp").write_text("#EXTM3U\n1080p/index.m3u8\n")


def _args(origin_root: Path, video_id: str = "myvideo") -> argparse.Namespace:
    return argparse.Namespace(origin_root=origin_root, video_id=video_id)


def test_atomic_exchange_dirs_does_not_raise(tmp_path: Path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "marker").write_text("left")
    (right / "marker").write_text("right")

    result = atomic_exchange_dirs(left, right)

    assert isinstance(result, bool)
    if result:
        assert (left / "marker").read_text() == "right"
        assert (right / "marker").read_text() == "left"


def test_finalize_publish_renames_master_tmp_before_exchange(tmp_path: Path):
    video_dir = tmp_path / "myvideo"
    staging = video_dir / "abr.staging.tok1"
    _populate_valid_staging(staging)

    dest = finalize_push_publish(_args(tmp_path), staging, "tok1")

    assert (dest / "master.m3u8").is_file()
    assert not (dest / "master.m3u8.tmp").exists()
    for q in RENDITIONS:
        assert (dest / q / "index.m3u8").is_file()
        assert (dest / q / "segment_00000.ts").is_file()


def test_finalize_publish_rejects_missing_master_tmp(tmp_path: Path):
    video_dir = tmp_path / "myvideo"
    staging = video_dir / "abr.staging.tok1"
    _populate_valid_staging(staging)
    (staging / "master.m3u8.tmp").unlink()

    with pytest.raises(VastError, match="master.m3u8.tmp"):
        finalize_push_publish(_args(tmp_path), staging, "tok1")


def test_finalize_publish_rejects_empty_master_tmp(tmp_path: Path):
    video_dir = tmp_path / "myvideo"
    staging = video_dir / "abr.staging.tok1"
    _populate_valid_staging(staging)
    (staging / "master.m3u8.tmp").write_text("")

    with pytest.raises(VastError, match="master.m3u8.tmp"):
        finalize_push_publish(_args(tmp_path), staging, "tok1")


def test_finalize_publish_rejects_missing_rendition_playlist(tmp_path: Path):
    video_dir = tmp_path / "myvideo"
    staging = video_dir / "abr.staging.tok1"
    _populate_valid_staging(staging)
    (staging / "720p" / "index.m3u8").unlink()

    with pytest.raises(VastError, match="720p"):
        finalize_push_publish(_args(tmp_path), staging, "tok1")


def test_finalize_publish_rejects_missing_segments(tmp_path: Path):
    video_dir = tmp_path / "myvideo"
    staging = video_dir / "abr.staging.tok1"
    _populate_valid_staging(staging)
    (staging / "360p" / "segment_00000.ts").unlink()

    with pytest.raises(VastError, match="360p"):
        finalize_push_publish(_args(tmp_path), staging, "tok1")


def test_finalize_publish_fallback_when_renameat2_unavailable(tmp_path: Path):
    video_dir = tmp_path / "myvideo"
    dest = video_dir / "abr"
    _populate_valid_staging(dest)  # an existing published tree
    (dest / "master.m3u8.tmp").rename(dest / "master.m3u8")

    staging = video_dir / "abr.staging.tok2"
    _populate_valid_staging(staging)

    with patch.object(publish_module, "atomic_exchange_dirs", return_value=False):
        result_dest = finalize_push_publish(_args(tmp_path), staging, "tok2")

    assert result_dest == dest
    assert (dest / "master.m3u8").is_file()
    assert not staging.exists()
    # Backup cleaned up on success.
    assert not (video_dir / "abr.backup.tok2").exists()


def test_finalize_publish_second_run_replaces_first(tmp_path: Path):
    video_dir = tmp_path / "myvideo"

    staging1 = video_dir / "abr.staging.tok1"
    _populate_valid_staging(staging1)
    dest = finalize_push_publish(_args(tmp_path), staging1, "tok1")
    first_master = (dest / "master.m3u8").read_text()

    staging2 = video_dir / "abr.staging.tok2"
    _populate_valid_staging(staging2)
    (staging2 / "master.m3u8.tmp").write_text("#EXTM3U\n# second publish\n")
    dest2 = finalize_push_publish(_args(tmp_path), staging2, "tok2")

    assert dest2 == dest
    second_master = (dest / "master.m3u8").read_text()
    assert second_master != first_master
    assert "second publish" in second_master


def test_staging_bytes_on_disk_reflects_real_written_bytes(tmp_path: Path):
    staging = tmp_path / "staging"
    _populate_valid_staging(staging)

    total = staging_bytes_on_disk(staging)

    assert total > 0
    # Should be at least the sum of segment sizes actually written.
    assert total >= 100 * len(RENDITIONS)


def test_staging_bytes_on_disk_empty_dir_is_zero(tmp_path: Path):
    staging = tmp_path / "empty"
    staging.mkdir()
    assert staging_bytes_on_disk(staging) == 0
