"""Ephemeral per-job SSH deploy key: lets Vast push the finished HLS result
directly to origin, without ever granting it more than file-transfer access.

Generated on origin (not Vast) so the private half never has to travel
through Vast's own API surface (the onstart payload) -- it's pushed to the
instance directly over the already-trusted admin SSH channel, as raw stdin,
never as a command-line argument or a logged string. The public half is
appended to a local authorized_keys file, tagged with this job's token so it
can be found and removed again -- both on normal cleanup and by a stale-entry
sweep for the case where cleanup never runs (orchestrator killed -9).
"""

from __future__ import annotations

import argparse
import fcntl
import subprocess
import tempfile
import time
from pathlib import Path

from loguru import logger

from ..core.errors import VastError
from ..remote.ssh import ssh_base

TAG_PREFIX = "vast-deploy"


def _tag(job_token: str) -> str:
    return f"{TAG_PREFIX}-{job_token}-{int(time.time())}"


def generate_keypair() -> tuple[bytes, bytes]:
    """Generate a throwaway ed25519 keypair; return (private_bytes, public_bytes).

    Written to a temp file only because ssh-keygen needs a path, then read
    back into memory -- the temp directory (and both files in it) is removed
    on exit, nothing persists on local disk beyond this call.
    """
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "deploy_key"
        pub_path = Path(f"{key_path}.pub")
        try:
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path), "-q"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise VastError(f"Could not generate deploy keypair: {exc}") from exc
        private_bytes = key_path.read_bytes()
        public_bytes = pub_path.read_bytes()
    return private_bytes, public_bytes


def install_public_key_locally(authorized_keys_path: Path, pubkey: bytes, job_token: str) -> str:
    """Append pubkey to authorized_keys under a unique tag; returns the tag
    (needed to remove exactly this line later, never a namesake from a
    concurrent job)."""
    tag = _tag(job_token)
    line = pubkey.decode("utf-8").strip()
    # A key's own trailing comment field is free-form (ssh-keygen defaults it
    # to "user@host"); overwrite it with our tag so removal/sweep can match
    # on it exactly.
    parts = line.split(None, 2)
    if len(parts) >= 2:
        line = f"{parts[0]} {parts[1]} {tag}"
    authorized_keys_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with authorized_keys_path.open("a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0, 2)  # end -- flock may have waited behind another writer
            f.write(line + "\n")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    authorized_keys_path.chmod(0o600)
    return tag


def remove_public_key_locally(authorized_keys_path: Path, tag: str) -> None:
    if not authorized_keys_path.exists():
        return
    with authorized_keys_path.open("r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            lines = f.readlines()
            kept = [line for line in lines if line.split()[-1:] != [tag]]
            if len(kept) != len(lines):
                f.seek(0)
                f.truncate()
                f.writelines(kept)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def sweep_stale_keys(authorized_keys_path: Path, max_age_seconds: float) -> None:
    """Remove any of this tool's own deploy-key entries older than
    max_age_seconds -- the backstop for a killed-9 orchestrator, whose
    `finally:` cleanup never ran. Never touches a line that doesn't match
    this tool's own tag format.
    """
    if not authorized_keys_path.exists():
        return
    now = time.time()
    with authorized_keys_path.open("r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            lines = f.readlines()
            kept = []
            removed = 0
            for line in lines:
                fields = line.split()
                comment = fields[-1] if fields else ""
                if comment.startswith(f"{TAG_PREFIX}-"):
                    try:
                        ts = int(comment.rsplit("-", 1)[1])
                    except (ValueError, IndexError):
                        kept.append(line)
                        continue
                    if now - ts > max_age_seconds:
                        removed += 1
                        continue
                kept.append(line)
            if removed:
                f.seek(0)
                f.truncate()
                f.writelines(kept)
                logger.warning(
                    "Swept {} stale deploy-key authorized_keys entr{} (older than {:.0f}s)",
                    removed,
                    "y" if removed == 1 else "ies",
                    max_age_seconds,
                )
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def install_private_key_on_remote(
    args: argparse.Namespace, host: str, port: int, private_key: bytes
) -> None:
    """Push the deploy private key onto Vast over the already-trusted admin
    SSH channel. The key travels only via stdin -- never as a command-line
    argument, never in a log line; the only thing ever logged is the fixed
    command text below, which contains no secret.
    """
    command = (
        "install -d -m 700 /root/.ssh && "
        "umask 077 && cat > /root/.ssh/vast_deploy_key && "
        "chmod 600 /root/.ssh/vast_deploy_key"
    )
    logger.debug("SSH {}:{}: {}", host, port, command)
    result = subprocess.run(
        ssh_base(args, host, port) + [command],
        input=private_key,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise VastError(
            f"Could not install deploy key on the instance: "
            f"{result.stderr.decode(errors='replace')}"
        )
