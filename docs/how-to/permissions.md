# How-to: restrict who can read a record

After this page you can protect a class, share a record with a team and see a denied read come back absent or as a masked twin.

```python
from typing import Annotated

import datacrystal as dc

TEAM = 5

@dc.entity(protected=True)
class Specimen:
    label: Annotated[str, dc.Unique]
    note: str = ""

@dc.entity
class FieldNote:
    tag: Annotated[str, dc.Unique]
    about: dc.Lazy[Specimen] | None = None

curator = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
agent = dc.Principal(uid=3, memberships={TEAM: dc.AGENT})
outsider = dc.Principal(uid=4, memberships={})
root = dc.root_principal(uid=99)

store = dc.Store.open("cabinet.store")
with store.acting_as(root):
    if store.root is None:
        store.root = {"seeded": True}
        store.commit()

with store.acting_as(curator):
    if store.get(Specimen, label="Q43010") is None:
        spec = Specimen(label="Q43010", note="type locality, verified")
        store.store(spec)
        dc.share(spec, TEAM, read=dc.VIEWER, write=dc.AGENT)
        store.store(FieldNote(tag="note-1", about=dc.Lazy.of(spec)))
        store.commit()

with store.acting_as(agent):
    print("agent sees:", store.get(Specimen, label="Q43010") is not None)

with store.acting_as(outsider):
    print("outsider sees:", store.get(Specimen, label="Q43010") is not None)
    twin = store.get(FieldNote, tag="note-1").about.get()
    print(isinstance(twin, Specimen), isinstance(twin, dc.Redacted), bool(twin))
    try:
        twin.note
    except dc.ReadDeniedError:
        print("reading a masked twin raises")

with store.acting_as(root):
    print("root sees:", store.get(Specimen, label="Q43010") is not None)
store.close()
```

- A freshly stored protected record is owner-only until you share it; there is no silent default grant.
  `dc.share(record, group, read=..., write=...)` always names both levels.
- A discovery surface answers "what is out there": `get`, `get_many(cls, key=...)`, `query`, `query_iter`, `count`, `pluck`, `explain` and `incoming`.
  A denied row is absent from all of them, never raises, and counts do not include it, because a visible drop would itself leak existence.
- A deref surface answers "resolve what I already hold": `Lazy.get()` and `get_many` over OIDs or handles.
  A denial returns a `dc.Redacted` twin: `isinstance(twin, Specimen)` holds and `twin.typename` reads, while reading a field, `dc_permissions`, or trying to `store()`, `mark_dirty()`, `delete()`, `upsert()` or share it raises `ReadDeniedError`.
  A later deref by a principal who can read gets the real, same-identity instance back.
- Root is `dc.root_principal(uid=...)`, shorthand for `Principal(uid=..., memberships={dc.WORLD: dc.EXECUTIVE})`.
  `WORLD` is the group every principal is implicitly in and `EXECUTIVE` is the level, so holding the top level in the world group is what makes root; it does not give the world executive rights.
  Every root action is stamped under its uid in the delta log.
- Sharing to the world is the opposite direction: `dc.share(rec, dc.WORLD, read=dc.VIEWER)` makes a record public.

## What the fence does and does not cover

- The read fence is live on every per-record surface: the live store, snapshots, the `datacrystal[web]` REST and GraphQL tier, `datacrystal[fts]` hits and blobs.
- `store.snapshot(principal=...)` binds one acting principal: `snap.get` raises `ReadDeniedError` on a denied row, `snap.get_many` returns a redacted twin, `query`, `count`, `all`, `explain` and `incoming` filter, and `snap.index_bitmaps()` raises on a protected class.
- Web requests are fenced by their resolved principal; a denied reference renders as wire-null.
- `search(query, snapshot=...)` post-filters ranked hits, and an index over a protected class refuses an unfenced `search()`.
- `store.open_blob`, `snapshot.open_blob` and `BlobHandle.bytes()` re-gate under the current principal.
- `tests/extras/test_read_fence_capstone.py` runs one protected record through every one of these surfaces.
- Four surfaces emit a full plaintext copy and are not row-fenced: the `datacrystal[arrow]` mirror, the retained delta log, the federation `/v1/deltas` feed and the private mirror-bootstrap iterator `Snapshot._stream`.
  A mirror of protected data is protected data, so they are guarded by placement under the store root, by root-binding (`Snapshot._stream` requires a root-bound snapshot for a protected class) and by the audit trail.
- The labels are an in-band contract, not cryptography: protect the store's files as you would without them ([explanation](../explanation.md#permissions-read-floors-write-floors-and-the-masked-deref)).

Reference: [Protecting records](../reference.md#protecting-records-protectedtrue).
Decisions: [ADR-008](../design/ADR-008-permissions-contract.md).
