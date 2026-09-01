"""Local atomic HLS publish, from an already-populated staging directory.

The Vast instance pushes the result directly into this staging directory
itself (see remote/job_script.py's uploading stage) -- this module no longer
does any network transfer. It only validates what's there, promotes
master.m3u8.tmp (deliberately pushed under that temporary name -- see
job_script.py) to its real name, and atomically swaps the staging directory
into the public `abr` dir.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import os
import shutil
from pathlib import Path

from loguru import logger

from ..core.constants import RENDITIONS
from ..core.errors import VastError


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


def staging_bytes_on_disk(staging: Path) -> int:
    """Real bytes written to disk under `staging` so far -- used to report
    upload progress. Same "actual disk blocks, not apparent size" reasoning
    as remote/snapshot.py's download-progress fix: rsync writes files as
    they land, but summing st_size would also work here since these are
    ordinary whole files (not sparse), not concurrently-written-out-of-order
    ones -- st_blocks is used anyway for consistency with that reasoning and
    because it's a closer match to "bytes actually transferred" than logical
    size for a file rsync may have partially written and not yet completed.
    """
    total = 0
    for path in staging.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_blocks * 512
            except OSError:
                continue
    return total


def finalize_push_publish(args: argparse.Namespace, staging: Path, job_token: str) -> Path:
    """Validate a fully-pushed staging directory and atomically publish it.

    Called once the remote job's upload stage has finished pushing every
    segment, every rendition playlist, and finally master.m3u8.tmp -- never
    before. staging/backup are named by job_token (not instance_id: the
    instance doesn't exist until after the staging dir is first created --
    see local_state.prepare_staging_dir).
    """
    dest = args.origin_root / args.video_id / "abr"
    backup = args.origin_root / args.video_id / f"abr.backup.{job_token}"

    master_tmp = staging / "master.m3u8.tmp"
    if not master_tmp.is_file() or master_tmp.stat().st_size == 0:
        raise VastError(f"Missing/empty result file: {master_tmp}")
    required = [
        staging / "1080p" / "index.m3u8",
        staging / "720p" / "index.m3u8",
        staging / "480p" / "index.m3u8",
        staging / "360p" / "index.m3u8",
    ]
    for path in required:
        if not path.is_file() or path.stat().st_size == 0:
            raise VastError(f"Missing/empty result file: {path}")
    for q in RENDITIONS:
        if not any(
            p.is_file() and p.stat().st_size > 0 for p in (staging / q).glob("segment_*.ts")
        ):
            raise VastError(f"No HLS segments found for {q}")

    # Promote the temp name only now that everything else is confirmed
    # present -- this is the "master published last, atomically" guarantee:
    # a client can never see a master.m3u8 whose renditions/segments aren't
    # already fully in staging, since this rename (and the exchange below)
    # only happen after the checks above already passed.
    master_tmp.rename(staging / "master.m3u8")

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
