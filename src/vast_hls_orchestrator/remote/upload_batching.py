"""Balances HLS media segments across upload workers by total byte size.

Embedded verbatim into the generated remote job script (see
remote/job_script.py) via a python3 heredoc, so this is the single source of
truth for the batching algorithm -- unit-tested directly here as plain
Python, and byte-identical to what actually runs on the Vast instance.
"""

from __future__ import annotations

import heapq
import sys


def plan_batches(entries: list[tuple[str, int]], num_batches: int) -> list[list[str]]:
    """Split `entries` (relative_path, size_bytes) into `num_batches` batches,
    each holding roughly the same total byte size.

    Greedy LPT (largest files first, always into the currently lightest
    batch) -- not round-robin by count and not one batch per rendition,
    since rendition sizes are very unequal and either of those would leave
    one slow worker carrying most of the actual bytes while the rest idle.
    """
    num_batches = max(1, num_batches)
    batches: list[list[str]] = [[] for _ in range(num_batches)]
    # (total_bytes_so_far, batch_index) min-heap; index breaks ties deterministically.
    heap: list[tuple[int, int]] = [(0, i) for i in range(num_batches)]
    heapq.heapify(heap)
    for path, size in sorted(entries, key=lambda e: e[1], reverse=True):
        total, idx = heapq.heappop(heap)
        batches[idx].append(path)
        heapq.heappush(heap, (total + size, idx))
    return batches


def _read_manifest(lines: list[str]) -> list[tuple[str, int]]:
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        size_str, _, path = line.partition(" ")
        entries.append((path, int(size_str)))
    return entries


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: upload_batching.py <num_batches> <out_dir>", file=sys.stderr)
        return 2
    num_batches = int(argv[0])
    out_dir = argv[1]
    entries = _read_manifest(sys.stdin.readlines())
    batches = plan_batches(entries, num_batches)
    for i, batch in enumerate(batches):
        with open(f"{out_dir}/batch_{i}.list", "w", encoding="utf-8") as f:
            f.writelines(path + "\n" for path in batch)
    total = sum(size for _, size in entries)
    with open(f"{out_dir}/segment_total_bytes", "w", encoding="utf-8") as f:
        f.write(str(total) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
