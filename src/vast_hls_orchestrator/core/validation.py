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
    if not args.origin_ssh_host or any(
        ord(ch) < 32 or ord(ch) == 127 for ch in args.origin_ssh_host
    ):
        raise VastError("--origin-ssh-host must be a non-empty hostname/IP")
    for name in (
        "disk_gb",
        "boot_timeout",
        "job_timeout",
        "failsafe_seconds",
        "ssh_reconnect_timeout",
        "rsync_retries",
        "origin_ssh_port",
    ):
        if getattr(args, name) <= 0:
            raise VastError(f"--{name.replace('_', '-')} must be positive")
    # Directly controls how many concurrent outbound SSH connections the
    # remote job opens against origin's sshd -- a typo here (e.g. an extra
    # zero) shouldn't be able to accidentally hammer it.
    if not (1 <= args.upload_workers <= 64):
        raise VastError("--upload-workers must be between 1 and 64")
    if args.upload_worker_stagger < 0:
        raise VastError("--upload-worker-stagger must not be negative")
    if args.upload_worker_stagger == 0:
        logger.warning(
            "--upload-worker-stagger is 0; simultaneous worker starts can trip "
            "origin sshd's MaxStartups (see README) unless it's configured generously"
        )
    if args.monitor_interval <= 0 or args.max_hourly <= 0:
        raise VastError("--monitor-interval and --max-hourly must be positive")
    if args.failsafe_seconds <= args.job_timeout:
        logger.warning(
            "Failsafe timeout ({}) is not greater than job timeout ({}); "
            "the instance may self-destruct during diagnostics/transfer",
            args.failsafe_seconds,
            args.job_timeout,
        )
