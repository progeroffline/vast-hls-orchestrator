"""Non-interactive SSH transport used for remote monitoring and readiness checks."""

from __future__ import annotations

import argparse
import subprocess
import time

from loguru import logger

from ..core.errors import VastError


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


def wait_for_ssh(
    args: argparse.Namespace, host: str, port: int, timeout_s: int = 300
) -> None:
    deadline = time.time() + timeout_s
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
