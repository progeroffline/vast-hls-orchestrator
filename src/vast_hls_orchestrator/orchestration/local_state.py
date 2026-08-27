"""Per-video file locking and recovery of interrupted local publish state."""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import time
from typing import Any

from loguru import logger

from ..core.errors import VastError


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
