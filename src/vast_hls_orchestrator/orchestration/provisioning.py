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
