# How-to: run a coordinator and edge followers

After this page you can serve one writer over HTTP and read and write through a local replica at the edge.

```bash
pip install 'datacrystal[web]' uvicorn      # coordinator
pip install 'datacrystal[follower]'         # follower
```

Save the coordinator as `coordinator.py` and run it with `python coordinator.py`.

```python
from typing import Annotated

import uvicorn
from fastapi import FastAPI

import datacrystal as dc
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    mohs: float | None = None

store = dc.Store.open("coordinator.store")
log = DeltaLog("coordinator.log")
store.attach(log)
if store.root is None:
    store.root = {"seeded": True}
    store.store(Mineral(qid="Q1", name="quartz", mohs=7.0))
    store.commit()

app = FastAPI()
app.include_router(federation_router(store, log))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, workers=1)
```

Save the follower as `follower.py` and run it with `python follower.py` while the coordinator runs.

```python
from typing import Annotated

import datacrystal as dc

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    mohs: float | None = None

edge = dc.Store.follower("http://127.0.0.1:8000")
print(edge.get(Mineral, qid="Q1").mohs)

for txn in edge.committing(retries=5):
    with txn:
        quartz = edge.get(Mineral, qid="Q1")
        quartz.mohs = (quartz.mohs or 0) + 0.5

edge.upsert(Mineral(qid="Q3", name="topaz", mohs=8.0))
edge.commit()
print(edge.sync(), edge.get(Mineral, qid="Q3").name, edge.get(Mineral, qid="Q1").mohs)
edge.close()
```

- Both scripts define the same `@entity` classes and run as scripts, because a follower resolves only types that exist under the same module path.
- The wire is three HTTP shapes under `/v1`: `GET /v1/head` (watermark probe), `GET /v1/deltas?after=<tid>` (the COMMIT-DELTA-v2 change-feed) and `POST /v1/submit` (contribute); the spec is [FEDERATION-WIRE-v1](../design/FEDERATION-WIRE-v1.md).
- Run the coordinator with one worker.
  The store is single-writer (the lease lock), and `POST /v1/submit` fans contributions onto the owner thread, so the store must be opened on the thread that runs the event loop; followers carry the read load ([deployment doctrine](web-deployment.md#the-deployment-doctrine--and-why-each-rule-holds)).
- The router ships no auth and binds nothing, so an exposed bind is open and writable until you add one.
  Pass `dependencies=[Depends(check_key)]` to `federation_router(store, log, ...)` to guard every route, and put a reverse proxy in front for TLS, body-size and rate limits, because `/v1/submit` batches are unbounded and `GET /v1/deltas?after=0` materializes the whole retained history on the event loop.
- `Store.follower(url, api_key=..., path=...)` replays the change-feed from TID 0 into a real local store, in memory by default and on disk with a `path`; `dc.open_follower(...)` is the function form.
  Reads then hit the local store with no round-trips.
- `edge.sync()` pulls the coordinator's deltas after the local watermark and applies them in place.
  A gap raises `DeltaGapError` (resync from 0) and a seen delta is a no-op.
  `sync()` is follower-only and refuses to run while local writes are buffered.
- On a follower, `commit()` fans the buffered entities into `POST /v1/submit` and then syncs, so the follower reads its own write.
  Each contributed entity needs a `dc.Unique` natural key, which makes a re-sent insert merge instead of duplicating.
- An update carries an OCC base token, the hash of the payload it was read at.
  If the entity has moved, `commit()` raises `ConflictError` with the buffer intact.
- `store.committing(retries=N)` is the same read-modify-write loop on a single node and a follower: on a conflict it discards, syncs, re-runs your block against the winning value and commits.
  Keep the whole read-modify-write inside the `with` block and do not call `commit()` yourself.
  `retries=0` makes a single strict attempt.
- By hand, `discard()` drops the buffered writes and re-reads committed state, so re-`get()` any reference you held across it.
- `SchemaSkewError` on contribute means the follower sent a field the coordinator's class lacks; upgrade the coordinator or drop the field.
- A reference field crosses the wire as the referent's coordinator OID, so a contribution may reference any entity already committed on the coordinator; after another follower syncs, it resolves to the same entity.

## Limits that fail loudly

- Contribute is upsert-only: a buffered `delete()` raises `NotImplementedError` on `commit()`, so delete on the coordinator.
- A reference to an entity created in the same batch (new to new) raises `NotImplementedError`; commit the referenced entity first.
- An entity with a `dc.Blob` field raises `NotImplementedError` on contribute; write it on the coordinator.
- Only scalar references and list edges (`list[Lazy[T]]`, `list[T]`) federate; an entity nested in a bare `list` or `dict` raises `NotImplementedError`.
- Protected classes do not federate yet.
  A contribution commits as the anonymous principal, which can neither create protected records nor clear a write floor, so the coordinator refuses any op on a `protected=True` class with 403 `{"error": "write-denied"}` before buffering ([ADR-008](../design/ADR-008-permissions-contract.md) R16).

Reference: [the web reflection API](../reference.md#datacrystalweb-reflection-api).
Why a follower is a real store: [explanation](../explanation.md#why-followers-are-real-local-stores-the-fractal-contract).
A coordinator that ticks a counter, for watching changes arrive: [examples/federation](../../examples/federation/README.md).
