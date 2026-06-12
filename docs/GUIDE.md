# datacrystal user guide

This guide documents what **exists today** (`0.1.0.dev0`, 2026-06). Everything that does not
exist yet is explicitly marked **`[planned — …]`** with its place on the
[roadmap](design/ROADMAP.md); nothing here describes vapor as if it were real. The public API
freezes at the v0.1.0 tag.

- [Open a store](#open-a-store)
- [Define entities](#define-entities)
- [The root](#the-root)
- [Writing: mutate, then commit](#writing-mutate-then-commit)
- [Lists and dicts inside entities](#lists-and-dicts-inside-entities)
- [What can be persisted](#what-can-be-persisted)
- [Reading: get, query, lazy references](#reading-get-query-lazy-references)
- [Identity and memory](#identity-and-memory)
- [Big data: keeping memory bounded](#big-data-keeping-memory-bounded)
- [Schema evolution](#schema-evolution)
- [Frozen entities](#frozen-entities)
- [Concurrency and deployment](#concurrency-and-deployment)
- [Durability and crash safety](#durability-and-crash-safety)
- [Errors](#errors)
- [Planned features and when they land](#planned-features-and-when-they-land)

## Open a store

```python
import datacrystal as dc

store = dc.Store.open("cabinet.store")        # a directory; created if needed
...
store.close()                                  # or: with dc.Store.open(...) as store:
```

`Store.open(path, *, durability="interval", lock_ttl=10.0, debug=False, lazy_timeout=None)`
(async: `await dc.aopen(...)`, same keywords — see
[Concurrency and deployment](#concurrency-and-deployment)):

- The directory holds `data.sqlite` (records as msgpack blobs, riding SQLite's journal) and
  `used.lock`, the **single-writer lease**: a second process opening the same store gets a loud
  `StoreLockedError` instead of silent corruption.
- `durability` is a triad. `"commit"` fsyncs every commit (plus `F_FULLFSYNC` on macOS —
  honest, so a commit costs ~4 ms there); an acked commit survives even power loss.
  `"interval"` (default) group-commits: fsync happens at WAL checkpoints, so a **process**
  crash (kill -9) loses nothing, while an OS crash or power loss may lose the last commits —
  but never corrupts the file. `"never"` skips fsync entirely: benchmarks and scratch stores
  only, an OS crash can corrupt it.
- `close()` **discards uncommitted changes** — commit first. Closing releases the lock and the
  in-memory graph.
- `debug=True` arms the **fingerprint safety net**: every commit re-encodes the live CLEAN
  entities and, for any that changed without the dirty tracking noticing (e.g. an
  `object.__setattr__` bypass, or in-place mutation of a `bytearray`), emits an
  `UntrackedMutationWarning` **and commits the change anyway** — detection plus rescue. It
  costs O(live entities) per commit; use it in development and tests.
- `lazy_timeout=<seconds>` enables the **LazyReferenceManager** — see
  [Identity and memory](#identity-and-memory).

## Define entities

An entity is a typed Python class; the decorator turns it into a slots dataclass and registers
it with the engine:

```python
from dataclasses import field
from typing import Annotated
import datacrystal as dc

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]                 # unique secondary key → store.get()
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None   # bitmap-indexed → store.query()
    mohs: float | None = None
    type_locality: dc.Lazy["Locality"] | None = None         # lazy reference
    tags: list = field(default_factory=list)
```

- `@dc.entity` applies `@dataclass(slots=True, weakref_slot=True, eq=False)`. Entity equality
  is **identity** — there is exactly one live instance per stored object.
- Field markers go inside `typing.Annotated`:
  - `dc.Index` — adds the field to the roaring-bitmap indexes; `==` and `.in_()` queries on it
    answer from bitmaps. Index/Unique fields must be scalar (`str | int | float | bool`,
    optionally `| None`).
  - `dc.Unique` — unique secondary key (e.g. URIs, slugs, external ids). Duplicates are
    rejected at commit (`UniqueViolationError`); `None` never collides (SQL-NULL-style).
  - `dc.FullText` — **inert in the core engine**: it reserves the field for
    `datacrystal[fts]` `[planned — after M4, see below]`. External consumers can already
    read it (the M3 FTS5 contract spike derives its indexing config from these markers).
- `@dc.entity(frozen=True)` declares an append-only record — see [Frozen entities](#frozen-entities).
- Entity classes are identified by `module:qualname` in the store. Keep an entity class
  importable under the same module path, or opening old data raises `UnregisteredTypeError`.

## The root

`store.root` is the entry point of the graph — **any persistable value**: an entity, a list, a
dict (including an empty one), even a scalar. It is `None` until you assign it; that is the
first-run check:

```python
store = dc.Store.open("cabinet.store")
if store.root is None:          # first run only
    store.root = {"minerals": [], "runs": 0}
store.root["minerals"].append(Mineral(qid="Q43010", name="quartz"))
store.commit()
```

- Everything reachable from the root is **pinned in memory and identity-stable**:
  `store.root is store.root` always holds, no strong reference of your own required. (Versions
  before 2026-06-11 had a bug here — the root graph could be garbage-collected and silently
  rehydrated. Fixed; you do not need to hold your own reference anymore.)
- `dc.Lazy[T]` is the explicit cut point where pinning (and loading, and memory) stops — use it
  for the parts of the graph that should not live in RAM permanently.
- Assigning `store.root = value` captures the value immediately: new entities in it are
  registered, and lists/dicts come back from `store.root` as tracked containers.

**"Multiple roots?"** There is exactly one root by design — but it can be a dict, which *is*
the named-roots pattern:

```python
store.root = {"minerals": [], "settings": {}, "log": []}     # plain dict as named roots
```

An entrypoint entity works just as well and gives you attribute access and types:

```python
@dc.entity
class Cabinet:
    minerals: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)

store.root = Cabinet()
```

Both are equivalent to the engine; pick by taste. And note that entities do **not** have to
hang off the root at all — see the next-but-one section.

## Writing: mutate, then commit

datacrystal buffers until commit — there is no session object, no `save()`, no dirty flag to
set:

```python
quartz = store.get(Mineral, qid="Q43010")
quartz.crystal_system = "trigonal"     # attribute write → tracked
quartz.tags.append("display-case")     # in-place container mutation → tracked
tid = store.commit()                   # atomically persists everything buffered
```

- `store.commit()` returns the new commit id (`int`), or `None` if nothing changed. A commit is
  atomic: after a crash you see an exact prefix of your commits, never a torn one.
- New entities are discovered automatically through reachability: assign them into the graph
  (or `store.root`) and commit. For a graph **not** reachable from the root, register its top
  explicitly with `store.store(obj)`.
- `store.mark_dirty(obj)` exists as an escape hatch but is rarely needed — attribute writes and
  in-place list/dict mutation are both tracked.
- `store.last_tid` is the current commit watermark.

## Lists and dicts inside entities

Every `list`/`dict` that enters an entity field (or the root) is wrapped in an owner-bound
`dc.PersistentList`/`dc.PersistentDict`. They behave like normal lists/dicts, plus:

- any mutation (`append`, `__setitem__`, `update`, `sort`, …) marks the owning entity dirty;
- mutating a container that belongs to a frozen entity raises `FrozenEntityError`;
- a container keeps its owning entity alive — holding just `e.tags` is enough to commit through it.

One semantic to know: **assignment copies.** Containers are by-value parts of their owner —
after `e.tags = data`, mutate through `e.tags`; later changes to the original `data` object do
not reach the entity. (They also round-trip by value: two entities sharing one list become two
lists after reopen.)

## What can be persisted

| Value | Persisted as |
|---|---|
| `None`, `bool`, `int`, `float`, `str`, `bytes` | itself (msgpack) |
| timezone-aware `datetime` | itself (msgpack timestamp) |
| naive `datetime`, `date`, `time`, `timedelta` | **as ISO strings** — they come back as `str` in v0.1; store aware datetimes, or convert at the edge |
| `list`, `dict` | by value; nested fine; dict keys must be scalars |
| `tuple` | **as a list** — it comes back as a list (msgpack has no tuple type) |
| `@dc.entity` instance | by reference (8-byte OID), eagerly loaded on access |
| `dc.Lazy[T]` | by reference, loaded on first `.get()` |
| plain (non-entity) dataclass | **rejected loudly** — it would round-trip as a dict; make it an entity |
| `set` / `frozenset` | **rejected loudly** — use a list (v0.1) |

There is no pickle anywhere: records are msgspec msgpack, and decoding is structurally
incapable of executing code.

## Reading: get, query, lazy references

```python
# unique-key lookup (exactly one Unique field as keyword)
azurite = store.get(Mineral, qid="Q193563")          # entity or None

# bitmap-indexed queries with a composable condition AST
hits = store.query(
    (Mineral.crystal_system == "monoclinic") & (Mineral.mohs >= 3.0)
)

# batch hydration — N+1 is never your problem
minerals = store.get_many(oids_or_lazies_or_entities)   # one storage round-trip

# lazy references
ref = azurite.type_locality      # dc.Lazy[Locality]
ref.loaded                       # False — nothing fetched yet
ref.get()                        # loads now (and caches): the Locality
ref.peek()                       # the target if loaded, else None — never loads
```

Query semantics:

- Operators on class-level fields: `==`, `!=`, `<`, `<=`, `>`, `>=`, `.in_([...])`; combine
  with `&`, `|`, `~`. **Parenthesize predicates** — `&` binds tighter than `==` (you get a
  helpful `QueryError` if you forget).
- `==` and `.in_()` on `dc.Index` fields answer from roaring bitmaps; all other predicates run
  as a Python residual over the bitmap candidates. Ordering comparisons never match `None`.
- A condition uses fields of **one entity class** — cross-entity joins are
  `[planned — v1, on Arrow mirrors]`.
- `query()` and `get()` reflect **committed** state; uncommitted buffered changes are not
  visible to them.
- Type checkers cannot model the magic class-attribute access (they see `Mineral.mohs` as
  `float | None` and flag the comparison). Runtime is fine either way; for checker-clean code
  use the equivalent typed proxy:

  ```python
  M = dc.fields(Mineral)
  hits = store.query((M.crystal_system == "cubic") & (M.mohs >= 6.0))
  ```

## Identity and memory

- One live instance per stored object: every path to an entity yields the same Python object,
  cycles included (`a.peer.peer is a`). Identity holds for as long as the object is alive.
- The root-reachable graph is pinned (see [The root](#the-root)). Entities **not** reachable
  from the root (query results, lazily loaded subgraphs) are collectable as soon as you drop
  them — and rehydrate transparently on next access.
- Rule of thumb for big datasets: structure the hot path as eager references and put `dc.Lazy`
  on the cold edges; the eager part is your RAM budget (~600 B/object envelope).

## Big data: keeping memory bounded

A dataset larger than RAM works today **if you keep it off the pinned root**. Three patterns,
in order of leverage:

1. **Free-floating entities.** Entities do not need to be reachable from the root: register
   them with `store.store(obj)`, commit, drop your references — they are collected, and come
   back on demand via `store.get()` (unique key) or `store.query()`. Memory is bounded by what
   you currently hold plus your query results, not by the dataset.

   ```python
   for batch in read_csv_in_batches(path):           # streaming ingest
       for row in batch:
           store.store(Specimen(**row))
       store.commit()                                # batch is collectable after this
   ```

2. **`dc.Lazy` cut points.** Anything eagerly reachable from the root is pinned; a `dc.Lazy`
   edge stops both the pinning and the loading. Keep the root small (ids, settings, hot
   objects) and reach the bulk through unique keys, queries, or lazy edges.

3. **Index-friendly queries.** `==`/`.in_()` on `dc.Index` fields answer from bitmaps and
   hydrate only the hits. Any other predicate (`>=`, `!=`, …) runs as a Python residual that
   **hydrates the whole extent** of the class — on a million-object class that is a full
   table scan with a matching RAM spike (the objects are collectable again afterwards, but
   the peak is real). Until Arrow mirrors land `[planned — v1]`, design hot filters as
   `dc.Index` equality facets.

Measured (M2 dev machine, SQLite backend, 300k objects ≈ 29 MB on disk): streaming ingest
3.4 s peaking at ~750 B/object RSS with **zero** entities left live; warm bitmap query
hydrating 75k hits 0.34 s; full-scan residual query +165 MB transient; unique-key `get()`
0.1 ms hydrating exactly one object. CI now gates these properties (peak-RSS byte budget,
results collectable, `get()` hydrates one — `tests/fitness/test_memory_bounded.py`).

Loaded `Lazy` references stay loaded until you drop the holder — **or** you open the store
with `lazy_timeout=<seconds>`: the LazyReferenceManager then demotes handles idle past the
timeout back to unloaded, releasing the subgraph behind the cut point (the next `.get()`
transparently reloads, identity preserved). Demotion only ever runs on the owner — as a
piggyback on your own store calls (sync), or as an owner-loop task (`aopen`). Timeout-only in
v0.1; an RSS-quota variant is deferred (psutil stays out of core), and a hard per-store memory
cap does not exist. For analytics-style scans, the honest answer is the Arrow/DuckDB tier
`[planned — v1]`, not the object graph.

## Schema evolution

You can evolve entity classes between runs; old records adapt **on load**:

| Change | What happens |
|---|---|
| add a field **with a default** | old records get the default when loaded ✔ |
| remove a field | old values are ignored ✔ |
| reorder fields | values map by name ✔ |
| add a field **without a default** | `SchemaMismatchError` naming the field — add a default |
| add a `dc.Unique` field | must default to `None`, else `SchemaMismatchError` (a shared non-None default would collide) |
| rename a field | seen as **remove + add**: the old values are dropped — see below |
| change a field's type | not checked (annotations are not validated on load) — avoid, or migrate |

To "rename" without losing data, migrate explicitly: keep the old field, add the new one with a
default, copy values over, commit, then remove the old field. Guided migrations are
`[planned — post-v0.1]`.

How it works (one paragraph, so the behavior is predictable): the store keeps a **type
lineage** — every field shape a class ever had gets its own row in the type dictionary, and
each record decodes through the shape it was written with, by field name. Old records are never
rewritten in place; they migrate to the newest shape the next time you modify and commit them.
A store that used schema evolution is still openable by this and any newer library version.

## Frozen entities

```python
@dc.entity(frozen=True)
class CatalogEvent:              # append-only: event logs, provenance, audit trails
    specimen_no: str
    kind: str
    note: str
```

Construct and commit them; afterwards any mutation — attribute write **or** in-place container
mutation — raises `FrozenEntityError`. Dirty tracking never arms for them, which also makes
them the cheapest records to commit in bulk.

## Concurrency and deployment

The contract ([ADR-001](design/ADR-001-concurrency-contract.md)) is **owner confinement**: a
store and its live graph belong to the thread that opened the store.

- Touching live entities or the store from another thread raises `WrongThreadError` —
  immediately and loudly, never corrupting.
- One writing process per store, enforced by the lease lock. Notably: `uvicorn --workers 4`
  means four processes — run datacrystal apps with **workers = 1** (how that scales anyway:
  [SCALING.md](design/SCALING.md)).
- If the OS pauses your process long enough for the lease to expire and be taken over, the next
  commit raises `LeaseLostError` instead of risking two writers.
- Foreign threads **ship work to the owner** instead of touching the graph:
  `store.submit(fn)` returns a `concurrent.futures.Future`. The owner runs pending
  submissions whenever it next calls into the store, or explicitly via
  `store.run_pending()`; under `aopen()` the event loop is woken instead, no owner call
  needed. Submission results must be plain data — a live entity in the result (even nested
  in a list/dict, or behind a `Lazy`) fails the future with `EntityEscapeError`.
- `store.commit()` itself is three-phase: it captures and encodes on the owner, hands the
  bytes to a dedicated IO worker thread, and finalizes on the owner. For sync stores that is
  an internal detail (commit blocks as before); for async stores it is what keeps the loop
  free.
- `store.snapshot()` gives ANY thread a frozen, read-only view of committed state — see
  [Snapshots and the commit-delta pipeline](#snapshots-and-the-commit-delta-pipeline).

### asyncio

```python
store = await dc.aopen("cabinet.store")     # binds the store to the running loop

async with store.transaction():             # an asyncio.Lock scope per unit of work
    store.root.entries.append(entry)        # … no other transaction interleaves …
# the scope committed on clean exit

await store.commit()                        # or commit explicitly outside scopes
store.close()
```

- Every task on the owning loop may touch the graph (one thread by construction); foreign
  threads still get `WrongThreadError`.
- `await store.commit()` captures **before its first await**, then applies off-loop while the
  loop keeps serving. A task that mutates an entity while a commit is in flight is safe by
  contract: the write re-dirties the entity and lands in the *next* commit. Concurrent
  `commit()` calls serialize on an internal lock.
- The asyncio doctrine, documented from day one (ADR-001): **a critical section is the code
  between awaits.** Mutate-and-commit with no `await` in between, or wrap the scope in
  `transaction()`. An exception inside `transaction()` commits nothing; the in-memory
  mutations stay buffered (live objects have no rollback) — handle the exception and decide:
  fix and commit, or close to discard.
- Hydration faults (`Lazy.get()`, queries) load synchronously on the loop — the explicit
  `Lazy[T]` cut points make where that can happen visible in your model.

## Snapshots and the commit-delta pipeline

### `store.snapshot()` — reading from any thread

A snapshot is a frozen view of the committed state at one commit watermark, and the
sanctioned way for worker threads to read while the owner keeps writing (ADR-001 rider 2):

```python
def report(store: dc.Store) -> int:        # runs on any thread
    with store.snapshot() as snap:         # pins one durable commit boundary
        fine = [s for s in snap.all(Specimen) if s.quality == "fine"]
        return len(fine)
```

- `snap.get(oid_or_ref)`, `snap.all(EntityClass)` and `snap.root` return **immutable
  views** (`dc.EntityView`): field access mirrors the live class, entity references are
  explicit `dc.Ref` tokens you resolve via `snap.get(ref)`, lists come back as tuples,
  dicts as read-only mappings. Never live entities — nothing a worker thread does with a
  snapshot can violate confinement or dirty tracking.
- `snap.tid` is the pinned watermark; `snap.types` is the type lineage at that watermark
  (what a delta consumer needs to bootstrap, see below).
- Close promptly (use the context manager): on the sqlite backend an open snapshot holds a
  WAL read transaction, which blocks checkpoint truncation.
- A snapshot taken while a commit is mid-flight may be one commit **ahead** of
  `store.last_tid` — the commit it sees is already durable; views are never torn.

### The commit-delta pipeline — what sidecars ride on

Every commit is describable as one versioned, msgpack-encodable **delta** — the public
[COMMIT-DELTA-v1](design/COMMIT-DELTA-v1.md) contract (DRAFT until the v0.1.0 tag). Attach
a consumer and every commit hands it exactly one delta, in TID order, on the owner thread,
strictly after the commit is durable:

```python
with store.snapshot() as snap:                  # 1. bootstrap at a watermark
    consumer = MySidecar.bootstrap(snap)        #    (lineage + state + watermark)
store.attach(consumer)                          # 2. ride the stream from there
```

- `attach()` requires `consumer.watermark == store.last_tid`: deltas are **not retained**,
  a consumer that is behind (or ahead — a store restored from backup) must rebuild from a
  snapshot. This is by design: sidecars are rebuildable derived data, always.
- Update/delete ops carry the record's **prior payload**, so index-shaped consumers
  un-index old values without ever reading the store.
- A consumer that raises is **detached** with a `ConsumerDetachedWarning` — the commit
  stays durable, the store stays healthy, the sidecar rebuilds and re-attaches.
- Writing a consumer? `datacrystal.testing.check_delta_consumer(factory, content=...)`
  certifies it against every contract obligation (idempotency, ordering, gap/version
  refusal, prior-based un-indexing); `datacrystal.testing.CountingConsumer` is the
  minimal reference implementation, `datacrystal/contract/applier.py` the normative one.

## Durability and crash safety

- A commit is one SQLite transaction; `durability="commit"` makes it fsync-durable per commit,
  the default `"interval"` group-commits at WAL checkpoints (see [Open a store](#open-a-store)
  for the triad's exact loss windows).
- Kill -9 mid-commit, power loss, OS crash: on reopen you get exactly a committed prefix —
  never a torn commit. Under `"commit"` that prefix is *every acked commit*; under
  `"interval"` an OS crash may trim the tail. This is CI-gated (the SIGKILL crash test runs
  under `"commit"`) and was true from the first walking skeleton.
- Backup: close the store (or pause writing) and copy the directory.
  `sqlite3.backup`/Litestream PITR recipes are `[planned — docs, v0.x]`.
- Opening a store written by a **newer** library version raises `NewerStoreError` naming both
  versions — never a misread.

## Errors

Everything derives from `dc.DataCrystalError`:

| Error | Meaning |
|---|---|
| `StoreClosedError` | operation on a closed store |
| `StoreLockedError` | another live process holds the lease |
| `LeaseLostError` | this process lost the lease (paused too long); refusing to write |
| `WrongThreadError` | live entity/store touched from a foreign thread |
| `EntityEscapeError` | a `submit()` result tried to carry a live entity across the owner boundary |
| `FrozenEntityError` | mutation of an `@entity(frozen=True)` record |
| `NotAnEntityError` | a non-entity where an entity is required |
| `UniqueViolationError` | duplicate value for a `dc.Unique` field in a commit |
| `SchemaMismatchError` | a class change beyond additive evolution (see [Schema evolution](#schema-evolution)) |
| `UnregisteredTypeError` | store has records of a class not imported in this process |
| `NewerStoreError` | store written by a newer format version |
| `CorruptRecordError` | a record failed its checksum — the file is damaged |
| `QueryError` | malformed condition (two classes mixed, missing parentheses, …) |

Pipeline consumers can additionally raise the contract errors (`datacrystal.contract`):
`DeltaGapError` (history missing — resync/rebuild; also raised by `attach()` on a
watermark mismatch) and `DeltaFormatError` (malformed/newer-versioned delta).

Two warnings live outside the exception family (both `UserWarning`s):
`UntrackedMutationWarning`, emitted by the `debug=True` safety net when a mutation slipped
past the dirty tracking — the entity is committed anyway; fix the write path it names —
and `ConsumerDetachedWarning`, emitted when an attached delta consumer raised during
delivery and was detached (the commit is durable; rebuild the sidecar and re-attach).

## Planned features and when they land

Sequencing follows the ratified [roadmap](design/ROADMAP.md) and the
[kickoff plan](design/KICKOFF.md); "milestone" refers to the v0.1 execution plan
(M2 → M3 → M4 ≈ tag v0.1.0 + PyPI release).

| Feature | Where it lands |
|---|---|
| bitmap-index snapshot views (`Snapshot.index_bitmaps()` — the reserved slot raises until then) | M4 — current milestone |
| v0.1.0 tag: API freeze (incl. the COMMIT-DELTA-v1 lock), PyPI publication | M4 |
| **full-text search** — `datacrystal[fts]`, SQLite FTS5 over `dc.FullText` fields, BM25 ranking via `store.search()`, per-field language stemming (Snowball; German + English first-class) | next after M4: the M3 contract spike was already FTS5-shaped (`tests/contract/fts_consumer.py` is the extra's embryo), the pipeline it rides is real |
| **pandas / polars / DuckDB** — zero-copy via Arrow columnar mirrors | v1 (after v0.x hardening) |
| **GraphQL / FastAPI** — `datacrystal[web]` with strawberry integration | extension package, after the v1 core freeze |
| vector search — `datacrystal[vector]`, usearch, ≥2 vector fields per entity | extension package, after v1 |
| reverse-reference index (`incoming()`), property-graph recipes | v1 |
| sets, guided migrations, custom scalar types | demand-driven |

Until the Arrow mirrors exist, getting data into pandas is a three-liner (copies, not
zero-copy, fine for thousands of rows):

```python
import pandas as pd
rows = store.query(Mineral.crystal_system != None)   # or any entity list
df = pd.DataFrame([{"qid": m.qid, "name": m.name, "system": m.crystal_system} for m in rows])
```
