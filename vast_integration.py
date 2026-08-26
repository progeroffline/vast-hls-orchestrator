#!/usr/bin/env python3
"""
Rent a cheap Vast.ai NVIDIA instance, transcode one source video to ABR HLS,
pull the result back to the Binary Racks origin over SSH/rsync, then destroy
the Vast.ai instance.

Designed to run ON the Binary Racks origin server, so origin credentials never
leave that server.

Requirements on Binary Racks:
  apt install -y python3 python3-requests python3-rich python3-loguru rsync openssh-client

Vast.ai requirements:
  - VAST_API_KEY exported on Binary Racks
  - the public key corresponding to --ssh-key must be added to Vast.ai account

Example:
  export VAST_API_KEY='...'
  python3 vast_integration_rich.py \
    --video-id test \
    --source-url https://origin.example.com/video/test.mp4 \
    --ssh-key /root/.ssh/vast_encoder
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import fcntl
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import requests
    from loguru import logger
    from rich.console import Console, Group
    from rich.live import Live
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError as exc:
    print(
        "Missing Python dependency: " + str(exc) + "\n"
        "Install with:\n"
        "  apt update && apt install -y python3-requests python3-rich python3-loguru rsync openssh-client",
        file=sys.stderr,
    )
    raise SystemExit(2)


API_BASE = "https://console.vast.ai/api/v0"
API_V1_BASE = "https://console.vast.ai/api/v1"
BAD_STATES = {"destroyed", "error", "exited", "failed", "offline", "unknown"}
DEFAULT_GPUS = ["RTX 3060", "RTX A2000", "RTX 4060"]
RENDITIONS = ["1080p", "720p", "480p", "360p"]

console = Console(stderr=True)


class VastError(RuntimeError):
    pass


class VastAuthError(VastError):
    pass


class OfferUnavailable(VastError):
    pass


class AmbiguousCreate(VastError):
    pass


class RichLogSink:
    LEVEL_STYLES = {
        "TRACE": "dim",
        "DEBUG": "dim cyan",
        "INFO": "cyan",
        "SUCCESS": "bold green",
        "WARNING": "yellow",
        "ERROR": "bold red",
        "CRITICAL": "bold white on red",
    }

    def __call__(self, message: Any) -> None:
        record = message.record
        style = self.LEVEL_STYLES.get(record["level"].name, "white")
        timestamp = record["time"].strftime("%H:%M:%S")
        level = record["level"].name.ljust(7)
        text = escape(record["message"])
        console.print(
            f"[dim]{timestamp}[/] [{style}]{level}[/] {text}",
            highlight=False,
        )
        if record.get("exception"):
            console.print(str(record["exception"]), style="red")


def configure_logging(verbose: bool = False) -> None:
    logger.remove()
    logger.add(RichLogSink(), level="DEBUG" if verbose else "INFO", enqueue=False)


@dataclass
class VariantProgress:
    name: str
    frame: int = 0
    fps: float = 0.0
    out_time_seconds: float = 0.0
    speed: float = 0.0
    bitrate: str = "-"
    progress: str = "waiting"


@dataclass
class RemoteSnapshot:
    stage: str = "starting"
    status: str = "RUNNING"
    downloaded_bytes: int = 0
    duration_seconds: float = 0.0
    gpu_util: float | None = None
    enc_util: float | None = None
    dec_util: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    power_w: float | None = None
    variants: dict[str, VariantProgress] = field(
        default_factory=lambda: {q: VariantProgress(q) for q in RENDITIONS}
    )
    remote_log_tail: str = ""


@dataclass
class DashboardContext:
    instance_id: int
    gpu_name: str
    hourly_price: float
    host: str
    port: int
    expected_input_bytes: int | None
    started_at: float


class VastClient:
    def __init__(self, api_key: str):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict[str, str] | None = None,
        timeout: int = 30,
        allow_404: bool = False,
        retry: bool = True,
        retry_429: bool = False,
        api_base: str = API_BASE,
    ) -> Any:
        url = f"{api_base}{path}"
        last_error = None
        attempts = 6 if retry or retry_429 else 1
        for attempt in range(attempts):
            try:
                logger.debug("Vast API {} {} (attempt {})", method, path, attempt + 1)
                r = self.s.request(
                    method, url, json=body, params=params, timeout=timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                if not retry:
                    raise VastError(
                        f"Ambiguous API result for {method} {path}: {exc}"
                    ) from exc
                delay = min(2**attempt, 20)
                logger.warning(
                    "Vast API transport error: {}. Retrying in {}s", exc, delay
                )
                time.sleep(delay)
                continue

            if allow_404 and r.status_code == 404:
                return None

            if r.status_code in {401, 403}:
                raise VastAuthError(
                    f"Vast API rejected credentials/permissions (HTTP {r.status_code}): "
                    f"{r.text[:500]}"
                )

            if r.status_code == 429 or 500 <= r.status_code < 600:
                last_error = VastError(f"HTTP {r.status_code}: {r.text[:500]}")
                can_retry_status = retry or (r.status_code == 429 and retry_429)
                if not can_retry_status:
                    raise last_error
                retry_after = r.headers.get("Retry-After", "")
                try:
                    delay = min(max(float(retry_after), 0.0), 60.0)
                except ValueError:
                    delay = min(2**attempt + random.random(), 20)
                logger.warning(
                    "Vast API HTTP {}. Retrying in {}s", r.status_code, delay
                )
                time.sleep(delay)
                continue

            if not r.ok:
                raise VastError(
                    f"{method} {url}: HTTP {r.status_code}: {r.text[:1000]}"
                )

            if not r.text.strip():
                return {}
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text}

        raise VastError(f"API request failed after {attempts} attempt(s): {last_error}")

    def search_offers(self, gpu_name: str, args: argparse.Namespace) -> list[dict]:
        payload = {
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "gpu_name": {"eq": gpu_name},
            "num_gpus": {"eq": 1},
            "reliability": {"gte": args.min_reliability},
            "cpu_cores_effective": {"gte": args.min_cpu},
            "cpu_ram": {"gte": args.min_ram_mb},
            "disk_space": {"gte": args.disk_gb},
            "disk_bw": {"gte": args.min_disk_bw},
            "direct_port_count": {"gte": 1},
            "cuda_max_good": {"gte": 12.6},
            "dph_total": {"lte": args.max_hourly},
            "duration": {"gte": args.boot_timeout + args.job_timeout},
            "order": [["dph_total", "asc"]],
            "type": "on-demand",
            "limit": 20,
        }
        data = self.request("POST", "/bundles/", body=payload)

        offers = data.get("offers", []) if isinstance(data, dict) else []
        if isinstance(offers, dict):
            offers = [offers]
        return [x for x in offers if isinstance(x, dict)]

    def create_instance(self, offer_id: int, body: dict) -> dict:
        # Never blindly retry this non-idempotent operation. A timed-out response may
        # still have created a billable contract.
        try:
            data = self.request(
                "PUT",
                f"/asks/{offer_id}/",
                body=body,
                retry=False,
                retry_429=True,
            )
        except VastError as exc:
            message = str(exc).lower()
            if any(
                marker in message
                for marker in (
                    "http 404",
                    "http 410",
                    "unavailable",
                    "not available",
                    "no_compatible_tag",
                    "already rented",
                )
            ):
                raise OfferUnavailable(str(exc)) from exc
            if "ambiguous api result" in message or re.search(
                r"http 5\d\d", message
            ):
                raise AmbiguousCreate(str(exc)) from exc
            raise
        if not isinstance(data, dict):
            raise VastError(f"Unexpected create response: {data!r}")
        if not data.get("success") or not data.get("new_contract"):
            message = json.dumps(data, ensure_ascii=False)
            lowered = message.lower()
            if any(
                marker in lowered
                for marker in ("unavailable", "no_compatible_tag", "already rented")
            ):
                raise OfferUnavailable(message)
            raise VastError(f"Create failed: {message}")
        return data

    def show_instance(self, instance_id: int) -> dict | None:
        data = self.request("GET", f"/instances/{instance_id}/", allow_404=True)
        if data is None:
            return None
        if isinstance(data, dict) and "instances" in data:
            instance = data["instances"]
            if isinstance(instance, list):
                return instance[0] if instance else None
            return instance if isinstance(instance, dict) else None
        return data if isinstance(data, dict) else None

    def instances_with_label(self, label: str) -> list[dict]:
        data = self.request(
            "GET",
            "/instances",
            params={
                "limit": "25",
                "select_cols": json.dumps(["id", "label", "actual_status"]),
                "select_filters": json.dumps({"label": {"eq": label}}),
            },
            api_base=API_V1_BASE,
        )
        items = data.get("instances", []) if isinstance(data, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def destroy_instance(self, instance_id: int) -> None:
        data = self.request("DELETE", f"/instances/{instance_id}/", allow_404=True)
        if data is None:
            logger.info("Vast instance {} is already gone", instance_id)
            return
        if isinstance(data, dict) and data.get("success") is False:
            raise VastError(f"Destroy failed for instance {instance_id}: {data}")
        logger.success("Vast instance {} destroyed", instance_id)


def source_size_bytes(url: str) -> int | None:
    try:
        r = requests.head(url, allow_redirects=True, timeout=20)
        r.raise_for_status()
        n = int(r.headers.get("Content-Length", "0"))
        return n if n > 0 else None
    except Exception as exc:
        logger.debug("Could not determine source size with HEAD: {}", exc)
        return None


def source_size_gb(url: str) -> float:
    n = source_size_bytes(url)
    return n / 1_000_000_000 if n else 10.0


def offer_estimated_cost(offer: dict, input_gb: float, expected_hours: float) -> float:
    hourly = float(offer.get("dph_total") or 999)
    down_cost = float(offer.get("inet_down_cost") or 0)
    up_cost = float(offer.get("inet_up_cost") or 0)
    output_gb = input_gb * 1.7
    return hourly * expected_hours + input_gb * down_cost + output_gb * up_cost


def choose_offers(
    client: VastClient, args: argparse.Namespace, input_gb: float
) -> list[dict]:
    offers: list[dict] = []
    search_errors: list[Exception] = []
    with console.status("[bold cyan]Searching Vast.ai offers...", spinner="dots"):
        for gpu in args.gpus:
            try:
                found = client.search_offers(gpu, args)
                logger.info("{}: found {} matching offers", gpu, len(found))
                offers.extend(found)
            except VastAuthError:
                raise
            except Exception as exc:
                search_errors.append(exc)
                logger.warning("Search failed for {}: {}", gpu, exc)

    dedup: dict[int, dict] = {}
    for offer in offers:
        try:
            dedup[int(offer["id"])] = offer
        except Exception:
            continue

    offers = list(dedup.values())
    offers.sort(
        key=lambda o: (
            offer_estimated_cost(o, input_gb, args.expected_hours),
            float(o.get("dph_total") or 999),
            -float(o.get("inet_down") or 0),
            -float(o.get("disk_bw") or 0),
        )
    )

    if not offers:
        if len(search_errors) == len(args.gpus):
            raise VastError(f"All offer searches failed: {search_errors[-1]}")
        raise VastError(
            "No offers matched the configured price/reliability/disk filters"
        )

    table = Table(
        title="Top Vast.ai candidates", show_lines=False, header_style="bold cyan"
    )
    table.add_column("Offer", justify="right")
    table.add_column("GPU")
    table.add_column("$/h", justify="right")
    table.add_column("Reliability", justify="right")
    table.add_column("Down Mbps", justify="right")
    table.add_column("Up Mbps", justify="right")
    table.add_column("Disk MB/s", justify="right")
    table.add_column("Est. job", justify="right")

    for offer in offers[:5]:
        est = offer_estimated_cost(offer, input_gb, args.expected_hours)
        table.add_row(
            str(offer.get("id", "-")),
            str(offer.get("gpu_name", "-")),
            f"{float(offer.get('dph_total') or 0):.4f}",
            f"{float(offer.get('reliability') or 0):.4f}",
            f"{float(offer.get('inet_down') or 0):.0f}",
            f"{float(offer.get('inet_up') or 0):.0f}",
            f"{float(offer.get('disk_bw') or 0):.0f}",
            f"${est:.4f}",
        )
    console.print(table)
    return offers


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


def build_onstart(job_script: str, failsafe_seconds: int) -> str:
    payload = base64.b64encode(job_script.encode()).decode()
    bootstrap = f"""#!/usr/bin/env bash
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
mkdir -p /workspace
rm -f /workspace/JOB_DONE /workspace/JOB_EXIT
printf 'bootstrap\n' > /workspace/JOB_STAGE
exec > >(tee -a /workspace/bootstrap.log) 2>&1

bootstrap_failed() {{
  rc=$?
  if [ "$rc" -ne 0 ] && [ ! -e /workspace/JOB_DONE ]; then
    printf '%s\n' "$rc" > /workspace/JOB_EXIT
    printf 'bootstrap-failed\n' > /workspace/JOB_STAGE
    touch /workspace/JOB_DONE
  fi
}}
trap bootstrap_failed EXIT

# Start the failsafe before package installation, which itself can hang or fail.
nohup bash -lc 'sleep {int(failsafe_seconds)}; if [ -z "${{CONTAINER_API_KEY:-}}" ] || [ -z "${{CONTAINER_ID:-}}" ]; then echo "Vast watchdog credentials are unavailable"; exit 1; fi; while true; do if command -v curl >/dev/null && curl -fsS --retry 5 --retry-all-errors -X DELETE -H "Authorization: Bearer $CONTAINER_API_KEY" "https://console.vast.ai/api/v0/instances/$CONTAINER_ID/"; then exit 0; fi; sleep 60; done' > /workspace/watchdog.log 2>&1 &

echo "=== Bootstrap started $(date -u +%FT%TZ) ==="
apt-get update -qq
apt-get install -y --no-install-recommends ffmpeg aria2 curl ca-certificates rsync
printf '%s' {payload!r} | base64 -d > /workspace/encode-job.sh
chmod 700 /workspace/encode-job.sh
echo "=== Starting encode job ==="
nohup /workspace/encode-job.sh > /workspace/job.log 2>&1 &
trap - EXIT
"""
    # Vast may initially evaluate onstart through /bin/sh. Keep that outer command
    # POSIX-compatible and explicitly feed the real script to bash.
    bootstrap_payload = base64.b64encode(bootstrap.encode()).decode()
    return f"printf '%s' {shlex.quote(bootstrap_payload)} | base64 -d | /bin/bash"


def ssh_base(args: argparse.Namespace, host: str, port: int) -> list[str]:
    return [
        "ssh",
        "-i",
        str(args.ssh_key),
        "-p",
        str(port),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={args.known_hosts}",
        f"root@{host}",
    ]


def ssh_run(
    args: argparse.Namespace,
    host: str,
    port: int,
    command: str,
    *,
    timeout: int = 30,
    check: bool = False,
) -> subprocess.CompletedProcess:
    logger.debug("SSH {}:{}: {}", host, port, command[:180])
    return subprocess.run(
        ssh_base(args, host, port) + [command],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=check,
    )


def wait_for_running(client: VastClient, instance_id: int, timeout_s: int) -> dict:
    deadline = time.time() + timeout_s
    last = None
    with console.status(
        f"[bold cyan]Provisioning Vast instance {instance_id}...", spinner="dots"
    ) as status:
        while time.time() < deadline:
            info = client.show_instance(instance_id)
            if info is None:
                raise VastError("Instance disappeared while provisioning")
            state = info.get("actual_status")
            msg = info.get("status_msg") or ""
            status.update(
                f"[bold cyan]Instance {instance_id}: {state}[/] [dim]{escape(str(msg))}[/]"
            )
            if state != last:
                logger.info("Instance state: {}{}", state, f"; {msg}" if msg else "")
                last = state
            if state == "running" and info.get("ssh_host") and info.get("ssh_port"):
                return info
            if state in BAD_STATES:
                raise VastError(f"Instance entered terminal/bad state: {state}")
            time.sleep(5)
    raise VastError("Timed out waiting for Vast instance to become running")


def wait_for_ssh(
    args: argparse.Namespace, host: str, port: int, timeout_s: int = 300
) -> None:
    deadline = time.time() + timeout_s
    with console.status(
        f"[bold cyan]Waiting for SSH on {host}:{port}...", spinner="dots"
    ):
        while time.time() < deadline:
            try:
                result = ssh_run(args, host, port, "echo SSH_OK", timeout=15)
                if result.returncode == 0 and "SSH_OK" in result.stdout:
                    logger.success("SSH is ready")
                    return
            except Exception:
                pass
            time.sleep(3)
    raise VastError("SSH did not become ready")


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
{{ cat /workspace/bootstrap.log 2>/dev/null; cat /workspace/job.log 2>/dev/null; }} | tr '\r' '\n' | tail -n 8 || true
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


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "--:--:--"
    seconds_i = int(seconds)
    h, rem = divmod(seconds_i, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_bytes(n: int | None) -> str:
    if not n or n <= 0:
        return "0 B"
    value = float(n)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def bar(percent: float, width: int = 22) -> Text:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100.0))
    text = Text()
    text.append("█" * filled, style="green")
    text.append("░" * (width - filled), style="grey37")
    return text


def render_dashboard(ctx: DashboardContext, snap: RemoteSnapshot) -> Group:
    elapsed = max(0.0, time.time() - ctx.started_at)

    summary = Table.grid(expand=True)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_row(
        f"[bold]Instance[/]: {ctx.instance_id}  [bold]GPU[/]: {escape(ctx.gpu_name)}",
        f"[bold]Price[/]: ${ctx.hourly_price:.4f}/h  [bold]Elapsed[/]: {format_duration(elapsed)}",
    )
    summary.add_row(
        f"[bold]SSH[/]: {escape(ctx.host)}:{ctx.port}",
        f"[bold]Stage[/]: [cyan]{escape(snap.stage)}[/]  [bold]Status[/]: {escape(snap.status)}",
    )

    if ctx.expected_input_bytes:
        download_pct = snap.downloaded_bytes * 100.0 / ctx.expected_input_bytes
        download_text = Text.assemble(
            ("Download  ", "bold"),
            bar(download_pct),
            (
                f" {download_pct:5.1f}%  {format_bytes(snap.downloaded_bytes)} / {format_bytes(ctx.expected_input_bytes)}",
                "",
            ),
        )
    else:
        download_text = Text(f"Download: {format_bytes(snap.downloaded_bytes)}")

    gpu = Table.grid(expand=True)
    gpu.add_column()
    gpu.add_column()
    gpu.add_column()
    gpu.add_column()
    gpu.add_row(
        f"GPU: {snap.gpu_util:.0f}%" if snap.gpu_util is not None else "GPU: -",
        f"NVENC: {snap.enc_util:.0f}%" if snap.enc_util is not None else "NVENC: -",
        f"NVDEC: {snap.dec_util:.0f}%" if snap.dec_util is not None else "NVDEC: -",
        (
            f"VRAM: {snap.memory_used_mb:.0f}/{snap.memory_total_mb:.0f} MiB"
            if snap.memory_used_mb is not None and snap.memory_total_mb is not None
            else "VRAM: -"
        ),
    )

    progress_table = Table(expand=True, header_style="bold cyan")
    progress_table.add_column("Quality", no_wrap=True)
    progress_table.add_column("Progress", ratio=2)
    progress_table.add_column("Media time", justify="right")
    progress_table.add_column("FPS", justify="right")
    progress_table.add_column("Speed", justify="right")
    progress_table.add_column("ETA", justify="right")
    progress_table.add_column("State", justify="right")

    duration = snap.duration_seconds
    for q in RENDITIONS:
        vp = snap.variants[q]
        pct = (vp.out_time_seconds / duration * 100.0) if duration > 0 else 0.0
        if vp.progress == "end":
            pct = 100.0
        pct = max(0.0, min(100.0, pct))
        remaining_media = (
            max(0.0, duration - vp.out_time_seconds) if duration > 0 else 0.0
        )
        eta = remaining_media / vp.speed if vp.speed > 0 else None
        state_style = (
            "green"
            if vp.progress == "end"
            else ("cyan" if vp.out_time_seconds > 0 else "dim")
        )
        progress_table.add_row(
            q,
            Text.assemble(bar(pct), f" {pct:5.1f}%"),
            f"{format_duration(vp.out_time_seconds)} / {format_duration(duration)}",
            f"{vp.fps:.1f}" if vp.fps else "-",
            f"{vp.speed:.2f}x" if vp.speed else "-",
            format_duration(eta),
            Text(vp.progress, style=state_style),
        )

    remote_text = Text(
        snap.remote_log_tail or "Waiting for remote log output...", style="dim"
    )

    return Group(
        Panel(summary, title="Vast.ai encoder", border_style="cyan"),
        Panel(Group(download_text, gpu), title="Resources", border_style="blue"),
        Panel(progress_table, title="ABR encode", border_style="green"),
        Panel(remote_text, title="Remote log tail", border_style="magenta"),
    )


def wait_for_job(
    args: argparse.Namespace,
    client: VastClient,
    instance_id: int,
    host: str,
    port: int,
    *,
    gpu_name: str,
    hourly_price: float,
    expected_input_bytes: int | None,
) -> tuple[str, int]:
    deadline = time.time() + args.job_timeout
    ctx = DashboardContext(
        instance_id=instance_id,
        gpu_name=gpu_name,
        hourly_price=hourly_price,
        host=host,
        port=port,
        expected_input_bytes=expected_input_bytes,
        started_at=time.time(),
    )
    last_stage: str | None = None
    last_status: str | None = None
    last_log_tail = ""
    ssh_failure_started: float | None = None

    placeholder = RemoteSnapshot(stage="connecting")
    with Live(
        render_dashboard(ctx, placeholder),
        console=console,
        refresh_per_second=4,
        transient=False,
        vertical_overflow="visible",
    ) as live:
        while time.time() < deadline:
            info = client.show_instance(instance_id)
            if info is None:
                raise VastError("Vast instance disappeared before result transfer")
            state = info.get("actual_status")
            if state in BAD_STATES:
                raise VastError(f"Instance entered bad state during encoding: {state}")

            new_host = str(info.get("ssh_host") or host)
            try:
                new_port = int(info.get("ssh_port") or port)
            except (TypeError, ValueError):
                new_port = port
            if (new_host, new_port) != (host, port):
                logger.warning(
                    "Vast changed SSH endpoint from {}:{} to {}:{}",
                    host,
                    port,
                    new_host,
                    new_port,
                )
                host, port = new_host, new_port
                ctx.host, ctx.port = host, port

            try:
                snap = fetch_remote_snapshot(args, host, port)
                if ssh_failure_started is not None:
                    logger.success("SSH monitoring connection recovered")
                ssh_failure_started = None
            except (subprocess.TimeoutExpired, VastError, OSError) as exc:
                now = time.monotonic()
                if ssh_failure_started is None:
                    ssh_failure_started = now
                disconnected_for = now - ssh_failure_started
                logger.warning(
                    "Remote monitoring unavailable for {:.0f}s: {}",
                    disconnected_for,
                    exc,
                )
                if disconnected_for >= args.ssh_reconnect_timeout:
                    raise VastError(
                        f"SSH did not recover within {args.ssh_reconnect_timeout}s"
                    ) from exc
                time.sleep(args.monitor_interval)
                continue

            live.update(render_dashboard(ctx, snap), refresh=True)

            if snap.stage != last_stage:
                logger.info("Remote stage -> {}", snap.stage)
                last_stage = snap.stage
            if snap.status != last_status:
                logger.info("Remote status -> {}", snap.status)
                last_status = snap.status

            # Print meaningful remote log changes above the dashboard, while the dashboard
            # itself always shows the latest tail.
            if snap.remote_log_tail and snap.remote_log_tail != last_log_tail:
                new_lines = [
                    line for line in snap.remote_log_tail.splitlines() if line.strip()
                ]
                if new_lines:
                    logger.debug("Remote log: {}", " | ".join(new_lines[-3:]))
                last_log_tail = snap.remote_log_tail

            if snap.status.startswith("DONE:"):
                rc_text = snap.status.split(":", 1)[1].strip()
                rc = int(rc_text or "1")
                if rc != 0:
                    logs = ssh_run(
                        args,
                        host,
                        port,
                        "echo '=== job.log ==='; tail -n 200 /workspace/job.log || true; "
                        'for q in 1080p 720p 480p 360p; do echo "=== $q ==="; tail -n 100 /workspace/out/$q/ffmpeg.log 2>/dev/null || true; done',
                        timeout=40,
                    )
                    raise VastError(
                        f"Encoder job failed with exit code {rc}\n{logs.stdout}"
                    )
                live.update(render_dashboard(ctx, snap), refresh=True)
                logger.success("Encoder job completed successfully")
                return host, port

            time.sleep(args.monitor_interval)

    raise VastError("Encoding job timeout exceeded")


def stream_process(cmd: list[str], *, label: str) -> None:
    logger.info("{}", label)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    assert proc.stdout is not None
    last_line = ""
    pending = b""
    try:
        with Live(
            Panel("Waiting for rsync...", title="Transfer", border_style="cyan"),
            console=console,
            refresh_per_second=4,
            transient=False,
        ) as live:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                pending += chunk
                parts = re.split(br"[\r\n]+", pending)
                pending = parts.pop() if parts else b""
                for raw in parts:
                    line = raw.decode("utf-8", "replace").strip()
                    if line and line != last_line:
                        live.update(
                            Panel(
                                Text(line, overflow="ellipsis"),
                                title="rsync live progress",
                                border_style="cyan",
                            ),
                            refresh=True,
                        )
                        last_line = line
            if pending.strip():
                last_line = pending.decode("utf-8", "replace").strip()
                live.update(Panel(Text(last_line), title="rsync result"), refresh=True)
        rc = proc.wait()
    except BaseException:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    if rc != 0:
        raise VastError(f"rsync failed with exit code {rc}: {last_line}")


def atomic_exchange_dirs(left: Path, right: Path) -> bool:
    """Atomically exchange two paths on Linux; return False if unsupported."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    rc = renameat2(
        -100,
        os.fsencode(left),
        -100,
        os.fsencode(right),
        2,  # RENAME_EXCHANGE
    )
    if rc == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        return False
    raise OSError(error, os.strerror(error), f"{left} <-> {right}")


def rsync_results(
    args: argparse.Namespace, host: str, port: int, instance_id: int
) -> Path:
    dest = args.origin_root / args.video_id / "abr"
    staging = args.origin_root / args.video_id / f"abr.staging.{instance_id}"
    backup = args.origin_root / args.video_id / f"abr.backup.{instance_id}"

    if staging.is_symlink():
        raise VastError(f"Refusing unsafe symlink staging path: {staging}")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    ssh_cmd = " ".join(
        [
            "ssh",
            "-i",
            shlex.quote(str(args.ssh_key)),
            "-p",
            str(port),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={shlex.quote(str(args.known_hosts))}",
        ]
    )
    cmd = [
        "rsync",
        "-a",
        "--partial",
        "--partial-dir=.rsync-partial",
        "--info=progress2",
        "-e",
        ssh_cmd,
        f"root@{host}:/workspace/out/",
        str(staging) + "/",
    ]
    last_error: Exception | None = None
    for attempt in range(1, args.rsync_retries + 1):
        try:
            stream_process(
                cmd,
                label=(
                    "Pulling encoded HLS from Vast.ai to Binary Racks... "
                    f"(attempt {attempt}/{args.rsync_retries})"
                ),
            )
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            if attempt < args.rsync_retries:
                logger.warning("rsync interrupted: {}; resuming shortly", exc)
                time.sleep(min(2**attempt, 10))
    if last_error is not None:
        raise last_error

    required = [
        staging / "master.m3u8",
        staging / "1080p" / "index.m3u8",
        staging / "720p" / "index.m3u8",
        staging / "480p" / "index.m3u8",
        staging / "360p" / "index.m3u8",
    ]
    for path in required:
        if not path.is_file() or path.stat().st_size == 0:
            raise VastError(f"Missing/empty result file: {path}")
    for q in RENDITIONS:
        if not any(p.is_file() and p.stat().st_size > 0 for p in (staging / q).glob("segment_*.ts")):
            raise VastError(f"No HLS segments found for {q}")

    for path in staging.glob("*/ffmpeg.log"):
        try:
            path.unlink()
        except OSError:
            pass
    for path in staging.glob("*/progress.txt"):
        try:
            path.unlink()
        except OSError:
            pass

    if dest.is_symlink() or backup.is_symlink():
        raise VastError("Refusing to publish through a symlinked abr/backup path")
    if staging.stat().st_dev != staging.parent.stat().st_dev:
        raise VastError("Staging and publish destination are not on the same filesystem")
    shutil.rmtree(backup, ignore_errors=True)
    try:
        if dest.exists() and atomic_exchange_dirs(staging, dest):
            # After the exchange, staging contains the old published tree.
            staging.rename(backup)
        else:
            if dest.exists():
                logger.warning(
                    "renameat2(RENAME_EXCHANGE) unavailable; using two-rename publish"
                )
                dest.rename(backup)
            staging.rename(dest)
    except Exception:
        if not dest.exists() and backup.exists():
            backup.rename(dest)
        raise
    else:
        shutil.rmtree(backup, ignore_errors=True)
        for stale_backup in dest.parent.glob("abr.backup.*"):
            if stale_backup.is_symlink():
                logger.warning("Leaving unsafe symlinked stale backup: {}", stale_backup)
                continue
            shutil.rmtree(stale_backup, ignore_errors=True)

    logger.success("Published ABR HLS atomically at {}", dest)
    return dest


def validate_inputs(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.video_id):
        raise VastError(
            "Invalid --video-id: use 1-128 ASCII letters, digits, dot, underscore or dash"
        )
    if args.video_id in {".", ".."}:
        raise VastError("Invalid --video-id")
    parsed = urlsplit(args.source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VastError("--source-url must be an absolute HTTP(S) URL")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in args.source_url):
        raise VastError("--source-url contains control characters")
    for name in (
        "disk_gb",
        "boot_timeout",
        "job_timeout",
        "failsafe_seconds",
        "ssh_reconnect_timeout",
        "rsync_retries",
    ):
        if getattr(args, name) <= 0:
            raise VastError(f"--{name.replace('_', '-')} must be positive")
    if args.monitor_interval <= 0 or args.max_hourly <= 0:
        raise VastError("--monitor-interval and --max-hourly must be positive")
    if args.failsafe_seconds <= args.job_timeout:
        logger.warning(
            "Failsafe timeout ({}) is not greater than job timeout ({}); "
            "the instance may self-destruct during diagnostics/transfer",
            args.failsafe_seconds,
            args.job_timeout,
        )


def acquire_video_lock(args: argparse.Namespace) -> Any:
    root = args.origin_root.resolve()
    video_dir = args.origin_root / args.video_id
    video_dir.mkdir(parents=True, exist_ok=True)
    if video_dir.resolve().parent != root:
        raise VastError(f"Video directory escapes --origin-root: {video_dir}")
    lock_path = video_dir / ".vast-hls.lock"
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.seek(0)
        owner = lock_file.read().strip() or "unknown process"
        lock_file.close()
        raise VastError(
            f"Another HLS job already holds the lock for {args.video_id} ({owner})"
        ) from exc
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"pid={os.getpid()} started={int(time.time())}\n")
    lock_file.flush()
    return lock_file


def recover_local_publish_state(args: argparse.Namespace) -> None:
    video_dir = args.origin_root / args.video_id
    dest = video_dir / "abr"
    backups = sorted(
        video_dir.glob("abr.backup.*"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    if not dest.exists() and backups:
        candidate = backups.pop(0)
        if candidate.is_symlink():
            raise VastError(f"Refusing symlinked publish backup: {candidate}")
        candidate.rename(dest)
        logger.warning("Recovered interrupted previous publish from {}", candidate)
    for path in backups:
        if path.is_symlink():
            raise VastError(f"Refusing symlinked publish backup: {path}")
        shutil.rmtree(path, ignore_errors=True)
    for path in video_dir.glob("abr.staging.*"):
        if path.is_symlink():
            raise VastError(f"Refusing symlinked stale staging path: {path}")
        shutil.rmtree(path, ignore_errors=True)


def recover_created_instance(
    client: VastClient, label: str, timeout_s: int = 45
) -> int | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            matches = client.instances_with_label(label)
            if matches:
                instance_id = int(matches[0]["id"])
                logger.warning(
                    "Recovered instance {} by unique label after ambiguous create response",
                    instance_id,
                )
                return instance_id
        except VastAuthError:
            raise
        except Exception as exc:
            logger.warning("Could not reconcile ambiguous create yet: {}", exc)
        time.sleep(5)
    return None


def collect_remote_diagnostics(
    args: argparse.Namespace, host: str, port: int
) -> None:
    command = (
        "echo '=== bootstrap.log ==='; tail -n 120 /workspace/bootstrap.log 2>/dev/null || true; "
        "echo '=== job.log ==='; tail -n 200 /workspace/job.log 2>/dev/null || true; "
        'for q in 1080p 720p 480p 360p; do echo "=== $q ffmpeg.log ==="; '
        'tail -n 120 "/workspace/out/$q/ffmpeg.log" 2>/dev/null || true; done'
    )
    try:
        result = ssh_run(args, host, port, command, timeout=25)
        output = result.stdout.strip()
        if output:
            console.print(Panel(Text(output), title="Remote failure diagnostics", border_style="red"))
        elif result.stderr.strip():
            logger.warning("Could not retrieve remote diagnostics: {}", result.stderr.strip())
    except Exception as exc:
        logger.warning("Could not retrieve remote diagnostics: {}", exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Encode one video on a temporary Vast.ai GPU"
    )
    parser.add_argument("--video-id", required=True, help="e.g. test")
    parser.add_argument(
        "--source-url", required=True, help="Public/readable source MP4 URL"
    )
    parser.add_argument("--origin-root", type=Path, default=Path("/var/www/html/video"))
    parser.add_argument("--ssh-key", type=Path, default=Path("/root/.ssh/vast_encoder"))
    parser.add_argument(
        "--known-hosts", type=Path, default=Path("/root/.ssh/vast_known_hosts")
    )
    parser.add_argument(
        "--image",
        default=os.getenv("VAST_IMAGE", "nvidia/cuda:12.6.3-runtime-ubuntu24.04"),
    )
    parser.add_argument("--disk-gb", type=int, default=40)
    parser.add_argument("--max-hourly", type=float, default=0.08)
    parser.add_argument("--min-reliability", type=float, default=0.98)
    parser.add_argument("--min-cpu", type=int, default=4)
    parser.add_argument("--min-ram-mb", type=int, default=8192)
    parser.add_argument("--min-disk-bw", type=float, default=200.0)
    parser.add_argument(
        "--expected-hours",
        type=float,
        default=0.5,
        help="Only used to rank offers by estimated total cost",
    )
    parser.add_argument("--boot-timeout", type=int, default=600)
    parser.add_argument("--job-timeout", type=int, default=3 * 3600)
    parser.add_argument(
        "--failsafe-seconds",
        type=int,
        default=4 * 3600,
        help="Vast instance self-destruct watchdog",
    )
    parser.add_argument(
        "--monitor-interval",
        type=float,
        default=2.0,
        help="Seconds between Rich dashboard refresh queries",
    )
    parser.add_argument(
        "--ssh-reconnect-timeout",
        type=int,
        default=180,
        help="How long to tolerate/retry a broken SSH monitoring connection",
    )
    parser.add_argument(
        "--rsync-retries",
        type=int,
        default=4,
        help="Resumable rsync transfer attempts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and rank offers, but do not rent an instance",
    )
    parser.add_argument("--verbose", action="store_true", help="Show DEBUG log events")
    parser.add_argument("--gpus", nargs="+", default=DEFAULT_GPUS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    try:
        validate_inputs(args)
    except Exception as exc:
        logger.error("{}", exc)
        return 2

    api_key = os.getenv("VAST_API_KEY")
    if not api_key:
        logger.error("export VAST_API_KEY first")
        return 2
    if not args.dry_run and not args.ssh_key.is_file():
        logger.error("SSH private key not found: {}", args.ssh_key)
        return 2

    client = VastClient(api_key)
    input_bytes = source_size_bytes(args.source_url)
    input_gb = input_bytes / 1_000_000_000 if input_bytes else 10.0
    logger.info(
        "Source size estimate: {}",
        f"{input_gb:.2f} GB"
        if input_bytes
        else "unknown (using 10 GB for offer ranking)",
    )

    instance_id: int | None = None
    selected_offer: dict | None = None
    remote_host: str | None = None
    remote_port: int | None = None
    lock_file: Any | None = None
    create_label: str | None = None
    create_was_ambiguous = False
    published = False
    original_known_hosts = args.known_hosts
    try:
        offers = choose_offers(client, args, input_gb)
        if args.dry_run:
            logger.success("Dry-run complete: {} matching offers; nothing rented", len(offers))
            return 0

        lock_file = acquire_video_lock(args)
        recover_local_publish_state(args)
        args.known_hosts = original_known_hosts.with_name(
            f"{original_known_hosts.name}.{os.getpid()}"
        )
        args.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        args.known_hosts.touch(mode=0o600, exist_ok=True)
        args.known_hosts.chmod(0o600)

        job_script = build_job_script(args.source_url, input_bytes)
        onstart = build_onstart(job_script, args.failsafe_seconds)
        create_label = f"hls-{args.video_id}-{int(time.time())}-{os.getpid()}"

        last_error = None
        for offer in offers[:10]:
            offer_id = int(offer["id"])
            body = {
                "image": args.image,
                "disk": args.disk_gb,
                "label": create_label,
                "runtype": "ssh_direct",
                "target_state": "running",
                "env": {"NVIDIA_DRIVER_CAPABILITIES": "compute,video,utility"},
                "onstart": onstart,
                "cancel_unavail": True,
                "python_utf8": True,
                "lang_utf8": True,
            }
            logger.info(
                "Renting offer {} ({} @ ${:.4f}/h)...",
                offer_id,
                offer.get("gpu_name"),
                float(offer.get("dph_total") or 0),
            )
            try:
                result = client.create_instance(offer_id, body)
            except OfferUnavailable as exc:
                last_error = exc
                logger.warning("Offer {} became unavailable: {}", offer_id, exc)
                continue
            except VastAuthError:
                raise
            except AmbiguousCreate as exc:
                # HTTP 5xx and transport failures are ambiguous for PUT: reconcile
                # by the unique label before doing anything else.
                create_was_ambiguous = True
                recovered = recover_created_instance(client, create_label)
                if recovered is None:
                    raise VastError(
                        "Create response was ambiguous and no labelled instance "
                        "appeared during reconciliation; refusing another rental"
                    ) from exc
                instance_id = recovered
                selected_offer = offer
                break
            except VastError:
                # Bad image/configuration and persistent rate-limit failures are not
                # offer races; trying ten offers would only hide the real error.
                raise
            instance_id = int(result["new_contract"])
            selected_offer = offer
            logger.success("Created Vast instance {}", instance_id)
            break

        if instance_id is None or selected_offer is None:
            raise VastError(f"Could not rent any candidate offer: {last_error}")

        info = wait_for_running(client, instance_id, args.boot_timeout)
        remote_host = str(info["ssh_host"])
        remote_port = int(info["ssh_port"])
        logger.info("SSH endpoint: root@{}:{}", remote_host, remote_port)

        wait_for_ssh(args, remote_host, remote_port)
        remote_host, remote_port = wait_for_job(
            args,
            client,
            instance_id,
            remote_host,
            remote_port,
            gpu_name=str(selected_offer.get("gpu_name") or "NVIDIA GPU"),
            hourly_price=float(selected_offer.get("dph_total") or 0),
            expected_input_bytes=input_bytes,
        )
        dest = rsync_results(args, remote_host, remote_port, instance_id)
        published = True

        logger.success("SUCCESS")
        logger.success("Master playlist: {}", dest / "master.m3u8")
        return 0

    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception as exc:
        if remote_host is not None and remote_port is not None:
            collect_remote_diagnostics(args, remote_host, remote_port)
        if args.verbose:
            logger.exception("Pipeline failed: {}", exc)
        else:
            logger.error("Pipeline failed: {}", exc)
        return 1
    finally:
        if instance_id is not None:
            try:
                logger.info("Destroying Vast instance {}...", instance_id)
                client.destroy_instance(instance_id)
            except Exception as exc:
                logger.error("Failed to destroy instance {}: {}", instance_id, exc)
                logger.warning(
                    "The in-container failsafe watchdog should destroy it later; check Vast.ai manually."
                )
        if create_was_ambiguous and create_label:
            try:
                for item in client.instances_with_label(create_label):
                    found_id = int(item["id"])
                    if found_id != instance_id:
                        logger.warning(
                            "Destroying late-discovered labelled instance {}", found_id
                        )
                        client.destroy_instance(found_id)
            except Exception as exc:
                logger.error("Could not complete labelled-instance cleanup: {}", exc)
        if not published and instance_id is not None:
            staging = args.origin_root / args.video_id / f"abr.staging.{instance_id}"
            if staging.exists() and not staging.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)
        if args.known_hosts != original_known_hosts:
            try:
                args.known_hosts.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove temporary known_hosts: {}", exc)
        if lock_file is not None:
            lock_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
