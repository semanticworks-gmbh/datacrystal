"""Helper app for the delta-log kill -9 crash test. Two subprocess modes:

    python _deltalog_crash_app.py write  <dir>            # commit forever + log
    python _deltalog_crash_app.py verify <dir> <min-tid>

The writer attaches a per-commit-fsynced ``DeltaLog`` to a ``durability=
"commit"`` store and prints each commit's TID *after* commit() returned — so
every printed TID was made durable AND delivered to the log before the print.
The verifier asserts the reopened log is an exact, gapless commit prefix
(crash debris truncated/swept), never claims more than the store made durable,
and replays cleanly through the reference applier.
"""

from __future__ import annotations

import sys
from typing import Annotated

import datacrystal as dc
from datacrystal.contract.applier import ReferenceApplier
from datacrystal.deltalog import DeltaLog

ROWS_PER_BATCH = 5


@dc.entity
class Specimen:
    batch: Annotated[int, dc.Index]
    label: str


def write(path: str) -> None:
    # durability="commit" + flush_every=1: the log fsyncs its delta inside the
    # commit's P3, before commit() returns — the log is exactly as durable as
    # the store, so every printed TID is recoverable from the log too.
    store = dc.Store.open(f"{path}/store", lock_ttl=0.5, durability="commit")
    log = DeltaLog(f"{path}/log")
    store.attach(log)
    batch = 0
    while True:
        for seq in range(ROWS_PER_BATCH):
            store.store(Specimen(batch=batch, label=f"b{batch}-{seq}"))
        tid = store.commit()
        if tid is not None:
            print(tid, flush=True)
        batch += 1


def verify(path: str, min_tid: int) -> None:
    log = DeltaLog(f"{path}/log")
    durable = log.durable_watermark
    assert durable >= min_tid, (
        f"log lost acked commits: durable watermark {durable} < acked {min_tid}"
    )
    # An exact, gapless commit prefix — debris of the killed commit truncated
    # (partial append) or swept (orphan segment); the watermark never lied.
    tids = [d["tid"] for d in log.replay()]
    assert tids == list(range(1, durable + 1)), (
        f"log replay is not a gapless 1..{durable} prefix (got {len(tids)} deltas)"
    )
    # Replaying must reconstruct cleanly: no torn frame, no inconsistent prior.
    applier = ReferenceApplier()
    for delta in log.replay():
        applier.apply(delta)
    assert applier.watermark == durable

    # The log can only trail the store (deltas are delivered post-durability),
    # never lead it — reopening the store proves it never invented history.
    store = dc.Store.open(f"{path}/store", lock_ttl=0.1)
    assert store.last_tid >= durable, (
        f"log ahead of store: log {durable} > store {store.last_tid}"
    )
    store.close()
    print(f"VERIFY-OK durable={durable}", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "write":
        write(sys.argv[2])
    else:
        verify(sys.argv[2], int(sys.argv[3]))
