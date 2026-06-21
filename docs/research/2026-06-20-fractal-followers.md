# Fractal followers — single-writer replication for edge contributors (design exploration, 2026-06-20)

**Status: design exploration. Nothing shipped; needs a `needs-owner-decision` + a VISION line before
any story.** Rides ROADMAP **item 21** (networked replication), served from `datacrystal[web]`;
failover would touch **item 16** (lease fencing). Multi-writer/CRDT stay **Never**
([ROADMAP](../design/ROADMAP.md)). The single-writer shape is already ratified
([ADR-001](../design/ADR-001-concurrency-contract.md); [SCALING.md](../design/SCALING.md);
[transport memo](2026-06-11-replication-transports.md)). Prior-art evidence + citations live in the
companion [fractal-followers-prior-art.md](2026-06-20-fractal-followers-prior-art.md).

## 1. Why

- **The problem.** A system-of-record graph is cloud-hosted and shared by a team. Specialized worker
  nodes ("indexers"/"annotators") run on the **edge** — often behind NAT — and must (a) read the
  shared graph locally and fast (incl. multi-hop traversal) and (b) **contribute back** what they
  discover or enrich.
- **The persona — validated (dogfood).** This is the maintainer's own deployment: a cloud instance
  plus edge nodes that run **local LLMs (Ollama)** for embeddings/extraction and push results back.
  The shape lets the **cloud machine stay small** — reads *and* heavy compute offload to the edge;
  only a modest stream of result-commits funnels to the single writer.
- **Fits the charter.** Single-writer is preserved throughout; "the only infra is a blob store" holds
  (the coordinator is just one of your nodes, promoted). A follower that could only *read* would be a
  cache, not a contributor — so contribution is in scope from v0.

## 2. The idea — fractal

Every node runs the **same codebase**; **role is config**.

| Role | Lease | Writes | Reads | open |
|---|---|---|---|---|
| **Coordinator** | holds it | sole writer; owns OID/TID; resolves keys→OIDs | local | `Store.open("cabinet.store")` + mounts the federation router |
| **Follower** | none | **contribute** (`upsert`/insert) fanned in on `commit()` | local replica, full-speed | `open_follower("https://coord", api_key=…)` |

- Reads on a follower hit a **real local store** → identical API, sub-ms, no per-call round-trips
  (the reason to mirror, not RPC-proxy: a 10-edge traversal proxied = 10 round-trips).
- Writes never touch the shared graph directly; they **fan in** to the coordinator as commands
  (ADR-001's sanctioned `store.submit` path). Single-writer invariant intact.
- Symmetric code ⇒ a follower *could* later be promoted to coordinator (failover) — enabled, not v0.

## 3. The tracer bullet — concrete v0

**Scope:** 1 coordinator + 1–~10 edge followers, each an indexer/annotator **owning a data segment**,
**append-mostly** (new entities, new owned edges). Bulk migration/rewrite is *coordinator* work, so
the edge never needs an exclusive multi-commit window. Everything here is complete and correct for
that; **every cut fails closed** (raises loudly, never silently corrupts).

```
COORDINATOR (cloud)                       FOLLOWER (edge indexer/annotator)
 Store.open + DeltaLog from TID 0          open_follower(url, api_key=, segment="folderX")
 + federation router:                       └─ bootstrap: GET /v1/deltas?after=0  → apply (replay-from-0)
    GET  /v1/head   → {last_tid}            reads: LOCAL, full speed
    GET  /v1/deltas?after=<tid> → frames    contribute:
    POST /v1/submit → fan-in                  buffer upsert(Document/Annotation …)
                                              commit() ─POST /v1/submit─▶ store.submit(fn):
                                                                          cid guard → OCC check
                                              ◀─ {applied_tid, key→oid} ─ → upsert → commit
                                              block until own delta loops back (sync read-your-writes)
```

```python
# COORDINATOR — your normal app; one router mounts the federation surface
store = dc.Store.open("cabinet.store")                 # + DeltaLog attached from TID 0
app = FastAPI()
app.include_router(dc.web.federation_router(store), dependencies=[Depends(my_auth)])  # you bring authn/z

# FOLLOWER — one call gets a synced local replica + contribute-on-commit
store = dc.open_follower("https://coordinator", api_key=..., segment="folderX")
known = {d.path for d in store.query(Document)}        # fast LOCAL read of the shared graph
for path in scan_local_files():                        # data only this node sees
    if path not in known:
        store.upsert(Document(path=path, text=extract(path), segment="folderX"))
m = store.get(Mineral, qid="Q42")                      # existing cloud entity (real OID, from replica)
store.upsert(Annotation(of=m, kind="embedding", vector=embed(m)))   # enrich via a NEW owned edge
store.commit()                                         # fan-in to the coordinator (loud on conflict/skew)
```

- **Three endpoints** (in `datacrystal[web]`): `/v1/head` (liveness + lag), `/v1/deltas?after=<tid>`
  (COMMIT-DELTA-v1 frames from the `DeltaLog`; catch-up **and** held-open tail), `/v1/submit`
  (`store.submit`: cid guard → OCC → `upsert` → `commit`, idempotency-keyed → `{applied_tid, key→oid}`).
- **Follower**: replay-from-0 bootstrap (**no snapshot encoder**); local reads; `commit()` = `/submit`,
  blocking until its own delta returns; **single-threaded** (apply on the owner thread via `sync()` →
  no concurrent-sync hazard).
- **Delivery is one endpoint, three modes** — start simplest, upgrade transparently (identical
  follower logic): **plain poll** (v0) → **long-poll** (cheap, near-real-time) → **SSE** (deferred).
- **Data-model rules (load-bearing):**
  - contributed types **must** carry a `Unique` natural key (`path` / `url` / `(of, kind)`) — the basis
    of idempotency + conflict detection (DX-warn if missing);
  - a `segment`/`source` field marks ownership (optionally authz-enforced on `/submit`);
  - a new entity may reference only **existing** entities (real OID), never another new entity in the
    same batch (loud error) — annotator/indexer contributions fit this naturally.
- **Reuse vs net-new — why it's weeks, not quarters:**
  - *Reuse:* `Store`, `DeltaLog` (replay + tail), `store.submit` (fan-in), `upsert`/`commit` (apply +
    crash-safety), `Unique` index, msgpack codec, web lifespan/`Depends`.
  - *Net-new (the only real work):* the 3-route router; the `/submit` handler (cid guard + OCC +
    idempotency); `open_follower` (replay bootstrap + pull loop + commit-as-submit + sync RYW); error
    translation; the base-version carry. **A facade over the engine — not a new storage backend** (by
    the time `apply()` runs, OIDs/TID are already minted, so fan-in must sit at `commit()`, above the
    backend seam).

## 4. How it stays correct (cheap, fail-closed)

- **Schema skew → loud, never silent.** Roll out **coordinator-first**; a cid-lineage guard on
  `/submit` rejects an unknown shape with `SchemaSkewError` before `upsert`. (Without it, a
  newer-follower field the coordinator lacks is silently dropped — `_store.py:690` merges only known
  fields. That's the CouchDB silent-loss trap.)
- **Conflicts → `ConflictError`, never silent LWW.** OCC: each contribution carries the base version
  it read; the coordinator rejects if the entity moved. Segment-partitioned followers never trigger
  it — it's the fail-closed guard for when that assumption breaks. *Contract:* re-read before retry.
- **Idempotent contribution.** Natural-key `upsert` + an idempotency key (deduped in the commit txn) →
  a lost-ack retry re-merges, never double-applies. At-least-once delivery, exactly-once effect.
- **Atomic, ordered, gapless.** Deltas apply whole, per-watermark (no torn reads); `tid > watermark+1`
  is refused; the watermark is the source of truth (reconnect = reconcile, apply-twice ≡ apply-once).
- **Inherited free:** single-writer mutual exclusion + serialized application + crash-safety come from
  the coordinator's existing P1/P2/P3 `commit`. Read-your-writes is synchronous in v0.

## 5. What v0 cuts — and the path each cut opens (the outlook)

Every cut is a deliberate, fail-closed assumption; lifting it is the post-v0 roadmap:

| v0 assumption | Fails closed because… | Lifting it unlocks → |
|---|---|---|
| Small store (bootstrap by replay-from-0) | slow if huge, never wrong | **snapshot encoder + checksum** |
| Full-log retention (never prune) | log grows visibly, not silent | **retention horizon + re-bootstrap** |
| No same-batch new→new refs | violation → loud error | **intra-batch OID remap** (needs an ADR — engine touch) |
| ≤~10 followers, no rate-limit | fine at scale; doc'd ceiling | **`/snapshot` 429 / anti-thundering-herd** |
| Single-threaded follower (`sync()`) | no concurrent-sync hazard | **background auto-poll + fencing** |
| Synchronous contribute | simple + correct, not high-throughput | **async contribute + `wait_for(tid)` dial** |
| Contribute-only (OCC detects) | shared-field edits → loud reject | **conflict resolution policy** (LWW-with-authority) |

Permanently out (charter / scope): **multi-writer + CRDT** (Never); **automatic failover/promotion**
(needs item-16 fencing — a half-measure risks split-brain); **partial/filtered replication** (the
universal hard problem — escape hatch is "many small stores"). And two clarifications:

- **A write lease ("talking stick") is *not* the answer** — fan-in already gives single-mutator +
  serialized consistency; a correct lease (TTL, renewal, crash-reclaim, zombie fencing) is more moving
  parts than the stateless OCC it would replace. **OCC is the lightweight, fail-closed talking stick**;
  segment ownership is a stateless authz rule, not a lease. (A fenced *exclusive session* is the right
  tool only for coordinator-side bulk rewrites — never the edge path.)
- **An object-store backend (item 16) is orthogonal, not an alternative** — it changes *where bytes
  live*, not *who writes*; a shared bucket hits the **same** coordination wall (still one writer +
  followers + fan-in; S3-readers still materialize locally and catch up). It composes *under*
  fractal-followers (durability + writer fencing + an optional delta transport), it doesn't replace it.

## 6. Why this shape holds up (the critical check)

A fresh, unanchored red-team (independent re-derivation + 4 adversarial lenses + feasibility research,
none seeing the design) returned **proceed-with-changes**:

- **Single-writer is the validated choice, by others' scars:** ElectricSQL (CRDT inventors) abandoned
  its CRDT core because invariant-preservation under merge needs unbounded machinery; CouchDB's
  multi-master silently buries conflicts; dqlite's custom Raft is a maintenance sink. Our Never-list is
  these lessons a priori.
- **Reuse starts where Turso *ended up*:** Turso shipped physical-page replication, then rebuilt as
  logical CDC (8.9×–312× better). datacrystal ships logical deltas already.
- **Independent re-derivation converged** on the same hub-spoke / OCC / natural-key shape — so it isn't
  an artifact of how we reasoned. No materially simpler alternative keeps the contribution capability.
- **Transport:** writer-served HTTP, msgpack frames; **not** GraphQL (that's the external web surface),
  WebSockets (no bidirectional need), or a broker (the writer is already the distinguished node).
- **Honest cons:** eventual consistency (followers stale by delta-lag); single-writer write-ceiling
  (fine for periodic indexer contributions, *not* high-concurrency write storms); re-bootstrap of a
  large store stalls (mitigated only once the snapshot encoder lands — §5). The standing warning:
  replication is a **permanent correctness surface**, so v0 must be honestly small.

### Decision provenance

Each load-bearing choice was coined from a prior system's scar or principle; the full evidence +
citations are the **literature appendix**
([fractal-followers-prior-art.md](2026-06-20-fractal-followers-prior-art.md), referenced as **[PA]**).

| Decision | Coined from |
|---|---|
| Single-writer; no multi-writer/CRDT | CRDTs don't preserve invariants for free → **ElectricSQL** pivot; multi-master hides conflicts → **CouchDB**; custom consensus = maintenance sink → **dqlite** [PA §2] |
| Logical deltas (reuse COMMIT-DELTA-v1), not physical pages | page replication is wasteful → **Turso** CDC rewrite (8.9–312×) [PA §2] |
| Snapshot + resumable `since=<watermark>` log over HTTP | the proven change-feed → **CouchDB `_changes`**; snapshot-then-stream → **Debezium/Litestream** [PA §2] |
| Bootstrap completeness (checksum before serving) | silent corrupt replica → **Turso #5971**; rolling checksum → **LiteFS** [PA §3] |
| OCC detection, never silent LWW | hidden-conflict data loss → **CouchDB**; tunable read-consistency → **rqlite** [PA §3] |
| Idempotency key for fan-in writes | lost-ack ambiguity, at-least-once → **Stripe/Brandur/DBOS** [PA §2 fundamentals] |
| Read-your-writes via watermark, opt-in | TXID cookie → **LiteFS**; session guarantees → **Terry/Bayou** [PA §3] |
| Retention horizon → re-bootstrap | slow consumer fills disk → **Debezium slots**; generations → **Litestream**; tombstone resurrection → **Kafka** [PA §3] |
| Avoid FUSE / avoid broker | FUSE ~100 tx/s + ops → **LiteFS**; broker ops cost → **transport memo** [PA §2] |
| Partial replication deferred → "many small stores" | the universal tax → **ElectricSQL shapes / PowerSync buckets / CouchDB filters** [PA §2] |
| "Remote ≠ local" honesty in the write path | **Waldo** (*A Note on Distributed Computing*); the **Fallacies** [PA §2 fundamentals] |
| Single-writer + command fan-in spine | **Single Writer Principle** (Thompson/LMAX); Actor model; DBOS/Celery `concurrency=1` [PA §3] |

## 7. Next steps

1. **Decision (yours):** sequence the tracer bullet vs the search/index roadmap. Persona is settled;
   this is a *when*, not a *whether*. Greenlight ⇒ promote from exploration to a staged item-21 + a
   one-line VISION entry.
2. **Build the tracer bullet (§3)** — weeks, in `datacrystal[web]`. The only real new code is the
   `/submit` handler + `open_follower`. Ship the **cheap fail-closed guards** that gate correctness:
   the cid-lineage guard, the `ConflictError` re-read contract, and idempotency.
3. **Prove it** against §3's acceptance criteria on the mineral domain: replay-from-0 reproduces state;
   contribute round-trips to a 2nd follower; lost-ack retry is idempotent; a forced conflict raises
   `ConflictError`; a schema-skew submit raises `SchemaSkewError`. Ride the existing
   `check_delta_consumer` conformance kit for the consumer side.
4. **Then, demand-driven (§5 table):** snapshot encoder → retention horizon → async/`wait_for` →
   intra-batch refs (ADR) → fencing/failover. Each is independent and shippable alone.

## 8. References

- Companion evidence + citations: [fractal-followers-prior-art.md](2026-06-20-fractal-followers-prior-art.md).
- [transport memo](2026-06-11-replication-transports.md) · [ROADMAP](../design/ROADMAP.md) (items 21, 16; Never) ·
  [ADR-001](../design/ADR-001-concurrency-contract.md) · [SCALING.md](../design/SCALING.md) ·
  [COMMIT-DELTA-v1](../design/COMMIT-DELTA-v1.md) · [deltalog](../../src/datacrystal/deltalog.py) ·
  `datacrystal[web]` (`src/datacrystal/web/`).
