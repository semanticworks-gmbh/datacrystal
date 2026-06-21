# Fractal followers — literature & evidence appendix (research memo, 2026-06-20)

**Status: the evidence appendix** for [2026-06-20-fractal-followers.md](2026-06-20-fractal-followers.md)
(the design) and ROADMAP item 21. This is the **literature section**: the systems studied, what
worked, what bit them, the load-bearing lessons, and the citations behind every claim in the design.
The design's *Decision provenance* table (§6) maps each choice back here (cited as **[PA]**). No
commitment implied. Every claim is sourced in §4; a few figures are marked *unverified* where a
primary source could not be confirmed. Companion to the transport memo
([2026-06-11-replication-transports.md](2026-06-11-replication-transports.md)).

## 1. Why this memo

The fractal design (single writer + N watermark-fed read followers + command fan-in, over plain
HTTP, reusing COMMIT-DELTA-v1) is **not novel** — it is the convergent answer a dozen mature
systems arrived at, several after expensive detours. This memo records what those systems did,
what *worked*, and what *bit them*, so the design inherits their scars instead of re-earning them.

**The headline finding:** the entire field has converged on **single-writer / server-authority**
and *away* from CRDT/multi-master cores — including the team that invented CRDTs. datacrystal's
"no CRDT core, no multi-writer" ROADMAP stance is the mainstream lesson, not a limitation.

## 2. The systems

### Closest analogs (single-writer + replica-reads)

| System | Mechanism | What worked | What bit them | Lesson for us |
|---|---|---|---|---|
| **Turso / libSQL embedded replicas** | Local SQLite file synced from a cloud primary over HTTP; v1 was **physical 4 kB-page** replication, rebuilt as **logical CDC** | µs-local reads; trivial client API; read-your-writes on the writing node | Page-level was wasteful (CDC rewrite **8.9×–312× faster, 16× less data**); checkpoint divergence forced re-bootstrap; **silent zero-filled corrupt replica on incomplete bootstrap (`turso#5971`)**; "don't open the DB mid-sync → corruption" | **Ship logical deltas (we already do).** Bootstrap is the #1 hazard: **validate completeness before serving.** |
| **LiteFS (Fly.io)** | FUSE intercepts txns → LTX files streamed over HTTP; Consul leases for failover; rolling CRC64 | Transparent to the app; full local copy = fast reads; **TXID-cookie read-your-writes** | **FUSE caps ~100 tx/s**; FUSE + Consul = high ops; up to 10 s write-unavailability on failover; async loss window; now "limited updates" at Fly | **The TXID/watermark cookie is the RYW primitive.** **Avoid FUSE & consensus** — the moment you add failover you inherit them. |
| **rqlite** | SQLite behind **Raft** (multi-writer-via-leader) | Single binary; cluster in seconds; **tunable read consistency** (none→linearizable) | Every durable write pays a quorum round-trip; "strong" reads are a costly trap; "none" silently serves stale | **The tunable read-consistency dial** is the model for our freshness levels. Raft is the cost we avoid. |
| **dqlite (Canonical, LXD/MicroK8s)** | SQLite over **custom C-Raft** | In-process replicated SQLite | **The cautionary tale:** write amplification (**~33 TB written for 3.5 GB**), CPU spikes, memory leaks, fragile "no known leader" failover, opaque recovery | **Custom consensus is a maintenance sink.** Strongest validation of single-writer + HTTP. |

### Sync engines / local-first

| System | Mechanism | What worked | What bit them | Lesson |
|---|---|---|---|---|
| **ElectricSQL (legacy)** | Active-active Postgres↔SQLite via **Rich-CRDTs** | Demoed well | **Abandoned (Jul 2024):** CRDTs don't preserve relational invariants (FK/unique/sequences) for free — needed compensations/reservations/escrow per invariant; "wide surface for bugs" | **The CRDT inventors walked away from CRDTs.** Validates single-writer a priori. |
| **ElectricSQL (Electric Next, 1.x)** | **Read-path only**, Postgres as authority, partial replication via single-table **Shapes** over plain HTTP (offset+handle) | Much lower scope; CDN-friendly | Partial replication was the recurring tax — **rewrote the storage engine** (Aug 2025) under many-shape load; **~140 vs ~5,000 changes/s** for un-optimized vs index-friendly WHERE | **The pivot *is* our thesis.** Read-path-first. Subset filters must be index-friendly or throughput collapses. |
| **PowerSync** | Server-authoritative read sync into SQLite via **buckets** (YAML param+data queries); strict checkpoints | Designed for partial-replication at scale; client never resolves conflicts | Changing sync rules **rebuilds all buckets → mass re-sync**; row-level granularity = write amplification; blocking upload queue can stall | **Apply atomically at a checkpoint/watermark, never row-by-row.** Treat subset-definition change as a re-bootstrap. |
| **CouchDB / PouchDB** | Multi-master, `_changes` feed, MVCC revision trees, conflicts kept not merged | Decades-proven **HTTP-native change feed** (`since=<seq>`) | **Hidden-conflict data loss** ("not lost, just hidden away"); unbounded revision trees needing manual purge; slow filtered replication → "db-per-user" workaround | **The `since=<watermark>` resumable HTTP feed is the transport to copy.** Multi-master is the model to avoid. |
| **Replicache / Zero (Rocicorp)** | Optimistic client mutators **re-run authoritatively** on the server, client rebases | Optimistic UX "felt seamless"; server-authoritative "a fantastic move" | Schema/type duplication across client/server/cache; non-trivial `/push` scaffolding; zero-cache IVM is heavy machinery | **The mutator-rebase pattern** is the gold standard *if* we ever add optimistic writes — and only on a single ordered log. |

### Log-shipping / CDC / event-sourcing

| System | Mechanism | What worked | What bit them | Lesson |
|---|---|---|---|---|
| **Litestream** | Ships SQLite WAL to S3; **generation = snapshot + contiguous WAL run** | **Low maintenance** (sidecar, no consensus/FUSE); ~$1/mo DR | ~1 s async loss window; retention vs restore-replay tradeoff; **concurrent writers to one target corrupts** ("YOUR responsibility") | **Snapshot + tail-the-log-after-it** is canonical. Tune snapshot cadence *with* retention. Enforce single-writer-to-target. |
| **Debezium / Postgres slots** | Logical CDC; initial/incremental snapshot then stream | Logical = **natural table/column/predicate filtering** | **The retention-horizon failure made concrete:** a slow consumer pins WAL and **fills the disk**; `max_slot_wal_keep_size` then **invalidates the slot → forced fresh snapshot** | **Our exact control loop.** Bound retention deliberately; "aged-out → re-bootstrap" is the textbook resolution. |
| **Kafka log compaction** | Keep latest-per-key; null **tombstones** delete | Compacted topic = a latest-state snapshot | **Tombstone resurrection trap:** tombstones GC'd after `delete.retention.ms` (24 h default); a bootstrap slower than that **resurrects deleted keys** | **Deletes are the sharpest edge:** keep tombstones ≥ the snapshot horizon; encode deletes in the snapshot. |
| **Kleppmann, "Turning the DB inside out"** | Immutable event log → deterministic derived followers | The theoretical spine; new follower replays to converge | Replay-from-zero needs **snapshots** to bound cost; **determinism is a hidden hard requirement** | Our TID-ordered (never wall-clock) deltalog *is* this. **Guard apply-path determinism as hard as the commit path.** |

### Distributed-computing fundamentals

- **Waldo et al., "A Note on Distributed Computing" (1994):** you cannot make remote calls
  transparently local — latency, partial failure, concurrency, and memory/reference access differ
  *qualitatively*. → Reusing the local `commit()/apply()` **code path** across the wire is fine; the
  **semantic contract** at the call site is not local and must be surfaced (the basis for "same
  shape, different semantics," and for refusing follower writes in v0).
- **Fallacies of Distributed Computing:** network unreliable (→ retry + dedup), latency≠0 (→ no
  chatty round-trips; mirror, don't proxy), bandwidth finite (→ page large catch-up), not secure
  (→ auth `/deltas` and `/submit` off localhost), topology/admin change (→ reconnect + version skew).
- **Idempotency keys (Stripe / Brandur / DBOS):** at-least-once is the only honest network
  guarantee; **exactly-once *effect*** is reconstructed at the app via a caller key deduped
  **inside the writer's commit txn**. Mandatory for the (deferred) fan-in path.
- **Read-your-writes / monotonic reads (Terry/Bayou; CQRS):** the "created-then-404" UX bug.
  Fix: `submit()` returns its TID; offer `wait_for(tid)`; pin a client to one follower.
- **Single Writer Principle (Thompson/LMAX) + Actor model + DBOS + Celery `concurrency=1`:** the
  same idea at four granularities — one mutator + command fan-in + N readers. ADR-001's
  owner-confinement is this in-process; the replica item is "purely a transport swap."

## 3. Load-bearing lessons (curated)

1. **Ship logical deltas, not physical pages** — Turso's whole rewrite. We already do (COMMIT-DELTA-v1).
2. **The watermark/TID is the read-your-writes primitive** — LiteFS cookie, Turso, rqlite all converge.
3. **Make freshness a per-read dial, not one global guarantee** — rqlite's none/RYW/strong.
4. **Bootstrap/mid-life attach is the #1 correctness hazard** — `turso#5971`. Validate
   watermark + checksum before serving; carry a per-state checksum (LiteFS CRC64) to detect divergence.
5. **Snapshot + tail-after-it bounds catch-up cost** — Litestream/Debezium/Kafka/event-sourcing all do it.
6. **The retention-horizon → re-bootstrap loop is universal and has no third option** — unbounded
   log = disk exhaustion (Debezium); bounded log = re-bootstrap cost. Pick the horizon deliberately.
7. **Deletes need explicit tombstones retained ≥ the snapshot horizon** — Kafka resurrection trap;
   we have tombstone deltas (ADR-003), keep the discipline.
8. **At-least-once + idempotent apply; never promise exactly-once over the wire** — everyone says this.
9. **Apply atomically at a consistent watermark, never row-by-row** — PowerSync/Linear checkpoints; our P3 flip.
10. **Avoid FUSE and custom consensus** — the two maintenance sinks (LiteFS ~100 tx/s; dqlite 33 TB/3.5 GB).
11. **Partial replication is the universal hard problem** — everyone punts to **database-per-tenant**;
    filters must be index-friendly; "move-out" needs synthetic tombstones; a definition change is a re-bootstrap.
12. **Single-writer makes client-side conflict resolution *disappear*** — PowerSync/Zero/Linear get it free.
13. **Command fan-in (write-forwarding) is the standard shape** — LiteFS/Turso/rqlite all forward to the writer.
14. **Async replication has an inherent sub-second loss window — document it, don't pretend otherwise** (Litestream/LiteFS).
15. **Make any lossy decision loud** (CouchDB's silent conflict-burial is the anti-pattern; aligns with our loud-error invariants).

## 4. Literature & citations

Grouped; URL + the specific claim it backs. A few figures are flagged *unverified*.

**SQLite-distributed**
- Turso embedded replicas — whole-DB local replica, writes forwarded, RYW only on writer, no
  partial replication, page-level amplification: https://docs.turso.tech/features/embedded-replicas/introduction
- Turso page→CDC rewrite + benchmarks (8.9×–312×, 16.3× less data): https://turso.tech/blog/sync-benchmark
- Turso silent zero-filled corrupt replica on incomplete bootstrap: https://github.com/tursodatabase/turso/issues/5971
- "Don't open DB mid-sync → corruption": https://github.com/tursodatabase/libsql/discussions/1910 ; sync blocks reads: https://github.com/tursodatabase/libsql/issues/979
- Turso DB-per-tenant framing: https://turso.tech/blog/replicate-my-entire-production-database-you-must-be-mad-641d3f1ed9d8
- LiteFS architecture (LTX, TXID, rolling CRC64, HTTP streaming, Consul leases, split-brain re-snapshot): https://github.com/superfly/litefs/blob/main/docs/ARCHITECTURE.md
- LiteFS consistency / TXID-cookie RYW + `/litefs/<db>-pos`: https://fly.io/blog/tracking-consistency-with-litefs/
- LiteFS FUSE ~100 tx/s ceiling, async loss window: https://fly.io/docs/litefs/faq/
- LiteFS targets/DB-per-customer, ATTACH limits, FUSE rationale (benbjohnson): https://news.ycombinator.com/item?id=34250411
- LiteFS "limited updates" status: https://community.fly.io/t/what-is-the-status-of-litefs/23883 ; LiteFS Cloud sunset: https://community.fly.io/t/sunsetting-litefs-cloud/20829
- rqlite read-consistency levels: https://rqlite.io/docs/api/read-consistency/ ; linearizable vs strong: https://philipotoole.com/faster-reads-same-guarantees-linearizable-consistency-in-rqlite-8-32/ ; queued writes: https://rqlite.io/docs/api/queued-writes/
- dqlite internals: https://canonical.com/lxd/docs/latest/reference/dqlite-internals/ ; "no known leader": https://github.com/canonical/dqlite/issues/335 ; 33 TB/3.5 GB write amplification: https://github.com/canonical/microk8s/issues/3064 ; CPU spike: https://github.com/canonical/microk8s/issues/3227 ; memory growth: https://github.com/canonical/microk8s/issues/5016 ; recovery docs gap: https://github.com/canonical/microk8s/issues/2898 ; MTU/VPN sensitivity: https://github.com/canonical/microk8s/issues/1657

**Sync engines / local-first**
- ElectricSQL "Electric Next" retrospective (why they rebuilt, read-path-first): https://electric.ax/blog/2024/07/17/electric-next
- Rich-CRDTs (why plain CRDTs were insufficient): https://electric.ax/blog/2022/05/03/introducing-rich-crdts
- Shapes constraints + WHERE-clause throughput cliff (~140 vs ~5,000/s): https://electric.ax/docs/guides/shapes
- Electric v1.1 storage-engine rewrite under shape load: https://electric.ax/blog/2025/08/13/electricsql-v1.1-released
- PowerSync buckets / partial replication: https://www.powersync.com/blog/sync-rules-from-first-principles-partial-replication-to-sqlite ; checkpoint consistency: https://docs.powersync.com/architecture/consistency
- PouchDB conflicts (hidden-winner data loss): https://pouchdb.com/guides/conflicts.html ; CouchDB conflict model: https://docs.couchdb.org/en/stable/replication/conflicts.html ; unbounded revision trees: https://github.com/apache/couchdb/issues/1507
- Replicache consistency/how-it-works: https://doc.replicache.dev/concepts/consistency , https://doc.replicache.dev/concepts/how-it-works
- Zero custom mutators (speculative-discard) + synced queries: https://zero.rocicorp.dev/docs/custom-mutators , https://zero.rocicorp.dev/docs/synced-queries ; practitioner notes: https://www.solberg.is/zero ; Zero 1.0: https://www.infoq.com/news/2026/06/zero-version-1/
- Linear sync engine (lastSyncId total order, partial/lazy bootstrap, gap→full re-bootstrap): https://github.com/wzhudev/reverse-linear-sync-engine

**Log-shipping / CDC / event sourcing**
- Litestream how-it-works (generations = snapshot + WAL run): https://litestream.io/how-it-works/ ; motivation: https://litestream.io/blog/why-i-built-litestream/ ; tips (loss window, retention, concurrent-writer corruption): https://litestream.io/tips/
- Litestream ~1 MB/DB RAM + lz4 balloon — *unverified figures*: https://changelog.com/shipit/59 , https://fly.io/blog/all-in-on-sqlite-litestream/
- Debezium incremental snapshots: https://debezium.io/blog/2021/10/07/incremental-snapshots/ ; replication-slot WAL bloat + `max_slot_wal_keep_size` invalidation: https://www.morling.dev/blog/mastering-postgres-replication-slots/ ; connector-failure WAL accumulation: https://streamkap.com/resources-and-guides/debezium-replication-slot-issues
- Kafka log compaction + tombstone retention (`delete.retention.ms`, resurrection hazard — *partially verified*): https://docs.confluent.io/kafka/design/log_compaction.html
- Kleppmann "database inside out": https://martin.kleppmann.com/2015/11/05/database-inside-out-at-oredev.html
- CouchDB `_changes` feed (`since`, longpoll/continuous/eventsource, heartbeat, ordering caveat): https://docs.couchdb.org/en/stable/api/database/changes.html

**Fundamentals**
- Waldo et al., "A Note on Distributed Computing": https://scholar.harvard.edu/waldo/publications/note-distributed-computing ; summary: https://medium.com/paper-readings/a-note-on-distributed-computing-e27525f1123
- Fallacies of Distributed Computing: https://ably.com/blog/8-fallacies-of-distributed-computing ; history: https://blog.apnic.net/2025/12/08/21-years-and-counting-of-eight-fallacies-of-distributed-computing/ ; per-fallacy failures: https://arnon.me/wp-content/uploads/Files/fallacies.pdf
- Idempotency keys — Brandur: https://brandur.org/idempotency-keys ; Stripe: https://stripe.com/blog/idempotency , https://docs.stripe.com/api/idempotent_requests ; DBOS: https://docs.dbos.dev/typescript/tutorials/workflow-tutorial
- Single Writer Principle: https://mechanical-sympathy.blogspot.com/2011/09/single-writer-principle.html ; LMAX: https://martinfowler.com/articles/lmax.html
- Read-your-writes / CQRS staleness: https://www.mongodb.com/docs/manual/core/read-isolation-consistency-recency/ , https://codeopinion.com/eventual-consistency-is-a-ux-nightmare/ , https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs
- Actor model: https://dist-prog-book.com/chapter/3/message-passing.html ; Celery concurrency (*community phrasing — verify*): https://docs.celeryq.dev/en/stable/userguide/workers.html
