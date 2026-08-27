"""Thin HTTP client for the Vast.ai REST API with retry/error classification."""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from typing import Any

import requests
from loguru import logger

from ..core.constants import API_BASE, API_V1_BASE
from ..core.errors import AmbiguousCreate, OfferUnavailable, VastAuthError, VastError


class VastClient:
    def __init__(self, api_key: str):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict[str, str] | None = None,
        timeout: int = 30,
        allow_404: bool = False,
        retry: bool = True,
        retry_429: bool = False,
        api_base: str = API_BASE,
    ) -> Any:
        url = f"{api_base}{path}"
        last_error = None
        attempts = 6 if retry or retry_429 else 1
        for attempt in range(attempts):
            try:
                logger.debug("Vast API {} {} (attempt {})", method, path, attempt + 1)
                r = self.s.request(
                    method, url, json=body, params=params, timeout=timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                if not retry:
                    raise VastError(
                        f"Ambiguous API result for {method} {path}: {exc}"
                    ) from exc
                delay = min(2**attempt, 20)
                logger.warning(
                    "Vast API transport error: {}. Retrying in {}s", exc, delay
                )
                time.sleep(delay)
                continue

            if allow_404 and r.status_code == 404:
                return None

            if r.status_code in {401, 403}:
                raise VastAuthError(
                    f"Vast API rejected credentials/permissions (HTTP {r.status_code}): "
                    f"{r.text[:500]}"
                )

            if r.status_code == 429 or 500 <= r.status_code < 600:
                last_error = VastError(f"HTTP {r.status_code}: {r.text[:500]}")
                can_retry_status = retry or (r.status_code == 429 and retry_429)
                if not can_retry_status:
                    raise last_error
                retry_after = r.headers.get("Retry-After", "")
                try:
                    delay = min(max(float(retry_after), 0.0), 60.0)
                except ValueError:
                    delay = min(2**attempt + random.random(), 20)
                logger.warning(
                    "Vast API HTTP {}. Retrying in {}s", r.status_code, delay
                )
                time.sleep(delay)
                continue

            if not r.ok:
                raise VastError(
                    f"{method} {url}: HTTP {r.status_code}: {r.text[:1000]}"
                )

            if not r.text.strip():
                return {}
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text}

        raise VastError(f"API request failed after {attempts} attempt(s): {last_error}")

    def search_offers(self, gpu_name: str, args: argparse.Namespace) -> list[dict]:
        payload = {
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "gpu_name": {"eq": gpu_name},
            "num_gpus": {"eq": 1},
            "reliability": {"gte": args.min_reliability},
            "cpu_cores_effective": {"gte": args.min_cpu},
            "cpu_ram": {"gte": args.min_ram_mb},
            "disk_space": {"gte": args.disk_gb},
            "disk_bw": {"gte": args.min_disk_bw},
            "direct_port_count": {"gte": 1},
            "cuda_max_good": {"gte": 12.6},
            "dph_total": {"lte": args.max_hourly},
            "duration": {"gte": args.boot_timeout + args.job_timeout},
            "order": [["dph_total", "asc"]],
            "type": "on-demand",
            "limit": 20,
        }
        data = self.request("POST", "/bundles/", body=payload)

        offers = data.get("offers", []) if isinstance(data, dict) else []
        if isinstance(offers, dict):
            offers = [offers]
        return [x for x in offers if isinstance(x, dict)]

    def create_instance(self, offer_id: int, body: dict) -> dict:
        # Never blindly retry this non-idempotent operation. A timed-out response may
        # still have created a billable contract.
        try:
            data = self.request(
                "PUT",
                f"/asks/{offer_id}/",
                body=body,
                retry=False,
                retry_429=True,
            )
        except VastError as exc:
            message = str(exc).lower()
            if any(
                marker in message
                for marker in (
                    "http 404",
                    "http 410",
                    "unavailable",
                    "not available",
                    "no_compatible_tag",
                    "already rented",
                )
            ):
                raise OfferUnavailable(str(exc)) from exc
            if "ambiguous api result" in message or re.search(
                r"http 5\d\d", message
            ):
                raise AmbiguousCreate(str(exc)) from exc
            raise
        if not isinstance(data, dict):
            raise VastError(f"Unexpected create response: {data!r}")
        if not data.get("success") or not data.get("new_contract"):
            message = json.dumps(data, ensure_ascii=False)
            lowered = message.lower()
            if any(
                marker in lowered
                for marker in ("unavailable", "no_compatible_tag", "already rented")
            ):
                raise OfferUnavailable(message)
            raise VastError(f"Create failed: {message}")
        return data

    def show_instance(self, instance_id: int) -> dict | None:
        data = self.request("GET", f"/instances/{instance_id}/", allow_404=True)
        if data is None:
            return None
        if isinstance(data, dict) and "instances" in data:
            instance = data["instances"]
            if isinstance(instance, list):
                return instance[0] if instance else None
            return instance if isinstance(instance, dict) else None
        return data if isinstance(data, dict) else None

    def instances_with_label(self, label: str) -> list[dict]:
        data = self.request(
            "GET",
            "/instances",
            params={
                "limit": "25",
                "select_cols": json.dumps(["id", "label", "actual_status"]),
                "select_filters": json.dumps({"label": {"eq": label}}),
            },
            api_base=API_V1_BASE,
        )
        items = data.get("instances", []) if isinstance(data, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def request_logs(self, instance_id: int, *, tail: str = "1000") -> str | None:
        """Fetch the instance's own container log tail (no SSH required).

        Vast generates the dump asynchronously and hands back an S3 URL, which
        may briefly 404 before the upload completes -- callers are expected to
        poll this periodically and treat a `None` result as "not ready yet".
        """
        try:
            data = self.request(
                "PUT",
                f"/instances/request_logs/{instance_id}",
                body={"tail": tail},
                timeout=20,
                retry=False,
            )
        except VastError as exc:
            logger.debug("Could not request instance logs: {}", exc)
            return None
        if not isinstance(data, dict) or not data.get("success"):
            return None
        result_url = data.get("result_url")
        if not result_url:
            return None
        try:
            # Deliberately a bare request, not self.s: result_url is a
            # presigned S3 link and must not receive our Vast API bearer token.
            r = requests.get(result_url, timeout=20)
            r.raise_for_status()
            return r.text
        except requests.RequestException as exc:
            logger.debug("Could not download instance logs from {}: {}", result_url, exc)
            return None

    def attach_ssh_key(self, instance_id: int, public_key: str) -> None:
        """Explicitly attach a public key to an already-created instance.

        Account-level keys (Console -> Keys) are supposed to be injected into
        every new instance automatically, but that has been observed to not
        reliably apply to instances created straight through this API the way
        it does for instances rented through the web console. This is Vast's
        own documented way to add a key to a running Docker instance, so it's
        called unconditionally right after creation as a belt-and-braces step.
        """
        data = self.request(
            "POST",
            f"/instances/{instance_id}/ssh",
            body={"ssh_key": public_key},
            timeout=20,
            retry=False,
        )
        if isinstance(data, dict) and data.get("success") is False:
            raise VastError(f"Attach SSH key failed for instance {instance_id}: {data}")

    def destroy_instance(self, instance_id: int) -> None:
        data = self.request("DELETE", f"/instances/{instance_id}/", allow_404=True)
        if data is None:
            logger.info("Vast instance {} is already gone", instance_id)
            return
        if isinstance(data, dict) and data.get("success") is False:
            raise VastError(f"Destroy failed for instance {instance_id}: {data}")
        logger.success("Vast instance {} destroyed", instance_id)
