"""Helper app for the kill -9 crash test. Runs as a subprocess in two modes:

    python _crash_app.py write  <store-dir>          # commit forever, print TIDs
    python _crash_app.py verify <store-dir>

The writer prints each batch number AFTER its commit() returned — every
printed batch is an acked commit and must survive the SIGKILL. The verifier
reopens the store and reports, as one JSON line, which batches are torn
(atomicity: never a torn batch), whether the batch sequence has holes, the newest
batch present (durability) and how many rows it holds after the index rebuild.
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import Annotated

import datacrystal as dc
from tests.integration._crash_support import ack, emit_report, open_store_when_lease_is_stale


@dc.entity
class Row:
    """One row of a batch of ten."""

    batch: Annotated[int, dc.Index]
    seq: int


ROWS_PER_BATCH = 10


def write(path: str) -> None:
    """Commit batches of rows forever, printing each batch number once it is acked."""
    # durability="commit" pins the strongest policy: the gate's claim is
    # "every ACKED commit survives", which per-commit fsync promises even
    # against power loss (SIGKILL alone would also pass under "interval").
    store = dc.Store.open(path, lock_ttl=0.5, durability="commit")
    batch = 0
    while True:
        for seq in range(ROWS_PER_BATCH):
            store.store(Row(batch=batch, seq=seq))
        store.commit()
        ack(batch)
        batch += 1


def verify(path: str) -> None:
    """Reopen the killed store and report what survived."""
    store = open_store_when_lease_is_stale(path)
    rows = store.query(dc.fields(Row).batch >= 0)
    counts = Counter(r.batch for r in rows)
    max_present = max(counts, default=-1)
    # KICKOFF M4 exit, literally: indexes are rebuildable after kill -9 —
    # a bitmap-equality lookup answers from the freshly rebuilt postings.
    rows_in_newest_batch = store.count(dc.fields(Row).batch == max_present)
    store.close()
    emit_report(
        {
            "torn_batches": sorted(b for b, n in counts.items() if n != ROWS_PER_BATCH),
            "batch_sequence_has_holes": set(counts) != set(range(max_present + 1)),
            "newest_batch": max_present,
            "rows_in_newest_batch": rows_in_newest_batch,
        }
    )


if __name__ == "__main__":
    if sys.argv[1] == "write":
        write(sys.argv[2])
    else:
        verify(sys.argv[2])
