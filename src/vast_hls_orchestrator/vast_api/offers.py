"""Source size probing plus Vast.ai offer cost estimation, ranking and a display table."""

from __future__ import annotations

import argparse

import requests
from loguru import logger
from rich.table import Table

from ..core.console import console
from ..core.constants import GPU_NVENC_SESSIONS
from ..core.errors import VastAuthError, VastError
from .client import VastClient


def gpu_nvenc_sessions(gpu_name: str) -> int:
    """How many concurrent NVENC sessions this GPU model has (1 if unknown)."""
    return GPU_NVENC_SESSIONS.get((gpu_name or "").strip(), 1)


def source_size_bytes(url: str) -> int | None:
    try:
        r = requests.head(url, allow_redirects=True, timeout=20)
        r.raise_for_status()
        n = int(r.headers.get("Content-Length", "0"))
        return n if n > 0 else None
    except Exception as exc:
        logger.debug("Could not determine source size with HEAD: {}", exc)
        return None


def source_size_gb(url: str) -> float:
    n = source_size_bytes(url)
    return n / 1_000_000_000 if n else 10.0


def offer_estimated_cost(offer: dict, input_gb: float, expected_hours: float) -> float:
    hourly = float(offer.get("dph_total") or 999)
    down_cost = float(offer.get("inet_down_cost") or 0)
    up_cost = float(offer.get("inet_up_cost") or 0)
    output_gb = input_gb * 1.7
    return hourly * expected_hours + input_gb * down_cost + output_gb * up_cost


def choose_offers(
    client: VastClient, args: argparse.Namespace, input_gb: float
) -> list[dict]:
    offers: list[dict] = []
    search_errors: list[Exception] = []
    with console.status("[bold cyan]Searching Vast.ai offers...", spinner="dots"):
        for gpu in args.gpus:
            try:
                found = client.search_offers(gpu, args)
                logger.info("{}: found {} matching offers", gpu, len(found))
                offers.extend(found)
            except VastAuthError:
                raise
            except Exception as exc:
                search_errors.append(exc)
                logger.warning("Search failed for {}: {}", gpu, exc)

    dedup: dict[int, dict] = {}
    for offer in offers:
        try:
            dedup[int(offer["id"])] = offer
        except Exception:
            continue

    offers = list(dedup.values())
    offers.sort(
        key=lambda o: (
            # More NVENC engines first: the ABR pipeline's 4 parallel encode
            # branches get real hardware parallelism instead of time-slicing
            # a single engine (see core.constants.GPU_NVENC_SESSIONS). Cost
            # is only a tiebreaker within the same NVENC tier, not the
            # primary ranking -- "best offer" here means best for this job,
            # not just cheapest.
            -gpu_nvenc_sessions(o.get("gpu_name", "")),
            offer_estimated_cost(o, input_gb, args.expected_hours),
            float(o.get("dph_total") or 999),
            # Both required at a 500 Mbps floor by the search filters already;
            # among candidates that clear it, still prefer faster (1 Gbps+
            # is the stated preference) for both directions -- download feeds
            # the encode, upload carries the direct rsync push to origin.
            -float(o.get("inet_down") or 0),
            -float(o.get("inet_up") or 0),
            -float(o.get("disk_bw") or 0),
        )
    )

    if not offers:
        if len(search_errors) == len(args.gpus):
            raise VastError(f"All offer searches failed: {search_errors[-1]}")
        # Deliberately no automatic fallback to a different GPU model here --
        # args.gpus is exactly what was configured to search/rent (RTX 4080
        # only, by default); if none of it clears the filters, that's a hard
        # stop, not a cue to broaden the search.
        raise VastError(f"No suitable {'/'.join(args.gpus)} instance available")

    table = Table(
        title="Top Vast.ai candidates", show_lines=False, header_style="bold cyan"
    )
    table.add_column("Offer", justify="right")
    table.add_column("GPU")
    table.add_column("NVENC", justify="right")
    table.add_column("Direct", justify="right")
    table.add_column("$/h", justify="right")
    table.add_column("Reliability", justify="right")
    table.add_column("Down Mbps", justify="right")
    table.add_column("Up Mbps", justify="right")
    table.add_column("Disk MB/s", justify="right")
    table.add_column("Est. job", justify="right")

    for offer in offers[:5]:
        est = offer_estimated_cost(offer, input_gb, args.expected_hours)
        gpu_name = str(offer.get("gpu_name", "-"))
        table.add_row(
            str(offer.get("id", "-")),
            gpu_name,
            str(gpu_nvenc_sessions(gpu_name)),
            "yes" if int(offer.get("direct_port_count") or 0) >= 1 else "no",
            f"{float(offer.get('dph_total') or 0):.4f}",
            f"{float(offer.get('reliability') or 0):.4f}",
            f"{float(offer.get('inet_down') or 0):.0f}",
            f"{float(offer.get('inet_up') or 0):.0f}",
            f"{float(offer.get('disk_bw') or 0):.0f}",
            f"${est:.4f}",
        )
    console.print(table)
    return offers
