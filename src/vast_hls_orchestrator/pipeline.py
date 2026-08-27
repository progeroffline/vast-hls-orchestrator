"""End-to-end job lifecycle, rendered as one persistent full-screen TUI application."""

from __future__ import annotations

import argparse
import os
import shutil
import time
from typing import Any

from loguru import logger

from .core.console import console
from .core.validation import validate_inputs
from .orchestration.diagnostics import collect_remote_diagnostics
from .orchestration.job_monitor import wait_for_job
from .orchestration.local_state import acquire_video_lock, recover_local_publish_state
from .orchestration.provisioning import (
    ensure_ssh_key_attached,
    rent_instance,
    wait_for_running,
)
from .orchestration.publish import rsync_results
from .orchestration.remote_logs import RemoteLogTailer
from .remote.job_script import build_job_script
from .remote.onstart import build_onstart
from .remote.ssh import wait_for_ssh
from .ui.app import TuiApp
from .ui.phases import spinner_panel, summary_panel
from .vast_api.client import VastClient
from .vast_api.offers import choose_offers, render_offers_table, source_size_bytes


def run(args: argparse.Namespace) -> int:
    app = TuiApp(console, title=f"Vast HLS Orchestrator — {args.video_id}")
    try:
        with app:
            return _run(args, app)
    finally:
        # The alternate screen is gone the moment we leave it, so replay the
        # captured log history to the normal terminal for the operator.
        app.print_recap()


def _run(args: argparse.Namespace, app: TuiApp) -> int:
    try:
        validate_inputs(args)
    except Exception as exc:
        logger.error("{}", exc)
        app.set_body(summary_panel([str(exc)], style="red", title="Invalid arguments"))
        return 2

    api_key = os.getenv("VAST_API_KEY")
    if not api_key:
        logger.error("export VAST_API_KEY first")
        app.set_body(summary_panel(["export VAST_API_KEY first"], style="red", title="Missing credentials"))
        return 2
    if not args.dry_run and not args.ssh_key.is_file():
        logger.error("SSH private key not found: {}", args.ssh_key)
        app.set_body(
            summary_panel([f"SSH private key not found: {args.ssh_key}"], style="red", title="Missing SSH key")
        )
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
    original_known_hosts = args.known_hosts
    try:
        app.set_header_subtitle("searching offers")
        app.set_body(spinner_panel("Searching Vast.ai offers..."))
        offers = choose_offers(client, args, input_gb)
        app.set_body(render_offers_table(offers, input_gb, args.expected_hours))

        if args.dry_run:
            logger.success("Dry-run complete: {} matching offers; nothing rented", len(offers))
            app.set_header_subtitle("dry-run complete")
            return 0

        lock_file = acquire_video_lock(args)
        recover_local_publish_state(args)
        args.known_hosts = original_known_hosts.with_name(
            f"{original_known_hosts.name}.{os.getpid()}"
        )
        args.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        args.known_hosts.touch(mode=0o600, exist_ok=True)
        args.known_hosts.chmod(0o600)

        job_script = build_job_script(args.source_url, input_bytes)
        onstart = build_onstart(job_script, args.failsafe_seconds)
        create_label = f"hls-{args.video_id}-{int(time.time())}-{os.getpid()}"

        app.set_header_subtitle("renting a GPU instance")
        app.set_body(spinner_panel("Renting a Vast.ai GPU instance..."))
        instance_id, selected_offer, create_was_ambiguous = rent_instance(
            client, args, offers, create_label, onstart
        )

        # Account-level key injection doesn't reliably apply to instances
        # created straight through the API the way it does via the console,
        # so attach our key to this instance explicitly too.
        ensure_ssh_key_attached(client, instance_id, args.ssh_key)

        # Vast's own container logs work without SSH, so they're the only window
        # into what the rented machine is doing until wait_for_ssh succeeds.
        log_tailer = RemoteLogTailer(client, instance_id, app)

        app.set_header_subtitle("provisioning")
        app.set_body(spinner_panel(f"Provisioning instance {instance_id}..."))
        info = wait_for_running(client, instance_id, args.boot_timeout, on_poll=log_tailer.poll)
        remote_host = str(info["ssh_host"])
        remote_port = int(info["ssh_port"])
        logger.info("SSH endpoint: root@{}:{}", remote_host, remote_port)

        app.set_body(spinner_panel(f"Waiting for SSH on {remote_host}:{remote_port}..."))
        wait_for_ssh(args, remote_host, remote_port, on_poll=log_tailer.poll)

        app.set_header_subtitle("encoding")
        remote_host, remote_port = wait_for_job(
            args,
            client,
            instance_id,
            remote_host,
            remote_port,
            app=app,
            gpu_name=str(selected_offer.get("gpu_name") or "NVIDIA GPU"),
            hourly_price=float(selected_offer.get("dph_total") or 0),
            expected_input_bytes=input_bytes,
        )

        app.set_header_subtitle("transferring result")
        dest = rsync_results(args, remote_host, remote_port, instance_id, app)
        published = True

        logger.success("SUCCESS")
        logger.success("Master playlist: {}", dest / "master.m3u8")
        app.set_header_subtitle("done")
        app.set_body(summary_panel([f"Published: {dest / 'master.m3u8'}"], title="Success"))
        return 0

    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        app.set_header_subtitle("interrupted")
        app.set_body(summary_panel(["Interrupted by user"], style="yellow", title="Interrupted"))
        return 130
    except Exception as exc:
        if remote_host is not None and remote_port is not None:
            collect_remote_diagnostics(args, remote_host, remote_port, app)
        if args.verbose:
            logger.exception("Pipeline failed: {}", exc)
        else:
            logger.error("Pipeline failed: {}", exc)
        app.set_header_subtitle("failed")
        app.set_body(summary_panel([str(exc)], style="red", title="Pipeline failed"))
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
