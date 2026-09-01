"""End-to-end integration test: download -> encode -> finalizing -> direct
parallel upload -> local atomic publish -> complete.

Stitches the real generated job_script.py (run under bash, same fake tool
stand-ins as test_job_script_lifecycle.py) together with the real
finalize_push_publish() -- no reimplementation of either half, and no real
network/SSH/GPU involved anywhere in this test.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from test_job_script_lifecycle import RENDITION_NAMES, _run_job_script

from vast_hls_orchestrator.orchestration.publish import finalize_push_publish


def test_download_encode_finalize_upload_publish_end_to_end(tmp_path: Path):
    result, workspace, origin_staging = _run_job_script(tmp_path)

    diag = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode == 0, diag
    assert (workspace / "JOB_STAGE").read_text().strip() == "complete", diag
    assert (workspace / "JOB_EXIT").read_text().strip() == "0", diag

    # What pipeline.py does next, for real: verify and atomically publish
    # the staging directory the remote job already pushed everything into.
    # finalize_push_publish expects `staging` to live under
    # args.origin_root/args.video_id -- move the pushed-into tree there
    # instead of re-running the upload (that part is already proven above).
    video_dir = tmp_path / "published_video"
    video_dir.mkdir()
    real_staging = video_dir / "abr.staging.test-token"
    origin_staging.rename(real_staging)

    args = argparse.Namespace(origin_root=tmp_path, video_id="published_video")
    dest = finalize_push_publish(args, real_staging, "test-token")

    assert dest == video_dir / "abr"
    assert (dest / "master.m3u8").is_file()
    assert "1080p/index.m3u8" in (dest / "master.m3u8").read_text()
    for r in RENDITION_NAMES:
        assert (dest / r / "index.m3u8").is_file()
        assert (dest / r / "segment_00000.ts").is_file()
