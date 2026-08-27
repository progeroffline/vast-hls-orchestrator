"""Non-interactive SSH transport used for remote monitoring and readiness checks."""

from __future__ import annotations

import argparse
import subprocess
import time
from collections.abc import Callable

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
    args: argparse.Namespace,
    direct_host: str,
    direct_port: int,
    timeout_s: int = 300,
    *,
    proxy_host: str | None = None,
    proxy_port: int | None = None,
    on_poll: Callable[[], None] | None = None,
) -> tuple[str, int]:
    """Poll direct SSH, falling back to Vast's proxy SSH if it never comes up.

    Returns the (host, port) that actually became reachable, which can be the
    proxy endpoint even though direct was requested first: direct SSH depends
    on a per-instance reverse tunnel on the host registering correctly, which
    has been observed to fail (repeated "remote port forwarding failed" in the
    instance's own boot log) even though the container and proxy SSH are
    otherwise healthy.
    """
    candidates = [(direct_host, direct_port)]
    if proxy_host and proxy_port:
        candidates.append((proxy_host, proxy_port))

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if on_poll is not None:
            on_poll()
        for host, port in candidates:
            try:
                result = ssh_run(args, host, port, "echo SSH_OK", timeout=15)
                if result.returncode == 0 and "SSH_OK" in result.stdout:
                    logger.success("SSH is ready via {}:{}", host, port)
                    return host, port
            except Exception:
                pass
        time.sleep(3)
    raise VastError("SSH did not become ready via direct or proxy connection")
