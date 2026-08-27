"""End-to-end job lifecycle: rent, provision, encode, transfer, publish, destroy."""

from __future__ import annotations

import argparse
import os
import shutil
import time
from typing import Any

from loguru import logger

from .core.errors import VastError
from .core.validation import validate_inputs
from .orchestration.diagnostics import collect_remote_diagnostics
from .orchestration.job_monitor import wait_for_job
from .orchestration.local_state import acquire_video_lock, recover_local_publish_state
from .orchestration.provisioning import (
    read_public_key,
    rent_instance,
    wait_for_running,
    wait_for_ssh_with_recovery,
)
from .orchestration.publish import rsync_results
from .remote.job_script import build_job_script
from .remote.onstart import build_onstart
from .ui.formatting import format_cost, format_duration
from .vast_api.client import VastClient
from .vast_api.offers import choose_offers, source_size_bytes


def _log_cost_summary(rental_started_at: float | None, selected_offer: dict | None) -> None:
    """Log total spend so far, if we got far enough to have actually rented anything."""
    if rental_started_at is None or selected_offer is None:
        return
    hourly_price = float(selected_offer.get("dph_total") or 0)
    elapsed = time.time() - rental_started_at
    logger.info(
        "Total cost: {} ({} @ ${:.4f}/h)",
        format_cost(hourly_price, elapsed),
        format_duration(elapsed),
        hourly_price,
    )


def run(args: argparse.Namespace) -> int:
    try:
        validate_inputs(args)
    except Exception as exc:
        logger.error("{}", exc)
        return 2

    api_key = os.getenv("VAST_API_KEY")
    if not api_key:
        logger.error("export VAST_API_KEY first")
        return 2
    if not args.dry_run and not args.ssh_key.is_file():
        logger.error("SSH private key not found: {}", args.ssh_key)
        return 2

    client = VastClient(api_key)
    input_bytes = source_size_bytes(args.source_url)
    input_gb = input_bytes / 1_000_000_000 if input_bytes else 10.0
    logger.info(
        "Source size estimate: {}",
        f"{input_gb:.2f} GB"
        if input_bytes
        else "unknown (using 10 GB for offer ranking)",
    )

    instance_id: int | None = None
    selected_offer: dict | None = None
    remote_host: str | None = None
    remote_port: int | None = None
    lock_file: Any | None = None
    create_label: str | None = None
    create_was_ambiguous = False
    published = False
    rental_started_at: float | None = None
    original_known_hosts = args.known_hosts
    try:
        offers = choose_offers(client, args, input_gb)
        if args.dry_run:
            logger.success("Dry-run complete: {} matching offers; nothing rented", len(offers))
            return 0

        lock_file = acquire_video_lock(args)
        recover_local_publish_state(args)
        args.known_hosts = original_known_hosts.with_name(
            f"{original_known_hosts.name}.{os.getpid()}"
        )
        args.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        args.known_hosts.touch(mode=0o600, exist_ok=True)
        args.known_hosts.chmod(0o600)

        # The onstart script writes this directly into the instance's own
        # authorized_keys with root access -- the actual guarantee for SSH
        # access, independent of whether Vast's own account-level key
        # injection is in sync with the running container (see onstart.py).
        public_key = read_public_key(args.ssh_key)
        if not public_key:
            raise VastError(
                f"Could not read public key for {args.ssh_key} (looked for "
                f"{args.ssh_key}.pub and tried ssh-keygen -y)"
            )

        job_script = build_job_script(args.source_url, input_bytes)
        onstart = build_onstart(job_script, args.failsafe_seconds, public_key)
        create_label = f"hls-{args.video_id}-{int(time.time())}-{os.getpid()}"

        instance_id, selected_offer, create_was_ambiguous = rent_instance(
            client, args, offers, create_label, onstart
        )
        # Vast starts billing from here, not from whenever encode-monitoring
        # happens to start -- provisioning/bootstrap/download cost money too.
        rental_started_at = time.time()

        info = wait_for_running(client, instance_id, args.boot_timeout)
        remote_host = str(info["ssh_host"])
        remote_port = int(info["ssh_port"])
        logger.info("SSH endpoint: root@{}:{}", remote_host, remote_port)

        remote_host, remote_port = wait_for_ssh_with_recovery(
            args, client, instance_id, remote_host, remote_port
        )
        remote_host, remote_port = wait_for_job(
            args,
            client,
            instance_id,
            remote_host,
            remote_port,
            gpu_name=str(selected_offer.get("gpu_name") or "NVIDIA GPU"),
            hourly_price=float(selected_offer.get("dph_total") or 0),
            expected_input_bytes=input_bytes,
            rental_started_at=rental_started_at,
        )
        dest = rsync_results(args, remote_host, remote_port, instance_id)
        published = True

        logger.success("SUCCESS")
        logger.success("Master playlist: {}", dest / "master.m3u8")
        _log_cost_summary(rental_started_at, selected_offer)
        return 0

    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        _log_cost_summary(rental_started_at, selected_offer)
        return 130
    except Exception as exc:
        if remote_host is not None and remote_port is not None:
            collect_remote_diagnostics(args, remote_host, remote_port)
        if args.verbose:
            logger.exception("Pipeline failed: {}", exc)
        else:
            logger.error("Pipeline failed: {}", exc)
        _log_cost_summary(rental_started_at, selected_offer)
        return 1
    finally:
        if instance_id is not None:
            try:
                logger.info("Destroying Vast instance {}...", instance_id)
                client.destroy_instance(instance_id)
            except Exception as exc:
                logger.error("Failed to destroy instance {}: {}", instance_id, exc)
                logger.warning(
                    "The in-container failsafe watchdog should destroy it later; check Vast.ai manually."
                )
        if create_was_ambiguous and create_label:
            try:
                for item in client.instances_with_label(create_label):
                    found_id = int(item["id"])
                    if found_id != instance_id:
                        logger.warning(
                            "Destroying late-discovered labelled instance {}", found_id
                        )
                        client.destroy_instance(found_id)
            except Exception as exc:
                logger.error("Could not complete labelled-instance cleanup: {}", exc)
        if not published and instance_id is not None:
            staging = args.origin_root / args.video_id / f"abr.staging.{instance_id}"
            if staging.exists() and not staging.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)
        if args.known_hosts != original_known_hosts:
            try:
                args.known_hosts.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove temporary known_hosts: {}", exc)
        if lock_file is not None:
            lock_file.close()
