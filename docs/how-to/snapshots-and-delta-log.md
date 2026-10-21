# How-to: read from any thread, follow the commit stream and keep an audit log

After this page you can read committed state from a worker thread, attach a consumer to every commit and replay a durable history of them.

```python
import threading
from typing import Annotated

import datacrystal as dc
from datacrystal.deltalog import DeltaLog
from datacrystal.testing import CountingConsumer

@dc.entity
class Specimen:
    specimen_no: Annotated[str, dc.Unique]
    quality: Annotated[str, dc.Index]
    mass_g: float

store = dc.Store.open("cabinet.store")
log = DeltaLog("cabinet.deltalog")
store.attach(log)
with store.snapshot() as snap:
    counter = CountingConsumer.bootstrap(snap)
store.attach(counter)

store.store(Specimen(specimen_no=f"DC-{store.last_tid}", quality="fine", mass_g=120.0))
store.commit()

def report() -> None:
    with store.snapshot() as snap:
        S = dc.fields(Specimen)
        print("fine >= 100 g:", snap.count((S.quality == "fine") & (S.mass_g >= 100.0)), "at tid", snap.tid)

worker = threading.Thread(target=report)
worker.start()
worker.join()
print(counter.counts, counter.watermark == store.last_tid)
print(len(list(log.replay())), "deltas replayed")
store.close()
```

- A snapshot is a frozen view of the committed state at one watermark, and the sanctioned way for a worker thread to read while the owner writes.
  It returns immutable `dc.EntityView`s and explicit `dc.Ref` tokens that you resolve with `snap.get(ref)`; lists come back as tuples and dicts as read-only mappings.
  The method set is in [Snapshots](../reference.md#snapshots).
- Close a snapshot promptly: on SQLite an open snapshot holds a WAL read transaction that blocks checkpoint truncation.
- `store.attach(consumer)` hands the consumer exactly one delta per commit, in TID order, on the owner thread, strictly after the commit is durable ([COMMIT-DELTA-v2](../design/COMMIT-DELTA-v2.md)).
- `attach()` requires `consumer.watermark == store.last_tid`.
  Deltas are not retained, so a consumer that is behind, or ahead after a restore from backup, rebuilds from a snapshot.
- Updates carry the record's prior payload, so index-shaped consumers un-index old values without reading the store, and `store.delete()` emits tombstones through the same channel.
- A consumer that raises is detached with a `ConsumerDetachedWarning`; the commit stays durable and the sidecar rebuilds and re-attaches.
- To write a consumer, implement `dc.DeltaConsumer` (a `watermark` property plus `apply(delta)`) and certify it with `datacrystal.testing.check_delta_consumer(factory, content=...)`.
  `CountingConsumer` is the minimal reference, and [search](search.md) and [analytics](analytics.md) are the shipping examples.
- `datacrystal.deltalog` is the consumer that ships with core, with no extra and no third-party dependency.
  It appends every delta byte for byte in TID order, so the store gains an audit history and the base for time travel by replay and follower catch-up.
- It is opt-in and records only from the moment you attach it: attach to a fresh store (`watermark == 0`) for the complete history, or use `DeltaLog.bootstrap(path, snapshot)` mid-life to record from the join point on.
  A full-state checkpoint that would make a mid-life log self-contained is `[planned — demand-driven]`.
- `log.replayed_state()` reconstructs the exact committed state of a log attached at `watermark == 0`.
- Each flush fsyncs the segment bytes before it commits the `manifest.json` watermark, and a reopen truncates a partial append, so the log is always a gapless commit prefix.
  `flush_every=1` (default) is as durable as the store; `flush_every=N` trails by up to N−1 commits and a crash in that window means a rebuild.
- Retention is yours: segments roll at `max_segment_bytes` and pruning is a deliberate operator choice.
  A log directory has one owner process.

Reference: [Snapshots](../reference.md#snapshots), [The commit-delta pipeline](../reference.md#the-commit-delta-pipeline).
Cross-thread reads rest on [ADR-002](../design/ADR-002-storage-read-views.md).
