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
