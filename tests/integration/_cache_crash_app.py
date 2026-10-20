"""Helper for the index-cache crash test (#63). Two modes:

    python _cache_crash_app.py write  <store-dir>            # seed a cache, commit forever
    python _cache_crash_app.py verify <store-dir>

The writer cleanly closes a store with ``cache_index=True`` once (writing the
sidecar at an EARLY watermark), reopens, then commits forever — so when SIGKILLed
the on-disk cache is STALE (its watermark trails the committed records) and the
``.tmp`` write of the next sidecar, if any, was never renamed in. The verifier
proves that reopening WITH the cache returns answers identical to reopening
WITHOUT it (the records are authoritative; a stale/partial sidecar is
watermark-rejected and rebuilt — it can never lie) and that the store itself is an
uncorrupted commit prefix. It reports what it found as one JSON line.
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import Annotated

import datacrystal as dc
from tests.integration._crash_support import ack, emit_report, open_store_when_lease_is_stale


@dc.entity
class Row:
    """One row of a batch of ten, with a pure-Unique key (the Design A no-eq path)."""

    key: Annotated[str, dc.Unique]
    batch: Annotated[int, dc.Index]
    seq: int


ROWS_PER_BATCH = 10


def write(path: str) -> None:
    """Seed the cache once, then commit batches forever, printing each acked batch number."""
    # Seed the cache at an early watermark, then never close cleanly again.
    s = dc.Store.open(path, lock_ttl=0.5, durability="commit", cache_index=True)
    for seq in range(ROWS_PER_BATCH):
        s.store(Row(key=f"0-{seq}", batch=0, seq=seq))
    s.commit()
    s.count(Row)            # build the index this session
    s.close()               # writes index.cache at this (early) watermark
    ack(0)

    s = dc.Store.open(path, lock_ttl=0.5, durability="commit", cache_index=True)
    batch = 1
    while True:
        for seq in range(ROWS_PER_BATCH):
            s.store(Row(key=f"{batch}-{seq}", batch=batch, seq=seq))
        s.commit()
        ack(batch)  # acked AFTER commit() returned
        batch += 1


def verify(path: str) -> None:
    """Reopen the killed store with and without the stale cache and report what each answers."""
    # Authoritative truth: reopen WITHOUT the cache (rebuild from records).
    ref = open_store_when_lease_is_stale(path)
    ref_count = ref.count(Row)
    ref_keys = sorted(r.key for r in ref.query(Row))
    ref.close()

    # Reopen WITH the now-stale cache: must be byte-identical in its answers and
    # must hydrate every row (a resurrected/dropped OID would raise here).
    s = open_store_when_lease_is_stale(path, cache_index=True)
    counts = Counter(r.batch for r in s.query(Row))
    cache_count = s.count(Row)
    cache_keys = sorted(r.key for r in s.query(Row))
    s.close()

    max_present = max(counts, default=-1)
    emit_report(
        {
            "torn_batches": sorted(b for b, n in counts.items() if n != ROWS_PER_BATCH),
            "batch_sequence_has_holes": set(counts) != set(range(max_present + 1)),
            "newest_batch": max_present,
            "records_count": ref_count,
            "cache_count": cache_count,
            "cache_keys_equal_record_keys": cache_keys == ref_keys,
        }
    )


if __name__ == "__main__":
    if sys.argv[1] == "write":
        write(sys.argv[2])
    else:
        verify(sys.argv[2])
