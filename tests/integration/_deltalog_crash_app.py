"""Helper app for the delta-log kill -9 crash test. Two subprocess modes:

    python _deltalog_crash_app.py write  <dir>            # commit forever + log
    python _deltalog_crash_app.py verify <dir>

The writer attaches a per-commit-fsynced ``DeltaLog`` to a ``durability=
"commit"`` store and prints each commit's TID *after* commit() returned — so
every printed TID was made durable AND delivered to the log before the print.
The verifier reports, as one JSON line, whether the reopened log is an exact,
gapless commit prefix (crash debris truncated/swept), how far the store got, and
where the reference applier ends after replaying the log.
"""

from __future__ import annotations

import sys
from typing import Annotated

import datacrystal as dc
from datacrystal.contract.applier import ReferenceApplier
from datacrystal.deltalog import DeltaLog
from tests.integration._crash_support import ack, emit_report, open_store_when_lease_is_stale

ROWS_PER_BATCH = 5


@dc.entity
class Specimen:
    """A specimen of a batch of five."""

    batch: Annotated[int, dc.Index]
    label: str


def write(path: str) -> None:
    """Commit batches forever into a store and its delta log, printing each acked TID."""
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
            ack(tid)
        batch += 1


def verify(path: str) -> None:
    """Reopen the killed delta log and its store and report what each holds."""
    log = DeltaLog(f"{path}/log")
    durable = log.durable_watermark
    # An exact, gapless commit prefix — debris of the killed commit truncated
    # (partial append) or swept (orphan segment); the watermark never lied.
    tids = [d["tid"] for d in log.replay()]
    # Replaying must reconstruct cleanly: no torn frame, no inconsistent prior.
    applier = ReferenceApplier()
    for delta in log.replay():
        applier.apply(delta)

    # The log can only trail the store (deltas are delivered post-durability),
    # never lead it — reopening the store proves it never invented history.
    store = open_store_when_lease_is_stale(f"{path}/store")
    store_last_tid = store.last_tid
    store.close()
    emit_report(
        {
            "durable_watermark": durable,
            "replay_is_gapless_prefix": tids == list(range(1, durable + 1)),
            "applier_watermark": applier.watermark,
            "store_last_tid": store_last_tid,
        }
    )


if __name__ == "__main__":
    if sys.argv[1] == "write":
        write(sys.argv[2])
    else:
        verify(sys.argv[2])
