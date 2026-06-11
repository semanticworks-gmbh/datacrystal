# Roadmap (ratified 2026-06-10, after round-2 cross-examination)

This supersedes the "MVP roadmap" section in [DESIGN.md](DESIGN.md). It is the resolution of the
round-2 recommendations ([../research/2026-06-10-round2/](../research/2026-06-10-round2/)) against
the constraints: solo maintainer, single-writer, local-first persona, ~24-month honest v1.0.

Amended 2026-06-10 with the accepted [SDA-LAYERING](SDA-LAYERING.md) deltas (marked "SDA delta"
inline): frozen entities + batch hydration in item 1, unique secondary-key index in item 4,
`datacrystal[fts]` resequenced into late v0.x (item 10), `datacrystal[ledger]` punted (item 19).

Amended 2026-06-11 (owner request): git-like data branching/time-travel recorded as punted
item 20; the object-store/datalake positioning clarified under item 16.

Amended 2026-06-11, second (owner request): networked replication recorded as punted item 21
(transport-agnostic, rides item 3's contract); Zyre/pyre evaluated and declined inside it.

## Core v0.x (ordered)

1. **Object engine**: slots-dataclasses canonical form, msgspec msgpack records, WeakValueDictionary
   OID registry, tri-state dirty tracking, explicit `Lazy[T]` (class-swap ghosts stay deferred as optimization).
   Concurrency contract = **owner-thread/loop confinement per [ADR-001](ADR-001-concurrency-contract.md)**:
   owner binding at open, `WrongThreadError`/`EntityEscapeError` taxonomy, three-phase commit
   (capture on-owner → I/O off-loop → flip+re-arm on-owner), LazyReferenceManager as owner task,
   `store.submit()` for foreign threads. Includes `@entity(frozen=True)` append-only entity mode
   (dirty tracking never arms; event logs/provenance) and a batch hydration API
   (load-many-by-OID — N+1 must never be the user's problem) — SDA deltas.
2. **SQLite-as-blob-store** behind the 3-method storage protocol, plus a **single-writer
   lease-refreshed lock file with a loud error** (~1–2 days; port of EclipseStore
   `StorageLockFileManager.java`). `uvicorn --workers 4` silently corrupting is the #1 foreseeable
   user error — docs alone don't prevent it.
3. **Commit-delta/watermark pipeline specified, versioned, and tested as a PUBLIC contract.**
   It is the substrate every sidecar (FTS, vector, mirrors, reverse index, future RDF/CRDT) rides on —
   the single most load-bearing undelivered component. Idempotency + ordering semantics locked
   before any consumer ships. Includes **`store.snapshot()` minimal immutable views**
   (frozen-DTO reads + frozen roaring bitmaps at commit watermarks) — promoted into v0.x per
   ADR-001 rider 2; full Arrow mirrors remain v1. First real consumer: `datacrystal[fts]`
   (item 10), resequenced into late v0.x as this contract's validation harness — SDA delta.
4. **pyroaring bitmap indexes + Condition AST** — the differentiating query story, in the first release.
   Includes a **unique secondary-key index** (string alias → entity; lookup + upsert-by-natural-key,
   e.g. URIs/slugs/external ids) as an explicit v0.x commitment, not an implied detail — SDA delta.
5. **Format hygiene**: versioned (not frozen) custom-log record header with reserved
   sealed-flag / footer-offset / tid-watermark fields (~zero cost, preserves the whole option).
6. **Docs**: workers=1 + asyncio deployment guide incl. the asyncio doctrine ("a critical section
   is the code between awaits"); DBOS/Celery `concurrency=1` command-queue recipe for multi-process
   write fan-in; Litestream + `sqlite3.backup` PITR recipe; [SCALING.md](SCALING.md) tiers.

## Core v1

7. **Arrow columnar mirrors** + DuckDB/polars zero-copy queries.
8. **Reverse-reference index** as rebuildable Arrow sidecar on the watermark pipeline + minimal
   traversal API (`incoming()` first). Buys backlinks, orphan detection, cascade checks, impact
   analysis (~+5–15% on the 600 B/object envelope).
9. **DuckPGQ property-graph recipe** over the Arrow mirrors (docs-only, days).

## Extension packages (separate extras, after v1 core freeze)

10. `datacrystal[fts]` — SQLite FTS5 sidecar (cheapest: stdlib). **Resequenced into late v0.x**
    (SDA delta): the watermark pipeline (item 3) is the most load-bearing undelivered component
    and must not ship as a public contract with zero consumers; FTS5 is the cheapest real
    validation harness, and the first customer (SDA) needs it anyway.
11. `datacrystal[vector]` — usearch sidecar. Must support ≥2 `@Vector` fields per entity
    (SDA "Triple Sigmatics" dual embeddings — one `.usearch` file per field, as designed).
12. `datacrystal[web]` — FastAPI + strawberry GraphQL integration.
13. v1.x: **read-only snapshot readers**, scoped to open-at-watermark, no live invalidation.

## Punted — demand-driven, zero roadmap commitment

14. Custom append-only log + footer/checkpoint boot chain (only on profiling evidence from real
    workloads; SQLite-blob v0.x has no boot problem — boot index *is* the B-tree).
15. Optional Rust "turbo" wheel for log scan/compaction behind the same 3-method protocol
    (only after #14; PyO3 wheel-matrix tax until PEP 803-class ABI relief lands).
16. S3 log shipping / SlateDB-style lease-fencing extension (requires #14). **Datalake
    positioning (2026-06-11)**: the supported object-store stories *before* #14 are
    (a) Litestream replication of the SQLite file to S3 (item 6 recipe — works with today's
    backend) and (b) v1 Arrow mirrors exported as parquet-on-S3, queryable by DuckDB/polars/
    Spark-class readers (rides item 7) — that is the datalake answer. An S3-*primary* blob
    backend is pluggable behind the 3-method protocol in principle, but stays gated on #14 +
    lease fencing: per-commit PUT latency and conditional-write fencing make it wrong for the
    interactive path until the log exists.
17. `datacrystal-rdf` — term dictionary, triples Arrow sidecar, rdflib Store facade (~500 LOC),
    optional in-memory pyoxigraph as SPARQL accelerator.
18. CRDT field extension (`Annotated[Text, pyr.Crdt]`, pycrdt doc blobs; 10–100x plaintext memory
    overhead — outside the 600 B envelope by definition).
19. `datacrystal[ledger]` — hash-chained commit log + Merkle inclusion/completeness proofs as a
    Tier-3 watermark consumer (GoBD/audit/agent-provenance; SDA delta). Demand-driven; requires
    only the deterministic replayable commit deltas already promised by item 3 — never Merkle
    computation in the commit path.
20. **Git-like data branching / time-travel** (à la Omnigraph/lakeFS/Dolt; owner request
    2026-06-11). Today the SQLite-blob backend *overwrites* rows — no history is retained, so
    nothing branches "for free". The prerequisites ride already-planned work: item 3's
    deterministic, replayable commit-delta stream (if *retained*) plus item 13's
    open-at-watermark readers give **time-travel reads** and **branch-by-replay** (copy store,
    replay a prefix) at moderate cost; crude-but-real today: branch = file copy of a closed
    store, and Litestream PITR is linear time travel. Full in-store copy-on-write branching
    with merges is major new scope — per-branch identity/unique/index invariants and
    object-graph merge conflicts (adjacent to the CRDT "Never" rationale) — and systems like
    Omnigraph get branching cheap precisely because their substrate is immutable columnar
    snapshots (Lance), which for us corresponds to the v1 Arrow mirror tier, not the live
    object graph. Demand-driven; merge semantics never promised for core.
21. **Networked replication layer** — `datacrystal[replica]` (owner request 2026-06-11;
    transports evaluated in
    [2026-06-11-replication-transports.md](../research/2026-06-11-replication-transports.md)).
    The *shape* is already ratified, not new scope: exactly one writer (lease, invariant 10)
    + N read-only followers applying the [COMMIT-DELTA-v1](COMMIT-DELTA-v1.md) stream from
    their TID watermark + writes traveling as command fan-in to the writer (ADR-001
    "Consequences", the held-open actor→server door). Because the delta stream is
    deterministic, idempotent (apply-twice ≡ apply-once) and totally ordered, the durable log
    IS the writer's store; a transport only has to be an ordered, catch-up-capable pipe —
    item 3's contract is therefore both prerequisite and most of the design. When demand
    arrives: reference transport = writer-served HTTP/SSE (`GET /deltas?after=<tid>` catch-up
    + live tail; command POSTs land on `store.submit()`) — "no coordination server" falls out
    of the single writer being the distinguished node already; broker (NATS JetStream) and
    object-store (rides item 16 / Litestream) variants behind the same follower interface.
    Peer discovery is out of scope: Slurm publishes the node set, Dask's scheduler knows its
    workers, clouds have no broadcast anyway. **Zyre/pyre: evaluated and declined** — ZRE is
    LAN-broadcast discovery plus best-effort group messaging (no durable log, no replay, no
    late-joiner catch-up; a sequence gap disconnects the peer), beacons are inoperative on
    cloud VPCs, and the pyre binding has had no release since 2021. A masterless
    "all peers publish writes" queue is the multi-writer shape the Never list forecloses;
    add the sequencer it needs and it collapses back to command fan-in over any
    request/reply transport. Analytics fan-out (Dask/HPC) is explicitly NOT this item:
    compute workers want the columnar tier (items 7/16, parquet-on-S3), not replicated live
    object graphs.

## Never (all five round-2 recommendations agree, ratified)

- Rust for dirty tracking / registry / swizzling / Condition AST (PyObject-bound, FFI-dominated).
- redb / fjall / sled / RocksDB as storage (dead bindings or operational mismatch).
- CRDT as the core data model (destroys the 600 B envelope, unique indexes, ref integrity;
  Figma/Linear/post-pivot ElectricSQL all chose LWW-with-authority instead).
- Clustering / FaaS scale-out / multi-writer in core (Eclipse Data Grid took a funded company a decade).
- GIL-thread-partitioned boot scans (measured 1.5x **slower** than single-threaded).
- Homegrown SPARQL or Cypher engine.
