"""Resumable rsync pull from the Vast instance and atomic HLS publish."""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import shlex
import shutil
import time
from pathlib import Path

from loguru import logger

from ..core.constants import RENDITIONS
from ..core.errors import VastError
from .transfer import stream_process


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
