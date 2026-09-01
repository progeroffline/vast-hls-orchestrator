"""Builds the remote Bash job: download source, preflight GPU, encode ABR HLS.

Encoding is one FFmpeg process: a single NVDEC decode of the source is split
on the GPU into four branches, each scaled (scale_cuda) and encoded (NVENC)
to its own HLS rendition, instead of four separate processes each redundantly
decoding the whole source from scratch. NVIDIA documents this 1:N transcode
pattern directly. Note this does not multiply NVENC throughput on its own --
GPUs with a single physical NVENC engine (the --gpus default: RTX 3060/A2000/
4060) still time-slice all four encode sessions on that one engine either
way; the win here is skipping 4x redundant decode/host-copy work, and it
scales further automatically on GPUs with multiple NVENC engines (RTX 4070
Ti and above).
"""

from __future__ import annotations

import shlex
from pathlib import Path

LADDER = [
    # name,  size,       bitrate, maxrate, bufsize, audio, cq
    ("1080p", "1920:1080", "6500k", "7150k", "13000k", "160k", 19),
    ("720p", "1280:720", "3500k", "3850k", "7000k", "128k", 20),
    ("480p", "854:480", "1800k", "2000k", "3600k", "128k", 21),
    ("360p", "640:360", "900k", "1000k", "1800k", "96k", 22),
]


def _split_filter() -> str:
    labels = [name for name, *_ in LADDER]
    split_outputs = "".join(f"[v{name}]" for name in labels)
    scales = "; ".join(
        f"[v{name}]scale_cuda={size}[s{name}]" for name, size, *_ in LADDER
    )
    return f"[0:v]split={len(LADDER)}{split_outputs}; {scales}"


def _preflight_filter() -> str:
    labels = [name for name, *_ in LADDER]
    split_outputs = "".join(f"[p{name}]" for name in labels)
    scales = "; ".join(
        f"[p{name}]scale_cuda={size}[q{name}]" for name, size, *_ in LADDER
    )
    return f"[0:v]split={len(LADDER)}{split_outputs}; {scales}"


def _preflight_maps() -> str:
    return " \\\n  ".join(
        f'-map "[q{name}]" -c:v h264_nvenc -preset p3 -tune hq' for name, *_ in LADDER
    )


def _encode_outputs(out_dir: str, fifo: str) -> str:
    blocks = []
    last_index = len(LADDER) - 1
    for i, (name, _size, bitrate, maxrate, bufsize, audio, cq) in enumerate(LADDER):
        progress = f' \\\n    -progress "{fifo}"' if i == last_index else ""
        blocks.append(
            f'-map "[s{name}]" -map "0:a:0?" -c:v h264_nvenc -preset p3 -tune hq '
            f"-rc vbr -cq {cq} \\\n"
            f"    -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} \\\n"
            f"    -force_key_frames 'expr:gte(t,n_forced*6)' -forced-idr 1 \\\n"
            f"    -c:a aac -b:a {audio} -ac 2 \\\n"
            f"    -hls_time 6 -hls_playlist_type vod -hls_flags independent_segments \\\n"
            f'    -hls_segment_filename "{out_dir}/{name}/segment_%05d.ts"{progress} '
            f'"{out_dir}/{name}/index.m3u8"'
        )
    return " \\\n  ".join(blocks)


def _embedded_upload_batching_source() -> str:
    # Embedded verbatim into the uploading stage below (see
    # _upload_stage_script) so there's exactly one source of truth for the
    # batching algorithm -- unit-tested directly as plain Python in
    # tests/test_upload_batching.py, and byte-identical to what runs here.
    return Path(__file__).with_name("upload_batching.py").read_text(encoding="utf-8")


def _upload_stage_script(
    *,
    staging_remote_path: str,
    origin_ssh_user: str,
    origin_ssh_host: str,
    origin_ssh_port: int,
    upload_workers: int,
    upload_worker_stagger: float,
    upload_retries: int,
    rendition_list: str,
) -> str:
    quoted_dest = shlex.quote(f"{origin_ssh_user}@{origin_ssh_host}:{staging_remote_path}")
    batching_source = _embedded_upload_batching_source()
    return f"""
set_stage uploading
echo "=== Upload result to origin (direct push, up to {upload_workers} parallel workers) ==="

UPLOAD_DIR=/workspace/upload
rm -rf "$UPLOAD_DIR"
mkdir -p "$UPLOAD_DIR"
: > "$UPLOAD_DIR/done_workers"

DEPLOY_KEY=/root/.ssh/vast_deploy_key
ORIGIN_KNOWN_HOSTS=/root/.ssh/known_hosts_origin
: > "$ORIGIN_KNOWN_HOSTS"
ORIGIN_SSH_OPTS="-i $DEPLOY_KEY -p {origin_ssh_port} -c aes128-gcm@openssh.com -o Compression=no -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$ORIGIN_KNOWN_HOSTS"
ORIGIN_DEST={quoted_dest}

find "$OUT" -name 'segment_*.ts' -type f -printf '%s %P\\n' > "$UPLOAD_DIR/manifest.txt"
segment_count=$(wc -l < "$UPLOAD_DIR/manifest.txt")
num_workers={upload_workers}
if [ "$segment_count" -lt "$num_workers" ]; then
  num_workers=$segment_count
fi
if [ "$num_workers" -lt 1 ]; then
  num_workers=1
fi

cat > "$UPLOAD_DIR/upload_batching.py" <<'UPLOAD_BATCHING_PY_EOF'
{batching_source}UPLOAD_BATCHING_PY_EOF
python3 "$UPLOAD_DIR/upload_batching.py" "$num_workers" "$UPLOAD_DIR" < "$UPLOAD_DIR/manifest.txt"

playlist_bytes=0
for q in {rendition_list}; do
  playlist_bytes=$((playlist_bytes + $(stat -c '%s' "$OUT/$q/index.m3u8")))
done
master_bytes=$(stat -c '%s' "$OUT/master.m3u8")
segment_bytes=$(cat "$UPLOAD_DIR/segment_total_bytes")
echo $((segment_bytes + playlist_bytes + master_bytes)) > "$UPLOAD_DIR/total_bytes"

upload_batch() {{
  idx="$1"
  batch="$UPLOAD_DIR/batch_$idx.list"
  log="$UPLOAD_DIR/worker_$idx.log"
  : > "$log"
  if [ ! -s "$batch" ]; then
    echo "$idx" >> "$UPLOAD_DIR/done_workers"
    return 0
  fi
  attempt=1
  while [ "$attempt" -le {upload_retries} ]; do
    if rsync -a --whole-file --partial --partial-dir=.rsync-partial \\
        --files-from="$batch" -e "ssh $ORIGIN_SSH_OPTS" \\
        "$OUT/" "$ORIGIN_DEST/" >> "$log" 2>&1; then
      echo "$idx" >> "$UPLOAD_DIR/done_workers"
      return 0
    fi
    echo "batch $idx attempt $attempt failed" >> "$log"
    sleep_s=$((2 ** attempt)); [ "$sleep_s" -gt 10 ] && sleep_s=10
    sleep "$sleep_s"
    attempt=$((attempt + 1))
  done
  return 1
}}

echo "=== Pushing batched segments ($num_workers active workers) ==="
pids=()
for i in $(seq 0 $((num_workers - 1))); do
  upload_batch "$i" &
  pids[$i]=$!
  sleep {upload_worker_stagger}
done

upload_failed=0
for i in "${{!pids[@]}}"; do
  if ! wait "${{pids[$i]}}"; then
    echo "Upload batch $i failed after {upload_retries} attempts" >&2
    echo "--- worker_$i.log tail ---" >&2
    tail -n 100 "$UPLOAD_DIR/worker_$i.log" >&2 2>/dev/null || true
    upload_failed=1
  fi
done
if [ "$upload_failed" -ne 0 ]; then
  echo "Upload failed: one or more segment batches did not succeed after retries" >&2
  exit 30
fi

push_file() {{
  src="$1"
  dest_rel="$2"
  attempt=1
  while [ "$attempt" -le {upload_retries} ]; do
    if rsync -a --whole-file -e "ssh $ORIGIN_SSH_OPTS" "$src" "$ORIGIN_DEST/$dest_rel" >> "$UPLOAD_DIR/playlists.log" 2>&1; then
      return 0
    fi
    sleep_s=$((2 ** attempt)); [ "$sleep_s" -gt 10 ] && sleep_s=10
    sleep "$sleep_s"
    attempt=$((attempt + 1))
  done
  return 1
}}

echo "=== Pushing rendition playlists ==="
for q in {rendition_list}; do
  if ! push_file "$OUT/$q/index.m3u8" "$q/index.m3u8"; then
    echo "Failed to upload $q/index.m3u8" >&2
    tail -n 100 "$UPLOAD_DIR/playlists.log" >&2 2>/dev/null || true
    exit 31
  fi
done

echo "=== Pushing master playlist (as master.m3u8.tmp -- published atomically by origin) ==="
if ! push_file "$OUT/master.m3u8" "master.m3u8.tmp"; then
  echo "Failed to upload master.m3u8" >&2
  tail -n 100 "$UPLOAD_DIR/playlists.log" >&2 2>/dev/null || true
  exit 32
fi

echo "=== Upload complete ==="
"""


def build_job_script(
    source_url: str,
    expected_input_bytes: int | None,
    *,
    staging_remote_path: str,
    origin_ssh_user: str,
    origin_ssh_host: str,
    origin_ssh_port: int,
    upload_workers: int,
    upload_worker_stagger: float,
    upload_retries: int,
) -> str:
    quoted_url = shlex.quote(source_url)
    initial_required = int((expected_input_bytes or 0) * 1.05 + 2_147_483_648)
    rendition_dirs = ",".join(name for name, *_ in LADDER)
    rendition_list = " ".join(name for name, *_ in LADDER)
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail

DONE=/workspace/JOB_DONE
STATUS=/workspace/JOB_EXIT
STAGE=/workspace/JOB_STAGE
INPUT=/workspace/input/source.mp4
DURATION_FILE=/workspace/input/duration.txt
OUT=/workspace/out
rm -f "$DONE" "$STATUS"
mkdir -p /workspace/input "$OUT"/{{{rendition_dirs}}}

set_stage() {{
  tmp="$STAGE.tmp.$$"
  printf '%s\n' "$1" > "$tmp"
  mv -f "$tmp" "$STAGE"
  printf '[%s] STAGE: %s\n' "$(date -u +%FT%TZ)" "$1"
}}

# Encode-stage process handles, read by finish() below. Declared here (empty)
# so `set -u` doesn't choke if finish() runs before the encode stage ever
# starts (e.g. a download/preflight failure) -- the encode stage overwrites
# these with real PIDs once it actually starts ffmpeg/the progress relay.
ffmpeg_pid=""
relay_pid=""

# The one EXIT trap for the whole job, from here to the very end -- it must
# never be replaced or cleared by anything downstream (e.g. during the
# encode stage), or a successful run stops short of ever writing JOB_EXIT/
# JOB_DONE and the orchestrator polls a stage that never advances again.
finish() {{
  rc=$?
  trap - EXIT INT TERM
  # If we're exiting while the encode was still in flight (SIGINT/SIGTERM
  # during `wait "$ffmpeg_pid"` below), the encode stage's own explicit
  # cleanup after that wait never got to run -- do it here so a killed job
  # doesn't leave ffmpeg/the relay running or the fifo behind.
  if [ -n "$ffmpeg_pid" ]; then kill -TERM "$ffmpeg_pid" 2>/dev/null || true; fi
  if [ -n "$relay_pid" ]; then
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" 2>/dev/null || true
  fi
  rm -f "$OUT/progress.fifo"
  # Defense-in-depth: the deploy key (see the uploading stage below) is also
  # gone once the instance is destroyed shortly after this job exits either
  # way, but there's no reason to leave it sitting on disk in the meantime.
  rm -f /root/.ssh/vast_deploy_key
  printf '%s\n' "$rc" > "$STATUS.tmp.$$"
  mv -f "$STATUS.tmp.$$" "$STATUS"
  if [ "$rc" -eq 0 ]; then
    set_stage complete
  else
    set_stage failed
  fi
  touch "$DONE"
  exit "$rc"
}}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

set_stage download
echo "=== Download source ==="
initial_available="$(df -PB1 /workspace | awk 'NR==2 {{print $4}}')"
if [ "$initial_available" -lt {initial_required} ]; then
  echo "Insufficient disk before download: need at least {initial_required} bytes free, have $initial_available" >&2
  exit 11
fi
aria2c \\
  --allow-overwrite=true \\
  --auto-file-renaming=false \\
  --file-allocation=none \\
  --max-tries=8 \\
  --retry-wait=3 \\
  --timeout=30 \\
  --connect-timeout=20 \\
  --console-log-level=notice \\
  --summary-interval=2 \\
  -x 16 -s 16 -k 8M \\
  -d /workspace/input \\
  -o source.mp4 \\
  {quoted_url}

test -s "$INPUT"
duration="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$INPUT")"
awk -v d="$duration" 'BEGIN {{ exit !(d > 0) }}'
printf '%s\n' "$duration" > "$DURATION_FILE.tmp.$$"
mv -f "$DURATION_FILE.tmp.$$" "$DURATION_FILE"
echo "=== Source ==="
ffprobe -v error -show_entries format=duration,size -of default=nw=1 "$INPUT"

set_stage disk-check
available="$(df -PB1 /workspace | awk 'NR==2 {{print $4}}')"
required="$(awk -v d="$duration" 'BEGIN {{printf "%.0f", d * 13200000 / 8 * 1.15 + 2147483648}}')"
if [ "$available" -lt "$required" ]; then
  echo "Insufficient disk: need about $required bytes free, have $available" >&2
  exit 12
fi

set_stage gpu-check
echo "=== GPU and FFmpeg capabilities ==="
nvidia-smi
encoders_list="$(ffmpeg -hide_banner -encoders 2>/dev/null)"
grep -q 'h264_nvenc' <<< "$encoders_list"
filters_list="$(ffmpeg -hide_banner -filters 2>/dev/null)"
grep -q 'scale_cuda' <<< "$filters_list"
echo "=== Hardware preflight (NVDEC + split + scale_cuda + {len(LADDER)}x NVENC) ==="
ffmpeg -v warning -nostats -ss 0 -t 2 \\
  -hwaccel cuda -hwaccel_output_format cuda -i "$INPUT" \\
  -filter_complex "{_preflight_filter()}" \\
  {_preflight_maps()} \\
  -f null -

atomic_progress_relay() {{
  fifo="$1"
  target="$2"
  tmp="$target.tmp.$$"
  : > "$tmp"
  while IFS= read -r line; do
    printf '%s\n' "$line" >> "$tmp"
    case "$line" in
      progress=*)
        mv -f "$tmp" "$target"
        tmp="$target.tmp.$$"
        : > "$tmp"
        ;;
    esac
  done < "$fifo"
  rm -f "$tmp" "$fifo"
}}

set_stage encoding
fifo="$OUT/progress.fifo"
rm -f "$fifo"
mkfifo "$fifo"
atomic_progress_relay "$fifo" "$OUT/progress.txt" & relay_pid=$!

echo "=== Start ABR encode (single NVDEC decode, GPU-side split into {len(LADDER)} branches) ==="
ffmpeg -y -hide_banner -nostats -stats_period 1 \\
  -hwaccel cuda -hwaccel_output_format cuda -i "$INPUT" \\
  -filter_complex "{_split_filter()}" \\
  {_encode_outputs("$OUT", "$fifo")} \\
  > "$OUT/ffmpeg.log" 2>&1 &
ffmpeg_pid=$!
set +e
wait "$ffmpeg_pid"
rc=$?
# Terminate the relay rather than passively waiting on it: if ffmpeg died
# before ever opening the fifo for writing (e.g. a bad filter graph caught
# immediately), the relay is still blocked reading from it and would
# otherwise hang here forever instead of the job actually failing. finish()
# (the single EXIT trap for the whole job, still active here) does the same
# cleanup defensively if a signal interrupts the `wait` above instead.
kill -TERM "$relay_pid" 2>/dev/null || true
wait "$relay_pid" 2>/dev/null || true
set -e
rm -f "$fifo"

if [ "$rc" -ne 0 ]; then
  echo "ABR encode failed (rc=$rc)" >&2
  echo "--- ffmpeg.log tail ---"
  tail -n 200 "$OUT/ffmpeg.log" 2>/dev/null || true
  exit 20
fi

set_stage finalizing
cat > "$OUT/master.m3u8.tmp" <<'EOF'
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-INDEPENDENT-SEGMENTS

#EXT-X-STREAM-INF:BANDWIDTH=7350000,AVERAGE-BANDWIDTH=6660000,RESOLUTION=1920x1080
1080p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4000000,AVERAGE-BANDWIDTH=3650000,RESOLUTION=1280x720
720p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2150000,AVERAGE-BANDWIDTH=1930000,RESOLUTION=854x480
480p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1100000,AVERAGE-BANDWIDTH=996000,RESOLUTION=640x360
360p/index.m3u8
EOF

for q in {rendition_list}; do
  test -s "$OUT/$q/index.m3u8"
  grep -q '^#EXT-X-ENDLIST' "$OUT/$q/index.m3u8"
  find "$OUT/$q" -name 'segment_*.ts' -type f -size +0c -print -quit | grep -q .
done
mv -f "$OUT/master.m3u8.tmp" "$OUT/master.m3u8"
echo "=== Encoding complete ==="
du -sh "$OUT"
{_upload_stage_script(
    staging_remote_path=staging_remote_path,
    origin_ssh_user=origin_ssh_user,
    origin_ssh_host=origin_ssh_host,
    origin_ssh_port=origin_ssh_port,
    upload_workers=upload_workers,
    upload_worker_stagger=upload_worker_stagger,
    upload_retries=upload_retries,
    rendition_list=rendition_list,
)}"""
