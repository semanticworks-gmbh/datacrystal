"""Helper app for the kill -9 crash test. Runs as a subprocess in two modes:

    python _crash_app.py write  <store-dir>          # commit forever, print TIDs
    python _crash_app.py verify <store-dir> <minimum-batch>

The writer prints each batch number AFTER its commit() returned — every
printed batch is an acked commit and must survive the SIGKILL. The verifier
asserts (a) every present batch is complete (atomicity: never a torn batch)
and (b) every acked batch is present (durability).
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import Annotated

import datacrystal as dc


@dc.entity
class Row:
    batch: Annotated[int, dc.Index]
    seq: int


ROWS_PER_BATCH = 10


def write(path: str) -> None:
    store = dc.Store.open(path, lock_ttl=0.5)
    batch = 0
    while True:
        for seq in range(ROWS_PER_BATCH):
            store.store(Row(batch=batch, seq=seq))
        store.commit()
        print(batch, flush=True)
        batch += 1


def verify(path: str, minimum_batch: int) -> None:
    store = dc.Store.open(path, lock_ttl=0.1)
    rows = store.query(Row.batch >= 0)
    counts = Counter(r.batch for r in rows)
    incomplete = {b: n for b, n in counts.items() if n != ROWS_PER_BATCH}
    assert not incomplete, f"torn batches survived the crash: {incomplete}"
    max_present = max(counts, default=-1)
    assert set(counts) == set(range(max_present + 1)), "batch sequence has holes"
    assert max_present >= minimum_batch, (
        f"acked batch {minimum_batch} lost; only {max_present} survived"
    )
    store.close()
    print(f"VERIFY-OK max_batch={max_present}", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "write":
        write(sys.argv[2])
    else:
        verify(sys.argv[2], int(sys.argv[3]))
