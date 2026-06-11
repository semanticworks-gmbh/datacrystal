# ADR-001: Concurrency contract — owner-thread/loop confinement (Option D)

Date: 2026-06-10
Status: **Accepted**

## Context

The stress test ([STRESS-TEST.md](STRESS-TEST.md)) identified five races any contract must answer:
(a) commit-time serialization racing mutator threads → persisted cross-object states that never
existed in memory; (b) the dirty-tracking hook-flip window → permanently lost updates; (c) the
LazyReferenceManager daemon demoting objects while app threads read → use-after-clear even in
"single-threaded" apps; (d) the single shared identity map forecloses ZODB-style session copies
(one-way door); (e) the async facade + concurrent requests against a shared graph.

Four options were developed in full and judged ([CONCURRENCY-OPTIONS.md](CONCURRENCY-OPTIONS.md)):
A — unsynchronized core + locking helpers (EclipseStore-literal); B — shared graph + commit write
barrier with `db.mutate()` scopes; C — session-scoped views with optimistic concurrency
(ZODB-style); D — owner-thread/loop confinement (Realm/sqlite3/JS model).

## Decision

**Option D.** The store binds to one owner thread (or event loop, via `aopen()`) at open. All
live-graph access — reads, writes, `Lazy.get()`, queries, `store()`, `commit()` — happens on the
owner. Foreign threads get `WrongThreadError` at API boundaries and interact via
`store.submit(fn)` (command → Future; returning a live entity raises `EntityEscapeError`) and
`store.snapshot()` (immutable watermark views, safe from any thread). Races (a), (b), (c), (e)
are closed by construction; D is also the only option with net-negative complexity (the
per-object demotion-lock protocol becomes unnecessary) and the only one that rescues class-swap
ghosts on both GIL and free-threaded builds.

Two riders (principled hybrid — the layers are orthogonal):

1. **From B:** the FastAPI/strawberry integration ships `async with store.transaction()`
   (an asyncio.Lock) by default — confinement gives memory safety, not request isolation;
   cross-await interleaving is answered by scopes.
2. **From C:** the snapshot/watermark read path is promoted into v0.1 (Realm's frozen-objects
   lesson: the pressure valve cannot be a retrofit).

### Bound sub-decisions (the "decide now" set)

1. **Binding semantics**: owner = the opening thread; `aopen()` binds the loop. Error taxonomy:
   `WrongThreadError`, `EntityEscapeError`. Production guards only at API boundaries (~35 ns);
   `debug=True` keeps hooks permanently armed and checks ghosts' `__getattribute__`.
2. **Three-phase commit**: P1 on-owner, await-free capture + msgspec encode; P2 off-loop I/O on
   bytes only; P3 on-owner flip-only-the-captured-set + hook re-arm + watermark bump. Writes
   racing P2 re-dirty and land in the next commit. The buffer-until-commit storer is built in
   this shape from its first version.
3. **Daemon principle**: the LazyReferenceManager NEVER touches the live graph from a foreign
   thread — loop task in async mode, sentinel-posts + owner-side piggyback in sync mode.
   (Required under every option; race (c) bites even single-threaded apps.)
4. **Snapshots in v0.1**: minimal immutable views (frozen-DTO record reads + frozen roaring
   bitmaps) at commit watermarks; full Arrow columnar mirrors remain v1.
5. **Identity-map door closed knowingly**: one shared WeakValueDictionary; writable per-session
   copies are foreclosed. Read-only/historical sessions (`session(before=watermark)`) remain
   additive on the commit-delta/watermark pipeline.
6. **asyncio doctrine, documented from day one**: a critical section is the code between awaits —
   mutate + commit-P1 with no `await` between, or use `store.transaction()`.

## Consequences

Closed forever: cross-thread access to live entities; mutate-from-any-thread shared graph;
transparent any-thread `Lazy.get()`; writable per-session copies.

Held open (all additive): Realm-style MVCC per-thread read views; actor mode (store spawns its
own owner thread); out-of-process server (command transport swap, ZEO/Redis shape); multi-store
per tenant/agent; read-only and historical sessions; chunked commit; a stop-the-world barrier
mode behind the same entry-point gates.

Distribution is unaffected by this choice: multi-process and multi-node are a replication layer
(exactly one writer + N watermark-fed readers + command fan-in), identical under every contract
option — see [SCALING.md](SCALING.md).

## Dissent recorded

The strongest counter-argument favored C on timing: pre-v0.1 is the only moment the shared
identity-map door reverses for free, and the secondary persona (FastAPI) knows the session model
from SQLAlchemy. Accepted anyway: a solo maintainer cannot afford C's concept count and
conflict/retry failure classes to serve a primary persona that is single-threaded by nature, and
C's ambient-session mode is operationally D with extra machinery underneath. If agent platforms
trend multi-tenant/server-ward, the actor→server door (command transport swap) is the escape
hatch, not a session rewrite.
