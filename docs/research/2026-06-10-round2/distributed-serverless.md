# Round-2 finding: distributed-serverless

_Round-2 research, 2026-06-10. Confidence: high. Cross-examined verdicts in [cross-examination.md](cross-examination.md)._

## Summary

The single-writer constraint is not a defect to engineer away in v1 — it is the same constraint SQLite, EclipseStore, and SlateDB all have, and all three ecosystems converged on the identical answer: keep exactly one writer, scale reads via replicated full copies fed by log shipping, and route writes to the writer through a queue/RPC. DBOS (dbos-transact-py, Postgres-backed durable-execution library, very active in 2026) "in front of" pyrsistance is a sensible actor/command-queue pattern (enqueue write-commands, one owner process consumes with concurrency=1), but using pyrsistance's own commit log AS the durable-execution substrate for multi-process coordination is a category error — the substrate must be shared across and outlive processes, which an embedded in-process store by definition is not. EclipseStore itself validates this layering: core is hard single-process (lease-refreshed used.lock file), read access for other processes is a read-only REST sidecar, and scale-out is a separate product (MicroStream Cluster, open-sourced July 2025 as Eclipse Data Grid: 1 Writer + N Reader nodes, full graph copy per node, event-stream replication, eventual consistency, Kubernetes) that took a funded company years. Serverless FaaS with scale-to-zero and concurrent instances is fundamentally mismatched with a mutable in-memory graph; the workable variants are a pinned single instance (Cloud Run max-instances=1) or, long-term, S3-conditional-write lease/fencing à la SlateDB. Recommended layering: v1 documents workers=1+asyncio and the command-queue recipe; v1.x adds read-only snapshot readers and S3 log shipping (nearly free given the append-only design); clustering, CRDT merge, and FaaS scale-out stay out permanently.

## Key findings

### DBOS in 2026: Postgres-backed durable-execution library, not a server

dbos-transact-py (MIT, github.com/dbos-inc/dbos-transact-py) checkpoints workflow inputs and step outputs to a Postgres 'system database'; recovery replays from the last checkpointed step. It is a library: 'no separate orchestration server and no infrastructure required besides Postgres' (docs.dbos.dev/architecture). Multiple app processes coordinate solely through shared Postgres; in distributed deployments interrupted-workflow detection goes through DBOS Conductor. Actively developed: May 2026 release added workflow timeline visualizations, DB-backed queue config, Java/Go parity. Queues guarantee tasks are 'processed exactly once' with global_concurrency and worker_concurrency limits, including concurrency=1 (docs.dbos.dev/python/tutorials/queue-tutorial).

### 'DBOS in front of the store' = valid actor pattern; 'our commit log as DE substrate' = category error

Pattern that works: uvicorn workers (or Lambda fns) enqueue write-command workflows to a DBOS queue; one designated process — the only one registered as consumer of that queue, with concurrency=1 — owns the pyrsistance store and applies commands serially. This is exactly the serializing-writer/actor model and DBOS's queue semantics support it. Caveats: (a) it reintroduces Postgres, undercutting the embedded zero-infra value prop, so it must be a documented user-land recipe, not a core dependency; (b) steps are at-least-once before checkpoint, so command application must be idempotent (pyrsistance's idempotent commit-delta/watermark design already fits). Conversely, pyrsistance's commit log cannot BE the durable-execution substrate for multi-process workers: DBOS-style recovery requires a checkpoint store reachable from every process and surviving any one process; an in-process single-writer log is reachable from exactly one. Within a single process, persisting workflow/step state as ordinary objects in the graph is legitimate (nice for the agent-memory persona) but that is just persistence, not distributed durable execution.

### Temporal/Hatchet/Celery all express the same serializing-writer pattern, at different infra cost

Temporal's entity/actor workflow pattern is the canonical form: signals append to an internal command queue, the main workflow loop drains it, serializing all side effects ('processing them through a queue guarantees...' — temporal.io/blog/actor-workflow-player-sessions); but a single global writer-workflow hits event-history limits (continue_as_new churn) and requires a Temporal server cluster. Hatchet (Postgres-backed, github.com/hatchet-dev/hatchet, >1B tasks/month in 2026) gives global serialization via a concurrency key with limit 1. Celery: one worker, concurrency=1, single queue — classic, but only at-least-once, no durable execution. All three prove the pattern (all writes through one process) is sound; none should be a pyrsistance dependency. The cheapest in-envelope version is an asyncio in-process queue (v1) or a unix-socket command RPC to the writer process (v1.x recipe).

### Litestream v0.5 (Oct 2025): physical log shipping transfers directly to an append-only object log

Litestream v0.5 (fly.io/blog/litestream-v050-is-here, Simon Willison Oct 3 2025) ships ordered page sets in the LTX format to S3 with point-in-time recovery, and its new VFS reads a replica directly from object storage — pages fetched on demand, cached locally, kept fresh by polling for new LTX files (litestream.io/how-it-works/vfs). Ben Johnson explicitly abandoned the FUSE approach (LiteFS) and folded its lessons back into single-binary Litestream. For pyrsistance this transfers almost verbatim and is EASIER: the store is already an append-only log with sealed data files + per-record checksums, so 'replication format = the storage format' — upload sealed segments + tx-log to S3, get backup, PITR, and lazy-hydrating read replicas for free. No page-diffing layer needed, unlike SQLite.

### libSQL/Turso embedded replicas are the correct mental model for `uvicorn --workers 4`

Turso embedded replicas: each process holds a full local SQLite copy for microsecond reads; writes are transparently forwarded to the single remote primary; after commit the local replica syncs, giving read-your-writes (docs.turso.tech/features/embedded-replicas, turso.tech blog). Mapped to pyrsistance: each worker process = read-only snapshot of the object graph at a watermark; writes = command RPC to the one writer process; the already-designed commit-delta/watermark pipeline (built for FTS5/usearch sidecars) doubles as the replica invalidation feed (apply OID-level invalidations, re-load changed records). LiteFS's LTX-position trick (client passes last-seen txn id, replica blocks until caught up) is the read-your-writes recipe. Negative results worth noting: LiteFS is pre-1.0, LiteFS Cloud sunset Oct 2024, development deprioritized; cr-sqlite (vlcn-io, CRDT multi-writer) is effectively dormant with README still saying 'work to be done to make this production ready' — CRDT merge of an arbitrary object graph with invariants is a research project and does not transfer.

### EclipseStore itself: hard single-process core + read-only sidecar + clustering as a separate product

Core EclipseStore enforces exclusive storage access via a lease-style lock file ('used.lock') refreshed by a dedicated thread — see StorageLockFileManager.java in the local source (/Users/sh/pyrsistance/resources/store/storage/storage/src/main/java/org/eclipse/store/storage/types/StorageLockFileManager.java). Cross-process read access is answered by storage-restservice (/Users/sh/pyrsistance/resources/store/storage/rest/: javalin/springboot service modules) — a read-only REST view of low-level storage records for the storage viewer, NOT a second writer. Scale-out was never put in core: it was the commercial MicroStream Cluster, open-sourced July 2025 as Eclipse Data Grid (EPL-2.0, github.com/eclipse-datagrid/datagrid): minimum '2 nodes, 1 Writer and 1 Reader', every node holds a complete copy of the object graph, replication via 'an event-based backup strategy... in an event stream', eventual consistency cluster-wide, Kubernetes-based, AWS SaaS on EFS (docs.microstream.one/enterprise/manual/1/cloud). Validation: the originators square single-writer with scale-out exactly via replicated full read copies + single writer — and it took a company a separate multi-year product. A solo maintainer should treat this as out of scope.

### Serverless reality: mutable in-memory graph is fundamentally wrong for scale-out FaaS; two narrow escapes exist

Lambda/Cloud Run sandboxes have ephemeral filesystems, scale to zero, and — fatally — scale out to N concurrent instances, each of which would be a writer; cold start would also require hydrating 0.6–3 GB (1–5M objects x ~600 B) from S3 per instance. Escape 1 (works today): pin to a single warm instance — Cloud Run min-instances=1/max-instances=1 with CPU always allocated is effectively a managed VM; EclipseStore's own serverless story is the same shape: IBM WebSphere Liberty InstantOn (CRIU checkpoint/restore) for near-instant restore of one warm JVM, not multi-instance (ibm.com case study). EclipseStore fully supports S3 as a storage target (docs.eclipsestore.io aws-s3 page) and the local source has AFS connectors for aws/azure/gcp/sql/redis/kafka (/Users/sh/pyrsistance/resources/store/afs/). Escape 2 (the future blueprint): S3 conditional writes (If-None-Match GA Aug 2024, If-Match/CAS Nov 2024) enable S3-native leases and writer fencing — SlateDB (slatedb.io) is an embedded engine on object storage with a formally verified single-writer manifest-fencing protocol plus multiple readers, and Gunnar Morling documented S3-CAS leader election. EclipseStore's used.lock lease translates directly to this. It is a v2+/extension-scale effort, not v1.x.

### Recommended layered story

v0.x/v1 (document, don't build): single process, workers=1 + asyncio; state plainly 'pyrsistance is an embedded primary — the libSQL model, where your process IS the database'; document the command-queue recipe (in-process asyncio queue; for multi-process fan-in, any of Celery/Hatchet/DBOS with a concurrency=1 queue consumed only by the store-owning process; DBOS specifically if users also want durable workflows — synergy, not dependency). v1.x (cheap, high leverage, reuses existing design): (a) read-only snapshot reader — second process opens the store read-only at the last committed watermark, refreshed via the commit-delta feed; this alone covers 'web worker reads + one writer' and debugging/inspection (EclipseStore's restservice analog); (b) log shipping — upload sealed segments + tx log to S3/directory for backup + PITR (Litestream analog, nearly free given append-only format + checksums); (c) unix-socket writer-RPC recipe for uvicorn --workers N. Never (in core): multi-writer, CRDT merge (cr-sqlite is dormant for a reason), Kubernetes clustering (Eclipse Data Grid took a company), running inside auto-scaling FaaS. Extension territory (only if the project succeeds): S3-lease single writer with SlateDB-style fencing for serverless.

## Recommendation

Do not engineer around single-writer in v1: document workers=1 + asyncio and the 'embedded primary' framing (libSQL model), with DBOS/Hatchet/Celery concurrency=1 command-queue as a user-land recipe for multi-process fan-in; in v1.x add read-only snapshot readers (reusing the commit-delta/watermark feed as invalidation) and S3 log shipping/PITR (nearly free on the append-only format); keep clustering, CRDT merge, and auto-scaling FaaS permanently out of core, reserving S3-conditional-write lease/fencing (SlateDB pattern) as a possible v2+ extension — justification: SQLite (Litestream/Turso) and EclipseStore (Data Grid: 1 writer + N full-copy readers) both prove the winning answer is replicated read-only copies fed by log shipping around exactly one writer, and everything beyond that took funded teams.

## Sources

- https://github.com/dbos-inc/dbos-transact-py
- https://docs.dbos.dev/architecture
- https://docs.dbos.dev/python/tutorials/queue-tutorial
- https://www.dbos.dev/blog/new-in-dbos-may-2026
- https://temporal.io/blog/actor-workflow-player-sessions
- https://temporal.io/blog/entity-workflow-loyalty-points
- https://github.com/hatchet-dev/hatchet
- https://fly.io/blog/litestream-v050-is-here/
- https://litestream.io/how-it-works/vfs/
- https://simonwillison.net/2025/Oct/3/litestream/
- https://docs.turso.tech/features/embedded-replicas/introduction
- https://turso.tech/blog/local-first-cloud-connected-sqlite-with-turso-embedded-replicas
- https://github.com/vlcn-io/cr-sqlite
- https://github.com/eclipse-datagrid/datagrid
- https://microstream.one/blog/2025/07/14/eclipse-data-grid-is-available-as-open-source/
- https://docs.microstream.one/enterprise/manual/1/cloud/index.html
- https://microstream.one/products/microstream-cluster/
- https://docs.eclipsestore.io/manual/storage/storage-targets/blob-stores/aws-s3.html
- https://www.ibm.com/case-studies/blog/eclipsestore-enables-high-performance-and-saves-96-data-storage-costs-with-websphere-liberty-instanton
- https://slatedb.io/
- https://slatedb.io/rfcs/0001-manifest/
- https://www.morling.dev/blog/leader-election-with-s3-conditional-writes/
- file:///Users/sh/pyrsistance/resources/store/storage/storage/src/main/java/org/eclipse/store/storage/types/StorageLockFileManager.java
- file:///Users/sh/pyrsistance/resources/store/storage/rest/
- file:///Users/sh/pyrsistance/resources/store/afs/
