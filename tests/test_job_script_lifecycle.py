"""End-to-end tests for the remote job script's full lifecycle: download ->
encode -> finalizing -> direct parallel upload -> complete.

Runs the ACTUAL script build_job_script() generates -- not a reimplementation
of its logic -- under bash, with /workspace remapped to a pytest tmp_path and
aria2c/ffprobe/nvidia-smi/ffmpeg/rsync replaced by tiny stand-ins on PATH.
There's no GPU or network in a unit test, but the things these tests guard
against (the EXIT-trap lifecycle bug; the upload stage's batching/retry/
ordering/atomicity) have nothing to do with what those tools actually do.

The fake `rsync` does a local file copy instead of a real SSH transfer (it
ignores the `user@host:` prefix and copies to the path after the colon),
standing in for "the remote job successfully pushed to origin" without any
real network access.
"""

from __future__ import annotations

import itertools
import os
import stat
import subprocess
from pathlib import Path

from vast_hls_orchestrator.remote.job_script import LADDER, build_job_script

RENDITION_NAMES = [name for name, *_ in LADDER]

_FAKE_TOOLS = {
    "aria2c": """#!/usr/bin/env bash
set -e
dir="."
name="out"
while [ $# -gt 0 ]; do
  case "$1" in
    -d) dir="$2"; shift 2 ;;
    -o) name="$2"; shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "$dir"
head -c 65536 /dev/urandom > "$dir/$name"
""",
    "ffprobe": """#!/usr/bin/env bash
echo 5
""",
    "nvidia-smi": """#!/usr/bin/env bash
exit 0
""",
    # `df -PB1` is a GNU coreutils flag combo (the real remote is Ubuntu);
    # BSD/macOS df rejects -B, so stub it in the exact POSIX -P output shape
    # the script parses (`awk 'NR==2 {print $4}'` reads Available bytes) to
    # keep the disk-space checks portable for a local test run.
    "df": """#!/usr/bin/env bash
echo "Filesystem 1B-blocks Used Available Capacity Mounted"
echo "fake 999999999999 0 999999999999 0% /"
""",
    # `find -printf` is GNU-only (the real remote is Ubuntu); BSD/macOS find
    # has no -printf at all. Stub just the one shape the uploading stage's
    # manifest build uses (`find DIR -name ... -type f -printf '%s %P\n'`),
    # delegating everything else (e.g. the finalizing stage's existing
    # `-print -quit` check, already portable) to the real find.
    "find": """#!/usr/bin/env bash
has_printf=0
for a in "$@"; do
  [ "$a" = "-printf" ] && has_printf=1
done
if [ "$has_printf" = "1" ]; then
  dir="$1"
  /usr/bin/find "$dir" -name 'segment_*.ts' -type f -print | while IFS= read -r f; do
    size=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f")
    rel="${f#"$dir"/}"
    printf '%s %s\\n' "$size" "$rel"
  done
else
  exec /usr/bin/find "$@"
fi
""",
    # GNU `stat -c FORMAT FILE` vs BSD `stat -f FORMAT FILE` (real remote is
    # Ubuntu/GNU); stub just the `-c '%s'` (file size) shape the uploading
    # stage uses for playlist/master byte counts.
    "stat": """#!/usr/bin/env bash
if [ "$1" = "-c" ]; then
  fmt="$2"
  file="$3"
  case "$fmt" in
    '%s') /usr/bin/stat -f%z "$file" ;;
    *) echo "fake stat: unsupported format $fmt" >&2; exit 1 ;;
  esac
else
  exec /usr/bin/stat "$@"
fi
""",
    # Local-copy stand-in for a real SSH-based rsync push. Ignores the
    # `user@host:` prefix in the destination arg (everything after the first
    # ':' is a local path on this same test machine) and skips '-e ssh ...'
    # (its value is a single following argv token to ignore). Supports
    # optional failure injection (FAKE_RSYNC_FAIL_BATCH/_FAIL_COUNT) for
    # retry tests, and an optional order log (FAKE_RSYNC_ORDER_LOG) for
    # ordering tests.
    "rsync": """#!/usr/bin/env bash
set -euo pipefail

files_from=""
src=""
dest=""
skip_next=0
for arg in "$@"; do
  if [ "$skip_next" = "1" ]; then
    skip_next=0
    continue
  fi
  case "$arg" in
    --files-from=*) files_from="${arg#--files-from=}" ;;
    -e) skip_next=1 ;;
    *@*:*) dest="$arg" ;;
    -*) : ;;
    *) src="$arg" ;;
  esac
done
dest_path="${dest#*:}"

if [ -n "${FAKE_RSYNC_FAIL_BATCH:-}" ] && [ "$(basename "${files_from:-nope}")" = "$FAKE_RSYNC_FAIL_BATCH" ]; then
  state_file="${FAKE_RSYNC_STATE_DIR:-/tmp}/fake_rsync_attempts_$(basename "$files_from")"
  count=0
  [ -f "$state_file" ] && count=$(cat "$state_file")
  count=$((count + 1))
  echo "$count" > "$state_file"
  max="${FAKE_RSYNC_FAIL_COUNT:-999999}"
  if [ "$count" -le "$max" ]; then
    echo "fake rsync: simulated failure for $files_from (attempt $count)" >&2
    exit 23
  fi
fi

if [ -n "$files_from" ]; then
  mkdir -p "$dest_path"
  while IFS= read -r rel; do
    [ -z "$rel" ] && continue
    mkdir -p "$dest_path/$(dirname "$rel")"
    cp "$src$rel" "$dest_path/$rel"
  done < "$files_from"
  if [ -n "${FAKE_RSYNC_ORDER_LOG:-}" ]; then
    echo "SEGMENTS $(basename "$files_from")" >> "$FAKE_RSYNC_ORDER_LOG"
  fi
else
  mkdir -p "$(dirname "$dest_path")"
  cp "$src" "$dest_path"
  if [ -n "${FAKE_RSYNC_ORDER_LOG:-}" ]; then
    case "$dest_path" in
      */master.m3u8.tmp) echo "MASTER" >> "$FAKE_RSYNC_ORDER_LOG" ;;
      *) echo "PLAYLIST $dest_path" >> "$FAKE_RSYNC_ORDER_LOG" ;;
    esac
  fi
fi

if [ -n "${FAKE_RSYNC_WORKER_START_LOG:-}" ] && [ -n "$files_from" ]; then
  echo "$(date +%s.%N) $(basename "$files_from")" >> "$FAKE_RSYNC_WORKER_START_LOG"
fi
""",
}


def _fake_ffmpeg_script(*, segments_per_rendition: int = 1) -> str:
    # FAKE_FFMPEG_ENCODE_MODE picks what the *main* encode invocation does;
    # the -encoders/-filters/preflight (-f null) invocations always succeed,
    # matching offers that already passed the real GPU preflight.
    renditions = " ".join(RENDITION_NAMES)
    return f"""#!/usr/bin/env bash
args="$*"
case "$args" in
  *-encoders*) echo " V..... h264_nvenc  fake NVENC h264 encoder"; exit 0 ;;
  *-filters*) echo " ... scale_cuda   fake CUDA scale filter"; exit 0 ;;
  *"-f null"*) exit 0 ;;
  *)
    if [ "${{FAKE_FFMPEG_ENCODE_MODE:-success}}" = "fail" ]; then
      exit 7
    fi
    out="$WORKSPACE/out"
    for r in {renditions}; do
      mkdir -p "$out/$r"
      printf '#EXTM3U\\n#EXT-X-ENDLIST\\n' > "$out/$r/index.m3u8"
      for i in $(seq 0 $(({segments_per_rendition} - 1))); do
        seg=$(printf 'segment_%05d.ts' "$i")
        printf 'fake-segment-data' > "$out/$r/$seg"
      done
    done
    # Satisfy the progress relay's blocking open+read, then let it see EOF,
    # same as a real ffmpeg -progress FIFO would.
    exec 3> "$out/progress.fifo"
    printf 'frame=1\\nprogress=end\\n' >&3
    exec 3>&-
    exit 0
    ;;
esac
"""


def _run_job_script(
    tmp_path: Path,
    *,
    encode_mode: str = "success",
    upload_workers: int = 16,
    upload_worker_stagger: float = 0.05,
    upload_retries: int = 4,
    segments_per_rendition: int = 1,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    origin_staging = tmp_path / "origin_staging"
    origin_staging.mkdir()
    fake_rsync_state_dir = tmp_path / "rsync_state"
    fake_rsync_state_dir.mkdir()
    # Real /root/.ssh (onstart.py's key_setup guarantees it exists before
    # job_script.py ever runs, so the uploading stage assumes it too) --
    # substituted the same way /workspace is, since this test can't actually
    # write to /root.
    root_ssh = tmp_path / "root_ssh"
    root_ssh.mkdir(mode=0o700)

    tools = dict(_FAKE_TOOLS)
    tools["ffmpeg"] = _fake_ffmpeg_script(segments_per_rendition=segments_per_rendition)
    for tool_name, content in tools.items():
        tool_path = bin_dir / tool_name
        tool_path.write_text(content)
        mode = tool_path.stat().st_mode
        tool_path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # A small expected size keeps the disk-space checks trivially satisfied
    # (they're mostly a fixed ~2 GiB reserve regardless) without needing a
    # multi-GB fake download.
    script = build_job_script(
        "https://example.com/source.mp4",
        expected_input_bytes=1000,
        staging_remote_path=str(origin_staging),
        origin_ssh_user="deployuser",
        origin_ssh_host="origin.invalid",
        origin_ssh_port=22,
        upload_workers=upload_workers,
        upload_worker_stagger=upload_worker_stagger,
        upload_retries=upload_retries,
    )
    script = script.replace("/workspace", str(workspace)).replace("/root/.ssh", str(root_ssh))

    script_path = tmp_path / "encode-job.sh"
    script_path.write_text(script)
    script_path.chmod(0o700)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["WORKSPACE"] = str(workspace)
    env["FAKE_FFMPEG_ENCODE_MODE"] = encode_mode
    env["FAKE_RSYNC_STATE_DIR"] = str(fake_rsync_state_dir)
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [str(script_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    return result, workspace, origin_staging


def test_full_lifecycle_success_reaches_complete_and_job_done(tmp_path: Path):
    """encoding -> finalizing -> uploading -> complete, with JOB_DONE/JOB_EXIT
    written and the full result pushed to the (fake, local) origin staging
    directory -- master pushed as master.m3u8.tmp, not master.m3u8, since
    origin (not the remote job) does that final rename+atomic-publish step.
    """
    result, workspace, origin_staging = _run_job_script(tmp_path)

    diag = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert (workspace / "JOB_DONE").exists(), diag
    assert (workspace / "JOB_EXIT").read_text().strip() == "0", diag
    assert (workspace / "JOB_STAGE").read_text().strip() == "complete", diag
    assert result.returncode == 0, diag
    assert "STAGE: uploading" in result.stdout, diag

    assert (origin_staging / "master.m3u8.tmp").is_file(), diag
    assert not (origin_staging / "master.m3u8").exists(), diag
    for r in RENDITION_NAMES:
        assert (origin_staging / r / "index.m3u8").is_file(), diag
        assert (origin_staging / r / "segment_00000.ts").is_file(), diag


def test_encode_failure_reaches_failed_and_job_done(tmp_path: Path):
    """A failed encode must still reach JOB_DONE, with JOB_STAGE=failed and
    a nonzero JOB_EXIT -- not hang with no trap ever firing. The upload
    stage must never even start."""
    result, workspace, origin_staging = _run_job_script(tmp_path, encode_mode="fail")

    diag = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert (workspace / "JOB_DONE").exists(), diag
    assert (workspace / "JOB_EXIT").read_text().strip() != "0", diag
    assert (workspace / "JOB_STAGE").read_text().strip() == "failed", diag
    assert result.returncode != 0, diag
    assert "STAGE: uploading" not in result.stdout, diag
    assert list(origin_staging.iterdir()) == [], diag


def test_segments_pushed_before_playlists_before_master(tmp_path: Path):
    order_log = tmp_path / "order.log"
    result, _workspace, origin_staging = _run_job_script(
        tmp_path, extra_env={"FAKE_RSYNC_ORDER_LOG": str(order_log)}
    )

    diag = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode == 0, diag
    assert (origin_staging / "master.m3u8.tmp").is_file(), diag

    entries = order_log.read_text().splitlines()
    kinds = [line.split()[0] for line in entries]
    assert "SEGMENTS" in kinds and "PLAYLIST" in kinds and "MASTER" in kinds, entries
    last_segments_idx = max(i for i, k in enumerate(kinds) if k == "SEGMENTS")
    first_playlist_idx = min(i for i, k in enumerate(kinds) if k == "PLAYLIST")
    master_idx = kinds.index("MASTER")
    assert last_segments_idx < first_playlist_idx < master_idx, entries


def test_single_batch_failure_retries_only_that_batch(tmp_path: Path):
    """One batch failing twice (then succeeding) must not force re-sending
    already-succeeded batches -- each batch's own retry loop is independent."""
    result, _workspace, origin_staging = _run_job_script(
        tmp_path,
        upload_workers=4,  # one worker per rendition's single segment
        extra_env={
            "FAKE_RSYNC_FAIL_BATCH": "batch_0.list",
            "FAKE_RSYNC_FAIL_COUNT": "2",
        },
    )

    diag = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode == 0, diag
    assert (origin_staging / "master.m3u8.tmp").is_file(), diag

    attempts_dir = tmp_path / "rsync_state"
    batch0_attempts = int((attempts_dir / "fake_rsync_attempts_batch_0.list").read_text().strip())
    assert batch0_attempts == 3  # 2 failures + 1 success

    # No other batch's state file should exist -- they only ever ran once
    # and only batch_0 has failure-injection state tracked at all.
    other_state_files = [
        p for p in attempts_dir.iterdir() if p.name != "fake_rsync_attempts_batch_0.list"
    ]
    assert other_state_files == []


def test_persistent_batch_failure_fails_job_and_never_publishes_master(tmp_path: Path):
    result, workspace, origin_staging = _run_job_script(
        tmp_path,
        upload_workers=4,
        upload_retries=3,
        extra_env={"FAKE_RSYNC_FAIL_BATCH": "batch_1.list"},  # never succeeds
    )

    diag = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode != 0, diag
    assert (workspace / "JOB_DONE").exists(), diag
    assert (workspace / "JOB_EXIT").read_text().strip() != "0", diag
    assert (workspace / "JOB_STAGE").read_text().strip() == "failed", diag
    assert not (origin_staging / "master.m3u8.tmp").exists(), diag
    assert not any(origin_staging.glob("*/index.m3u8")), diag

    attempts = int((tmp_path / "rsync_state" / "fake_rsync_attempts_batch_1.list").read_text().strip())
    assert attempts == 3  # exactly upload_retries attempts, no more


def test_worker_starts_are_staggered(tmp_path: Path):
    start_log = tmp_path / "starts.log"
    stagger = 0.3
    result, _workspace, _origin_staging = _run_job_script(
        tmp_path,
        upload_workers=3,
        upload_worker_stagger=stagger,
        segments_per_rendition=1,
        extra_env={"FAKE_RSYNC_WORKER_START_LOG": str(start_log)},
    )

    diag = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode == 0, diag

    lines = [line for line in start_log.read_text().splitlines() if line.strip()]
    timestamps = sorted(float(line.split()[0]) for line in lines)
    assert len(timestamps) >= 2, lines
    gaps = [b - a for a, b in itertools.pairwise(timestamps)]
    # Real scheduling jitter (worse still under a fully-loaded test run) means
    # gaps won't be exact, but they should be unmistakably closer to `stagger`
    # than to 0 (simultaneous starts, which this threshold would catch).
    assert all(gap >= stagger * 0.25 for gap in gaps), (gaps, stagger)
