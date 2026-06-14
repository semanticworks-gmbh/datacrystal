"""Helper for the index-cache crash test (#63). Two modes:

    python _cache_crash_app.py write  <store-dir>            # seed a cache, commit forever
    python _cache_crash_app.py verify <store-dir> <min-batch>

The writer cleanly closes a store with ``cache_index=True`` once (writing the
sidecar at an EARLY watermark), reopens, then commits forever — so when SIGKILLed
the on-disk cache is STALE (its watermark trails the committed records) and the
``.tmp`` write of the next sidecar, if any, was never renamed in. The verifier
proves that reopening WITH the cache returns answers identical to reopening
WITHOUT it (the records are authoritative; a stale/partial sidecar is
watermark-rejected and rebuilt — it can never lie) and that the store itself is an
uncorrupted commit prefix.
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import Annotated

import datacrystal as dc


@dc.entity
class Row:
    key: Annotated[str, dc.Unique]      # pure-Unique → the Design A (no-eq) path
    batch: Annotated[int, dc.Index]
    seq: int


ROWS_PER_BATCH = 10


def write(path: str) -> None:
    # Seed the cache at an early watermark, then never close cleanly again.
    s = dc.Store.open(path, lock_ttl=0.5, durability="commit", cache_index=True)
    for seq in range(ROWS_PER_BATCH):
        s.store(Row(key=f"0-{seq}", batch=0, seq=seq))
    s.commit()
    s.count(Row)            # build the index this session
    s.close()               # writes index.cache at this (early) watermark
    print(0, flush=True)

    s = dc.Store.open(path, lock_ttl=0.5, durability="commit", cache_index=True)
    batch = 1
    while True:
        for seq in range(ROWS_PER_BATCH):
            s.store(Row(key=f"{batch}-{seq}", batch=batch, seq=seq))
        s.commit()
        print(batch, flush=True)   # acked AFTER commit() returned
        batch += 1


def verify(path: str, minimum_batch: int) -> None:
    # Authoritative truth: reopen WITHOUT the cache (rebuild from records).
    ref = dc.Store.open(path, lock_ttl=0.1)
    ref_count = ref.count(Row)
    ref_keys = sorted(r.key for r in ref.query(Row))
    ref.close()

    # Reopen WITH the now-stale cache: must be byte-identical in its answers and
    # must hydrate every row (a resurrected/dropped OID would raise here).
    s = dc.Store.open(path, lock_ttl=0.1, cache_index=True)
    counts = Counter(r.batch for r in s.query(Row))
    cache_count = s.count(Row)
    cache_keys = sorted(r.key for r in s.query(Row))
    s.close()

    incomplete = {b: n for b, n in counts.items() if n != ROWS_PER_BATCH}
    assert not incomplete, f"torn batches survived the crash: {incomplete}"
    max_present = max(counts, default=-1)
    assert set(counts) == set(range(max_present + 1)), "batch sequence has holes"
    assert max_present >= minimum_batch, (
        f"acked batch {minimum_batch} lost; only {max_present} survived"
    )
    assert cache_count == ref_count and cache_keys == ref_keys, (
        "the stale cache returned a different answer than the records — "
        f"cache={cache_count}/{len(cache_keys)} vs records={ref_count}/{len(ref_keys)}"
    )
    print(f"VERIFY-OK max_batch={max_present} count={cache_count}", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "write":
        write(sys.argv[2])
    else:
        verify(sys.argv[2], int(sys.argv[3]))
