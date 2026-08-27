"""Command-line argument parsing."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .core.constants import DEFAULT_GPUS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Encode one video on a temporary Vast.ai GPU"
    )
    parser.add_argument("--video-id", required=True, help="e.g. test")
    parser.add_argument(
        "--source-url", required=True, help="Public/readable source MP4 URL"
    )
    parser.add_argument("--origin-root", type=Path, default=Path("/var/www/html/video"))
    parser.add_argument(
        "--ssh-key",
        type=Path,
        required=True,
        help=(
            "Local private key for Vast SSH; its public half must already be "
            "registered in the Vast.ai account used for VAST_API_KEY"
        ),
    )
    parser.add_argument(
        "--known-hosts", type=Path, default=Path("/root/.ssh/vast_known_hosts")
    )
    parser.add_argument(
        "--image",
        default=os.getenv("VAST_IMAGE", "progeroffline/vast-transcoder:1.1"),
        help=(
            "Docker image for the Vast instance. Default is the project's own "
            "prebuilt worker image (CUDA 12.6 + custom ffmpeg with NVENC/NVDEC/"
            "scale_cuda, aria2c -- see https://hub.docker.com/r/progeroffline/"
            "vast-transcoder), matching the 'HLS Transcoder' Vast template. "
            "Skips installing ffmpeg/aria2 at boot; rsync is still installed "
            "by onstart since it's not baked into the image."
        ),
    )
    parser.add_argument(
        "--disk-gb",
        type=int,
        default=150,
        help="Also the offer search's minimum required disk_space",
    )
    parser.add_argument(
        "--max-hourly",
        type=float,
        default=0.80,
        help="Price ceiling for the (RTX 4080-only, by default) offer search",
    )
    parser.add_argument("--min-reliability", type=float, default=0.98)
    parser.add_argument("--min-cpu", type=int, default=4)
    parser.add_argument("--min-ram-mb", type=int, default=16384)
    parser.add_argument("--min-disk-bw", type=float, default=500.0)
    parser.add_argument(
        "--min-download-mbps",
        type=float,
        default=500.0,
        help="Minimum offer inet_down (Mbps); higher (1 Gbps+) is preferred and used as a ranking tiebreaker",
    )
    parser.add_argument(
        "--min-upload-mbps",
        type=float,
        default=500.0,
        help="Minimum offer inet_up (Mbps); higher (1 Gbps+) is preferred and used as a ranking tiebreaker",
    )
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
        default=1.5,
        help="Seconds between SSH telemetry polls during encoding",
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
    parser.add_argument(
        "--gpus",
        nargs="+",
        default=DEFAULT_GPUS,
        help=(
            "GPU model allow-list to search/rent (default: RTX 4080 only, "
            "benchmarked as the best fit for this project's ABR pipeline -- "
            "no other model is searched unless explicitly overridden here)"
        ),
    )
    return parser.parse_args()
