"""Builds the remote Bash job: download source, preflight GPU, encode ABR HLS."""

from __future__ import annotations

import shlex


def build_job_script(
    source_url: str, expected_input_bytes: int | None = None
) -> str:
    quoted_url = shlex.quote(source_url)
    initial_required = int((expected_input_bytes or 0) * 1.05 + 2_147_483_648)
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail

DONE=/workspace/JOB_DONE
STATUS=/workspace/JOB_EXIT
STAGE=/workspace/JOB_STAGE
INPUT=/workspace/input/source.mp4
DURATION_FILE=/workspace/input/duration.txt
OUT=/workspace/out
CHILDREN=()
rm -f "$DONE" "$STATUS"
mkdir -p /workspace/input "$OUT"/{{1080p,720p,480p,360p}}

set_stage() {{
  tmp="$STAGE.tmp.$$"
  printf '%s\n' "$1" > "$tmp"
  mv -f "$tmp" "$STAGE"
  printf '[%s] STAGE: %s\n' "$(date -u +%FT%TZ)" "$1"
}}

stop_children() {{
  if [ "${{#CHILDREN[@]}}" -gt 0 ]; then
    kill -TERM "${{CHILDREN[@]}}" 2>/dev/null || true
    sleep 2
    kill -KILL "${{CHILDREN[@]}}" 2>/dev/null || true
    wait "${{CHILDREN[@]}}" 2>/dev/null || true
  fi
}}

finish() {{
  rc=$?
  trap - EXIT INT TERM
  stop_children
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
ffmpeg -hide_banner -encoders 2>/dev/null | grep -q 'h264_nvenc'
ffmpeg -hide_banner -filters 2>/dev/null | grep -q 'scale_cuda'
echo "=== Hardware preflight (NVDEC + scale_cuda + NVENC) ==="
ffmpeg -v warning -nostats \\
  -hwaccel cuda -hwaccel_output_format cuda -ss 0 -t 2 -i "$INPUT" \\
  -map 0:v:0 -an -vf scale_cuda=640:360 \\
  -c:v h264_nvenc -preset p5 -tune hq -f null -

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

encode_variant() {{
  name="$1"; size="$2"; bitrate="$3"; maxrate="$4"
  bufsize="$5"; audio="$6"; cq="$7"
  fifo="$OUT/$name/progress.fifo"
  rm -f "$fifo"
  mkfifo "$fifo"
  atomic_progress_relay "$fifo" "$OUT/$name/progress.txt" & relay_pid=$!
  ffmpeg_pid=""
  cleanup_variant() {{
    if [ -n "$ffmpeg_pid" ]; then kill -TERM "$ffmpeg_pid" 2>/dev/null || true; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" 2>/dev/null || true
    rm -f "$fifo"
  }}
  trap cleanup_variant EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  echo "=== Start $name ==="
  ffmpeg -y -hide_banner -nostats -stats_period 1 \\
    -hwaccel cuda -hwaccel_output_format cuda -i "$INPUT" \\
    -map 0:v:0 -map 0:a:0? -vf "scale_cuda=$size" \\
    -c:v h264_nvenc -preset p5 -tune hq -rc vbr -cq "$cq" \\
    -b:v "$bitrate" -maxrate "$maxrate" -bufsize "$bufsize" \\
    -force_key_frames 'expr:gte(t,n_forced*6)' -forced-idr 1 \\
    -c:a aac -b:a "$audio" -ac 2 \\
    -hls_time 6 -hls_playlist_type vod -hls_flags independent_segments \\
    -hls_segment_filename "$OUT/$name/segment_%05d.ts" \\
    -progress "$fifo" "$OUT/$name/index.m3u8" \\
    > "$OUT/$name/ffmpeg.log" 2>&1 &
  ffmpeg_pid=$!
  set +e
  wait "$ffmpeg_pid"
  rc=$?
  wait "$relay_pid" 2>/dev/null
  set -e
  trap - EXIT INT TERM
  rm -f "$fifo"
  return "$rc"
}}

set_stage encoding
encode_variant 1080p 1920:1080 6500k 7150k 13000k 160k 19 & CHILDREN+=("$!")
encode_variant 720p  1280:720  3500k 3850k 7000k  128k 20 & CHILDREN+=("$!")
encode_variant 480p  854:480   1800k 2000k 3600k  128k 21 & CHILDREN+=("$!")
encode_variant 360p  640:360   900k  1000k 1800k  96k  22 & CHILDREN+=("$!")

active=("${{CHILDREN[@]}}")
while [ "${{#active[@]}}" -gt 0 ]; do
  finished=""
  if wait -n -p finished "${{active[@]}}"; then rc=0; else rc=$?; fi
  next=()
  for pid in "${{active[@]}}"; do
    if [ "$pid" != "$finished" ]; then next+=("$pid"); fi
  done
  active=("${{next[@]}}")
  if [ "$rc" -ne 0 ]; then
    echo "A rendition failed (pid=$finished rc=$rc); cancelling the others" >&2
    CHILDREN=("${{active[@]}}")
    stop_children
    for q in 1080p 720p 480p 360p; do
      echo "--- $q log tail ---"
      tail -n 80 "$OUT/$q/ffmpeg.log" 2>/dev/null || true
    done
    exit 20
  fi
done
CHILDREN=()

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

for q in 1080p 720p 480p 360p; do
  test -s "$OUT/$q/index.m3u8"
  grep -q '^#EXT-X-ENDLIST' "$OUT/$q/index.m3u8"
  find "$OUT/$q" -name 'segment_*.ts' -type f -size +0c -print -quit | grep -q .
done
mv -f "$OUT/master.m3u8.tmp" "$OUT/master.m3u8"
echo "=== Encoding complete ==="
du -sh "$OUT"
"""
