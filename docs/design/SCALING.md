# Scaling tiers — how single-writer confinement scales out

The concurrency contract ([ADR-001](ADR-001-concurrency-contract.md)) governs who may touch live
Python objects **inside one process**. Multi-process and multi-node are a replication layer on
top — the same layer under any contract option. Ratified patterns
(see [../research/2026-06-10-round2/distributed-serverless.md](../research/2026-06-10-round2/distributed-serverless.md)):
SQLite (Litestream/Turso), EclipseStore (Eclipse Data Grid), and SlateDB all converged on
**exactly one writer, N read-only replicas fed by log shipping, writes funneled through a queue**.

## Tier 0 — one process, many cores

Owner thread/loop owns mutation. Parallelism comes from `store.snapshot()`: immutable watermark
views (Arrow tables, frozen bitmaps) readable from any thread, crunched by DuckDB/polars/numpy
kernels that **release the GIL** — real multi-core analytics. Background threads return results
via `store.submit()`. (Redis precedent: a single-threaded command loop is not "single-core software".)
Free-threaded 3.14t widens this lane (parallel snapshot analytics); the contract itself never
leaned on the GIL.

## Tier 1 — multiple processes, one machine (`uvicorn --workers N`)

- **Default: workers=1 + asyncio.** The v0.x lease lock file turns the misconfiguration into a
  loud error instead of corruption.
- **When needed: 1 writer process + N read-only processes.** SQLite WAL already allows concurrent
  cross-process readers beside one writer at the file level; reader processes open the store
  read-only at a commit watermark and refresh at watermarks (v1.x roadmap item). Writes fan in to
  the writer via a command channel — IPC, the documented DBOS/Celery `concurrency=1` recipe, or
  HTTP to a small writer service. The channel is `store.submit()` with a longer wire: D's
  command-shaped contract extends across processes naturally.

## Tier 2 — multiple nodes

Ship data out, funnel writes back. Columnar mirrors export to Parquet/Arrow/Lance in one call
(hand-off to Ray/Dask et al.); full read replicas via log shipping (today: Litestream over the
SQLite file, docs recipe; native once the custom append-log lands). Writable multi-node stays
permanently out of core — Eclipse Data Grid (1 writer + N full-copy readers, event-stream
replication) took a funded company a decade.

## Tier 3 — external derived-data consumers (the indexer scenario)

The commit-delta/watermark pipeline is a **public, versioned contract** (roadmap v0.x item 3)
precisely so consumers can live out of process. An external indexer tails commit deltas, computes
its index anywhere, stamps the artifact with the applied watermark; the app attaches it
read-only. Indexes are rebuildable derived data with idempotent watermark application, so the
indexer needs **zero write access**, may lag or crash, and can always be rebuilt from the graph.
This is textbook CDC; the in-process FTS5/usearch sidecars are merely the first two consumers of
the same contract.

## Never (ratified)

Multi-master mutation of the same live graph across threads, processes, or nodes; clustering or
FaaS scale-out in core; CRDT as the core data model. See [ROADMAP.md](ROADMAP.md) "Never" list.
