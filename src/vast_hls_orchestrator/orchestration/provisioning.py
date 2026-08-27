"""Renting a Vast.ai instance: candidate offer attempts and provisioning wait."""

from __future__ import annotations

import argparse
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from ..core.constants import BAD_STATES
from ..core.errors import AmbiguousCreate, OfferUnavailable, VastAuthError, VastError
from ..vast_api.client import VastClient


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


def ensure_ssh_key_attached(client: VastClient, instance_id: int, public_key: str) -> None:
    """Best-effort: also register our key with Vast's own account-level mechanism.

    This is a secondary safety net, not the primary guarantee -- the onstart
    script (see remote/onstart.py) writes the same key directly into the
    container's authorized_keys with root access, which doesn't depend on
    Vast's account-level injection being in sync with the running container
    (observed in practice to sometimes report a key as "attached" via this
    very API while the container's actual authorized_keys never receives it).
    Failure here is only logged: it isn't what SSH access actually depends on.
    """
    try:
        client.attach_ssh_key(instance_id, public_key)
    except VastError as exc:
        logger.debug(
            "Attach SSH key call for instance {} did not add a new key: {}", instance_id, exc
        )

    attached = {key.get("public_key", "").strip() for key in client.list_ssh_keys(instance_id)}
    if public_key.strip() in attached:
        logger.debug("Confirmed SSH key is attached to instance {}", instance_id)
    else:
        logger.warning(
            "Vast does not list our key as attached to instance {} -- relying on "
            "the onstart-injected authorized_keys entry for SSH access instead",
            instance_id,
        )


def wait_for_running(
    client: VastClient,
    instance_id: int,
    timeout_s: int,
    *,
    on_poll: Callable[[], None] | None = None,
) -> dict:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        if on_poll is not None:
            on_poll()
        info = client.show_instance(instance_id)
        if info is None:
            raise VastError("Instance disappeared while provisioning")
        state = info.get("actual_status")
        msg = info.get("status_msg") or ""
        if state != last:
            logger.info("Instance state: {}{}", state, f"; {msg}" if msg else "")
            last = state
        if state == "running" and info.get("ssh_host") and info.get("ssh_port"):
            return info
        if state in BAD_STATES:
            raise VastError(f"Instance entered terminal/bad state: {state}")
        time.sleep(5)
    raise VastError("Timed out waiting for Vast instance to become running")


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
            # Request proxy SSH alongside direct: direct's per-instance reverse
            # tunnel has been observed to fail to register on some hosts even
            # though the container is otherwise healthy, and proxy SSH goes
            # through Vast's own infrastructure instead, unaffected by that.
            "runtype": "ssh_direct ssh_proxy",
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
