"""CLI input validation performed before any network or filesystem side effects."""

from __future__ import annotations

import argparse
import re
from urllib.parse import urlsplit

from loguru import logger

from .errors import VastError


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
