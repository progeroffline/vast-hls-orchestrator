"""Tests for the segment-to-upload-worker batching algorithm.

plan_batches() must never lose or duplicate a segment, must preserve the
exact relative path strings the finalizing stage already produced (e.g.
"1080p/segment_00000.ts"), and must balance batches by total byte size --
not naive round-robin by count and not one batch per rendition, since
rendition sizes are very unequal (see the module docstring).
"""

from __future__ import annotations

from vast_hls_orchestrator.remote.upload_batching import plan_batches

# Loosely mirrors the real bitrate ladder's relative proportions (1080p
# segments are much larger than 360p ones) so a test that only worked on
# uniform file sizes wouldn't catch a round-robin-by-count regression.
RENDITION_SEGMENT_BYTES = {
    "1080p": 1_600_000,
    "720p": 850_000,
    "480p": 450_000,
    "360p": 220_000,
}


def _skewed_entries(segments_per_rendition: int = 20) -> list[tuple[str, int]]:
    entries = []
    for rendition, size in RENDITION_SEGMENT_BYTES.items():
        for i in range(segments_per_rendition):
            entries.append((f"{rendition}/segment_{i:05d}.ts", size))
    return entries


def test_every_segment_appears_exactly_once():
    entries = _skewed_entries()
    batches = plan_batches(entries, num_batches=16)

    all_paths = [path for batch in batches for path in batch]
    assert sorted(all_paths) == sorted(path for path, _ in entries)
    assert len(all_paths) == len(set(all_paths))


def test_relative_paths_preserved_verbatim():
    entries = [("1080p/segment_00000.ts", 100), ("360p/segment_00042.ts", 5)]
    batches = plan_batches(entries, num_batches=4)

    all_paths = {path for batch in batches for path in batch}
    assert all_paths == {"1080p/segment_00000.ts", "360p/segment_00042.ts"}


def test_batches_are_not_split_purely_by_rendition():
    entries = _skewed_entries()
    batches = plan_batches(entries, num_batches=16)

    # If this were "one worker per rendition," at most 4 batches would be
    # non-empty. With 16 batches and 4x more segments than workers, every
    # batch should contain a mix.
    non_empty = [b for b in batches if b]
    assert len(non_empty) == 16
    renditions_per_batch = [
        {path.split("/", 1)[0] for path in batch} for batch in non_empty
    ]
    assert any(len(r) > 1 for r in renditions_per_batch)


def test_batches_are_balanced_by_total_size():
    entries = _skewed_entries()
    batches = plan_batches(entries, num_batches=16)
    sizes = dict(entries)

    totals = [sum(sizes[path] for path in batch) for batch in batches]
    grand_total = sum(totals)
    ideal = grand_total / 16

    # Greedy LPT's worst-case bound is well within a generous tolerance for
    # this input shape; a naive round-robin-by-count split of this skewed
    # distribution would miss this badly (some batches nearly all 1080p,
    # others nearly all 360p).
    for total in totals:
        assert abs(total - ideal) / ideal < 0.15, (totals, ideal)


def test_fewer_segments_than_workers_leaves_some_batches_empty_not_erroring():
    entries = [("1080p/segment_00000.ts", 100), ("720p/segment_00000.ts", 50)]
    batches = plan_batches(entries, num_batches=16)

    assert len(batches) == 16
    non_empty = [b for b in batches if b]
    assert len(non_empty) == 2
    assert sorted(p for b in batches for p in b) == [
        "1080p/segment_00000.ts",
        "720p/segment_00000.ts",
    ]


def test_single_batch_gets_everything():
    entries = _skewed_entries(segments_per_rendition=5)
    batches = plan_batches(entries, num_batches=1)

    assert len(batches) == 1
    assert sorted(batches[0]) == sorted(path for path, _ in entries)


def test_no_entries_produces_empty_batches():
    batches = plan_batches([], num_batches=16)
    assert len(batches) == 16
    assert all(batch == [] for batch in batches)
