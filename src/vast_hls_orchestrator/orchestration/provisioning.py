"""Renting a Vast.ai instance: candidate offer attempts and provisioning wait."""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

from loguru import logger
from rich.markup import escape

from ..core.console import console
from ..core.constants import BAD_STATES
from ..core.errors import AmbiguousCreate, OfferUnavailable, VastAuthError, VastError
from ..remote.ssh import wait_for_ssh
from ..vast_api.client import VastClient

# How long a single SSH-readiness poll window lasts, how many full
# reboot-and-retry cycles to attempt before giving up, and how long to pause
# after a reboot (before the container is even back to "running") before
# polling SSH again.
SSH_ATTEMPT_TIMEOUT_S = 180
SSH_RECOVERY_ATTEMPTS = 3
SSH_REBOOT_SETTLE_S = 15


def read_public_key(ssh_key_path: Path) -> str | None:
    pub_path = Path(f"{ssh_key_path}.pub")
    try:
        return pub_path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(ssh_key_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def wait_for_running(client: VastClient, instance_id: int, timeout_s: int) -> dict:
    deadline = time.time() + timeout_s
    last = None
    with console.status(
        f"[bold cyan]Provisioning Vast instance {instance_id}...", spinner="dots"
    ) as status:
        while time.time() < deadline:
            info = client.show_instance(instance_id)
            if info is None:
                raise VastError("Instance disappeared while provisioning")
            state = info.get("actual_status")
            msg = info.get("status_msg") or ""
            status.update(
                f"[bold cyan]Instance {instance_id}: {state}[/] [dim]{escape(str(msg))}[/]"
            )
            if state != last:
                logger.info("Instance state: {}{}", state, f"; {msg}" if msg else "")
                last = state
            if state == "running" and info.get("ssh_host") and info.get("ssh_port"):
                return info
            if state in BAD_STATES:
                raise VastError(f"Instance entered terminal/bad state: {state}")
            time.sleep(5)
    raise VastError("Timed out waiting for Vast instance to become running")


def _reset_known_hosts(args: argparse.Namespace) -> None:
    # A reboot restarts the container fresh and can regenerate its sshd host
    # key. StrictHostKeyChecking=accept-new only auto-trusts a host it has
    # *no* record of -- it refuses a *changed* key for one already recorded
    # here, so a pre-reboot entry would otherwise lock us out permanently
    # instead of letting the retry through.
    try:
        args.known_hosts.write_text("", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not reset known_hosts before SSH retry: {}", exc)


def wait_for_ssh_with_recovery(
    args: argparse.Namespace,
    client: VastClient,
    instance_id: int,
    host: str,
    port: int,
) -> tuple[str, int]:
    """Wait for SSH; if it never comes up, reboot the instance and retry.

    onstart rewrites /root/.ssh/authorized_keys from scratch on every
    container start (see remote/onstart.py), so a reboot is the reset used
    here -- there is no SSH session yet to fix authorized_keys by hand.
    Vast's reboot API takes no parameters, so it always re-runs the exact
    onstart the instance was created with; one reboot per cycle already gets
    a freshly-written authorized_keys, doing it twice in a row wouldn't
    change the outcome.
    """
    last_error: VastError | None = None
    for attempt in range(1, SSH_RECOVERY_ATTEMPTS + 1):
        try:
            wait_for_ssh(args, host, port, timeout_s=SSH_ATTEMPT_TIMEOUT_S)
            return host, port
        except VastError as exc:
            last_error = exc
            if attempt == SSH_RECOVERY_ATTEMPTS:
                break
            logger.warning(
                "SSH not reachable after {}s (attempt {}/{}); rebooting instance {} "
                "to reset authorized_keys and retrying",
                SSH_ATTEMPT_TIMEOUT_S,
                attempt,
                SSH_RECOVERY_ATTEMPTS,
                instance_id,
            )
            client.reboot_instance(instance_id)
            info = wait_for_running(client, instance_id, args.boot_timeout)
            host = str(info["ssh_host"])
            port = int(info["ssh_port"])
            _reset_known_hosts(args)
            time.sleep(SSH_REBOOT_SETTLE_S)
    raise VastError(
        f"SSH never became reachable after {SSH_RECOVERY_ATTEMPTS} reboot-and-retry "
        f"cycles: {last_error}"
    )


def recover_created_instance(
    client: VastClient, label: str, timeout_s: int = 45
) -> int | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            matches = client.instances_with_label(label)
            if matches:
                instance_id = int(matches[0]["id"])
                logger.warning(
                    "Recovered instance {} by unique label after ambiguous create response",
                    instance_id,
                )
                return instance_id
        except VastAuthError:
            raise
        except Exception as exc:
            logger.warning("Could not reconcile ambiguous create yet: {}", exc)
        time.sleep(5)
    return None


def rent_instance(
    client: VastClient,
    args: argparse.Namespace,
    offers: list[dict],
    create_label: str,
    onstart: str,
) -> tuple[int, dict, bool]:
    """Try candidate offers in order; return (instance_id, selected_offer, was_ambiguous)."""
    last_error: Exception | None = None
    for offer in offers[:10]:
        offer_id = int(offer["id"])
        body = {
            "image": args.image,
            "disk": args.disk_gb,
            "label": create_label,
            "runtype": "ssh_direct",
            "target_state": "running",
            "env": {"NVIDIA_DRIVER_CAPABILITIES": "compute,video,utility"},
            "onstart": onstart,
            "cancel_unavail": True,
            "python_utf8": True,
            "lang_utf8": True,
        }
        logger.info(
            "Renting offer {} ({} @ ${:.4f}/h)...",
            offer_id,
            offer.get("gpu_name"),
            float(offer.get("dph_total") or 0),
        )
        try:
            result = client.create_instance(offer_id, body)
        except OfferUnavailable as exc:
            last_error = exc
            logger.warning("Offer {} became unavailable: {}", offer_id, exc)
            continue
        except VastAuthError:
            raise
        except AmbiguousCreate as exc:
            # HTTP 5xx and transport failures are ambiguous for PUT: reconcile
            # by the unique label before doing anything else.
            recovered = recover_created_instance(client, create_label)
            if recovered is None:
                raise VastError(
                    "Create response was ambiguous and no labelled instance "
                    "appeared during reconciliation; refusing another rental"
                ) from exc
            return recovered, offer, True
        except VastError:
            # Bad image/configuration and persistent rate-limit failures are not
            # offer races; trying ten offers would only hide the real error.
            raise
        instance_id = int(result["new_contract"])
        logger.success("Created Vast instance {}", instance_id)
        return instance_id, offer, False

    raise VastError(f"Could not rent any candidate offer: {last_error}")
