# Fractal followers — a minimal single-writer replication design (design exploration, 2026-06-20)

**Status: design exploration / proposal. Nothing here is shipped. No commitment implied — this
needs a `needs-owner-decision` + a VISION entry before any story.** It rides ROADMAP **punted item
21** (networked replication, served from `datacrystal[web]`); failover touches **item 16** (lease
fencing). Multi-writer/clustering in core stays **Never** ([ROADMAP](../design/ROADMAP.md)). The
single-writer scaling shape is already ratified ([ADR-001](../design/ADR-001-concurrency-contract.md)
Consequences; [SCALING.md](../design/SCALING.md);
[transport memo](2026-06-11-replication-transports.md)). Prior-art evidence backing every choice
here lives in the companion [fractal-followers-prior-art.md](2026-06-20-fractal-followers-prior-art.md).

## 1. TL;DR

**Fractal:** every node runs the *same* datacrystal codebase; **role is config.** One node holds
the lease and is the **coordinator** (the only writer). Every other node is a **follower** — a
local datacrystal store kept current by replaying the writer's
[COMMIT-DELTA-v1](../design/COMMIT-DELTA-v1.md) stream from its TID watermark. Followers read at
full local speed against the *same* API, **and they contribute back**: a follower's `commit()`
fans the buffered writes in to the coordinator as a command batch.

The motivating shape: the **system-of-record graph is cloud-hosted** (the coordinator, accessed by
the team); **specialized indexers run on the edge** (e.g. a MacBook scanning local files, or a node
with access to data/edges the cloud cannot reach), reading the shared graph locally and
**publishing what they find or enrich back to the cloud.** A follower that could only read would be
a cache, not a contributor — so writes are in scope from v0.

**The decision that keeps this minimal is not "no writes" — it is *which* writes.** There are two
write patterns, and only one is hard:

- **Contribute / enrich (v0, easy):** insert new entities you discovered, or add derived
  edges/fields you own. Append-mostly; nobody else is touching this data. When the contributed
  type has a **natural/unique key** (a document's path, a URL, a content hash), this is
  **idempotent by construction** — `upsert`-by-key applied twice ≡ once — so the scary parts
  (retry-double-apply, conflict resolution) simply do not fire.
- **Concurrent edit of shared state (deferred):** two writers mutating the *same field of the same
  existing entity*. This is where conflict policy, optimistic-reject and read-your-writes machinery
  live. The indexer persona does **not** need it, so v0 does not ship it.

So v0 reuses already-locked, already-tested contracts (COMMIT-DELTA-v1 for the read tail; `Unique`
indexes + key-merge `upsert` for idempotent contribution) plus a thin HTTP transport. That is why
it is simultaneously **contribution-capable** and **minimal**.

## 2. The fractal property (roles)

| Role | Lease? | Writes | Reads | `dc.…open(...)` |
|---|---|---|---|---|
| **Coordinator** | holds it | local, authoritative; owns the OID/TID sequences; resolves natural keys → OIDs | local | `Store.open("cabinet.store")` — unchanged; also mounts the federation router |
| **Follower** | none | **contribute-writes** (`upsert`/insert of new or owned data) fanned in to the coordinator on `commit()` | local replica, full-speed | `open_follower("https://coord", api_key=…)` → bootstrap + tail + submit |

Same binary, role chosen by a flag + a coordinator URL. A follower may also keep a **private local
store** for data that should never leave the node (scratch, machine-local caches) — that is just an
ordinary local `Store` the app opens itself, no special routing in v0.

Because the codebase is symmetric, a follower *could* later be promoted to coordinator (failover,
§8) — the architecture enables it; v0 does not ship it.

## 3. v0 — the minimal core (read replica + contribute-writes)

### 3a. Read path (a remote-fed replica)

```
cold start :  GET /v1/snapshot?at=<tid>  -> materialize local replica, watermark = tid
catch up   :  GET /v1/deltas?after=<wm>  -> apply in TID order, advance watermark
steady     :  (same endpoint, held open) -> apply live frames as they arrive
reconnect  :  GET /v1/deltas?after=<wm>  -> reconcile from watermark (idempotent), resume
aged out   :  GET /v1/snapshot?at=<tid>  -> re-bootstrap (watermark < retention horizon, §5)
```

Reads (`query/get/get_many/count/pluck/Lazy.get/incoming`) run **locally** against real data —
identical API, sub-ms, no per-call round-trips. A follower exposes its `watermark` and `delta_lag`
for observability.

### 3b. Contribute-write path (`commit()` = fan-in)

A follower buffers `upsert`/insert exactly like a local store; `commit()` ships the batch to the
coordinator instead of writing locally:

```python
# MacBook indexer — a follower: read the shared graph, contribute specialized local data
store = dc.open_follower("https://cloud-coordinator", api_key=...)   # bootstrap + tail

known = {d.path for d in store.query(Document)}        # fast LOCAL read of the cloud graph
for path in scan_local_files():                        # data only this machine can see
    if path not in known:
        store.upsert(Document(path=path, text=extract(path), source="sven-macbook"))

m = store.get(Mineral, qid="Q42")                      # an existing cloud entity (real OID, from replica)
store.upsert(Embedding(of=m, vector=embed(m)))         # enrich it with a new owned edge

ack = store.commit()    # NOT a local write: fan the batch in to the coordinator, await ACK
# ack -> {applied_tid: 4711, keys: {("Document","…/a.md"): <oid>, …}, status: "applied"}
```

Under the hood `commit()` on a follower:
1. drains buffered upserts into one command batch (with a batch idempotency key for the keyless
   case, §5.7);
2. `POST /v1/submit` to the coordinator; awaits the ACK;
3. coordinator: **dedup → resolve natural keys → assign OIDs (incl. intra-batch references) →
   apply in its single-writer commit → return `{applied_tid, key→oid map}`**;
4. the same change also arrives on the delta tail (idempotent — already applied by OID), advancing
   the follower's watermark so the contribution becomes visible in the local replica.

**What makes this the *easy* write case** (see §1): contributed types carry a natural key, so step 2
is idempotent (re-`upsert` by path/URL/hash is a no-op, not a duplicate — a network blip after
apply-before-ACK is safe to retry); existing cloud entities are referenced by their real OID (read
from the replica); new own-entities are referenced by natural key and resolved by the coordinator.
None of the conflict/optimistic-reject machinery is needed because nothing edits a shared field.

### 3c. What is net-new vs reused — the minimality claim

| Need | Provided by (today) | Net-new in v0 |
|---|---|---|
| Ordered, idempotent, gapless change stream | COMMIT-DELTA-v1 (locked) | — |
| Apply a delta into a store, replay, mid-life join | `deltalog` applier + `bootstrap()` | wire it to a remote feed |
| Atomic per-watermark state flip (no torn reads) | commit P3 flip (ADR-001) | — |
| Gap refusal (`tid > watermark+1`) | `DeltaGapError` | surface as "re-bootstrap" |
| Idempotent contribution | `Unique` indexes + key-merge `upsert` | dedup at the coordinator |
| Command fan-in (foreign → owner) | `store.submit(fn)` (ADR-001) | the HTTP `/submit` transport |
| Frozen consistent state to serialize a snapshot | `Snapshot` + web snapshot pool | a `/snapshot` encoder |

So v0 ≈ **an HTTP transport (read tail + submit) + a snapshot encoder + a follower loop + a
coordinator-side dedup/key-resolve step**, on locked contracts. The correctness-critical
machinery already exists and is conformance-tested.

## 4. Transport (v0 = four verbs)

Writer-served HTTP, msgpack frames reusing the COMMIT-DELTA-v1 wire format. **Outbound-only**
(NAT-friendly): the edge follower GETs the tail and POSTs contributions; it never listens.

| Verb | Purpose |
|---|---|
| `GET /v1/snapshot?at=<tid>` | bootstrap / re-bootstrap; returns `{watermark, checksum, state}` |
| `GET /v1/deltas?after=<tid>` | catch-up **and** live tail (same endpoint, held open); ordered frames; re-bootstrap signal if `<tid>` is below the retention horizon |
| `POST /v1/submit` | contribute-write fan-in; idempotency-keyed batch; returns `{applied_tid, key→oid}` |
| `GET /v1/types` | type-lineage schema (for the shared-package fallback, §6) |

- **Live tail = the held-open `/deltas` response, not a second mechanism** (CouchDB `_changes`
  model: `since=<watermark>` resume, same call held open for live). **SSE is an optional packaging
  of this, deferred** — v0 can ship pull + held-open response and add an `Accept:
  text/event-stream` variant later.
- **The watermark is the source of truth.** A dropped/duplicated frame costs nothing: reconnect
  and reconcile from the watermark; COMMIT-DELTA-v1 is apply-twice ≡ apply-once.
- **Snapshots are never streamed on the delta channel** — separate one-shot GET (Debezium/Kafka
  snapshot-then-stream discipline).
- **Not GraphQL** (that is the *external* surface in `datacrystal[web]`), **not WebSockets** (no
  bidirectional need — the tail is server→client, contributions are POSTs), **not a broker** (the
  writer is already the distinguished node).

## 5. Correctness seams (read + contribute), mostly already enforced

Each is a real failure mode from prior art; most are already guaranteed by datacrystal primitives.

1. **Bootstrap completeness before serving** — Turso shipped a silent zero-filled replica
   (`turso#5971`). The follower must verify `watermark` + `checksum` before serving; never expose
   partial state.
2. **No apply across a gap** — refuse `tid > watermark+1`, re-bootstrap. (`DeltaGapError` already.)
3. **Atomic per-watermark flip** — reads switch state only at a consistent watermark. (P3 flip.)
4. **Snapshot↔delta handover** — bootstrap at `snapshot.tid`, apply strictly `after` it.
   (`deltalog.bootstrap()`.)
5. **Retention horizon → re-bootstrap** — the coordinator keeps a bounded delta window (operator
   policy, as `deltalog` already frames it); a follower older than the window re-bootstraps. *Bound
   it deliberately:* unbounded log = disk exhaustion (Debezium's #1 failure), bounded = re-bootstrap
   cost. There is no third option.
6. **No re-bootstrap thundering herd** — serialize with backoff + jitter; the coordinator may
   rate-limit `/snapshot`.
7. **Idempotent contribution (the write seam, kept minimal):**
   - **Prefer natural keys.** Contributed entity types should carry a `Unique` key (path/URL/hash).
     `upsert`-by-key is idempotent, so a retry after a lost ACK re-merges rather than duplicating —
     this is what makes contribute-writes safe *without* heavy machinery.
   - **Batch idempotency key for the keyless/atomic case.** When a batch has no natural key (or
     must be all-or-nothing), the follower attaches a caller-generated key; the coordinator records
     `(key → applied_tid)` **inside the same commit txn** and returns the recorded result on retry
     (Stripe/Brandur/DBOS). At-least-once delivery, exactly-once *effect* — never promise
     exactly-once on the wire.
   - **OID assignment on ACK.** The writer owns the OID/TID sequence; the follower references
     existing entities by real OID and its own new entities by natural key; the coordinator
     resolves keys→OIDs (including intra-batch references) and returns the map. A follower never
     mints shared-graph OIDs.

Net-new correctness code in v0 is small: the bootstrap checksum guard (#1), surfacing gaps as
re-bootstrap (#2), the retention window + horizon check (#5), backoff (#6), and the
coordinator-side dedup/key-resolve (#7). The rest is reuse.

## 6. Schema distribution

- **Common types = a shared Python package** both coordinator and followers import. Best DX; keeps
  static typing, pyright, and the magic query syntax. **Recommended as the only v0 path.**
- *Fallback (defer / maybe cut):* synthesize read-only classes from `GET /v1/types` (possible — the
  schema-evolution tests already fabricate classes dynamically — but costs static typing). The
  minimality recommendation is to **cut it from v0** and require the shared package.
- **Private types** live in the follower's own local store (a separate `Store` the app opens). No
  unified two-store façade in v0 — keep it out until demand is real.

## 7. The harder write path (deferred — what v0 deliberately omits)

Contribute-writes (§3b) is **in**. What stays out is **concurrent editing of the same shared
state**, because that is where the genuinely hard distributed-systems decisions live:

1. **Conflict policy for shared fields.** Two writers (or a follower and the coordinator) mutate
   the same field of the same existing entity. The intended model when this lands is
   **LWW-with-authority** (the coordinator is authoritative; `upsert` already writes only changed
   fields) — *not* CRDT (ElectricSQL abandoned that; preserving invariants under merge needs
   unbounded machinery). For callers needing stronger guarantees, an **opt-in optimistic-reject**
   (`expected_prior_tid` → `ConflictError`) is the rqlite-style lever.
2. **A read-your-writes / freshness dial.** After a fan-in, the replica reflects the change only
   when the delta loops back. v0's indexer pattern does not need synchronous self-reads; when
   shared-edit workflows arrive, offer `submit()`-returns-TID + `wait_for(tid)` (LiteFS's
   TXID-cookie trick) as an opt-in, never the default.
3. **Cross-entity transactional contributions spanning shared mutations** — beyond the
   append-mostly batch v0 supports.

These are well-understood but not "minimal," and the indexer persona doesn't need them — so they
wait for an explicit decision (§12).

## 8. Out of scope (the cut-list)

- **Concurrent shared-field edits + conflict dial** (§7) — deferred; contribute-writes covers the
  persona.
- **Automatic failover / promotion** — needs network lease fencing (item 16). v0 = single
  coordinator + Litestream PITR. No half-measure (split-brain risk); LiteFS pays for this with
  FUSE + Consul — we decline.
- **Partial / filtered replication** — followers replicate the whole shared graph in v0. Filtered
  subsets are the recurring tax in *every* prior-art system (ElectricSQL shapes, PowerSync buckets,
  CouchDB filters — storage-engine rewrites or CPU sinks). The escape hatch is the proven one:
  **many small stores** (Turso/LiteFS "DB-per-tenant").
- **Multi-writer / CRDT** — **Never** (charter). The one unique thing the design owns. Refuse loudly.
- **Broker transport** (NATS/Kafka), **S3-primary backend** (item 16) — later variants behind the
  same interface.
- **Built-in monitoring/alerting** — v0 exposes `watermark`/`delta_lag` hooks; dashboards are docs.

## 9. Critical review — is the architecture effective?

**Verdict: the topology is proven and right to be single-writer; the risk is seam-detail
discipline, not architecture. Contribute-writes by natural key is the low-risk write subset that
delivers the indexer persona without opening the conflict-resolution can of worms.**

- **Single-writer is validated by others' scars.** ElectricSQL — the team that *invented*
  Rich-CRDTs — abandoned its CRDT core because preserving relational invariants (FK, uniqueness,
  sequences) under merge needs ever-growing machinery; they rebuilt with Postgres as sole
  authority. CouchDB's multi-master silently hides conflicting writes ("not lost, just hidden") —
  apps that never check lose edits. dqlite's custom embedded Raft is a documented maintenance sink
  (write amplification — TBs for GBs, CPU spikes, fragile failover). **Our Never-list is these
  lessons a priori.**
- **Reuse of COMMIT-DELTA-v1 starts where Turso *ended up.*** Turso shipped physical-page
  replication and rebuilt it as logical CDC (8.9×–312× better) after page-waste bit them. We ship
  logical deltas already.
- **Contribute-by-upsert-key is the field-tested easy case.** Idempotent ingestion keyed by a
  natural identifier is exactly how CDC sinks and document indexers stay correct under at-least-once
  delivery. We get it from existing `Unique` + `upsert`.
- **Honest cons (do not hide them):**
  - **Eventual consistency** — followers are stale by delta-lag; a contributing node sees its own
    write when the delta returns, not synchronously. Fine for indexers; the freshness dial (§7.2)
    is the future answer for interactive shared-edit apps.
  - **Re-bootstrap of a large store from one coordinator is a real stall** (snapshot transfer +
    index rebuild; cf. MaStR eval ~15.6k obj/s ingest, ~32 s cold reopen for 6.2 M objects).
    Bounded retention + chunked snapshots mitigate.
  - **"Same `commit()` shape" is a partial truth** (Waldo): local reads are honestly transparent;
    a follower `commit()` is a *network fan-in* with latency and partial-failure semantics — it
    must surface those (await, ACK, idempotent retry), not pretend to be a local commit.
  - **Natural-key discipline is now load-bearing.** Contribution safety relies on contributed
    types having a unique key; a keyless contribution falls back to the batch idempotency key.
    Document this as a first-class requirement, not an afterthought.
  - **Manual failover is a footgun** without fencing — hence cut (§8).

## 10. Performance envelope (honest)

- **Reads:** local-replica reads are sub-ms — the reason to mirror, not RPC-proxy (a 10-edge
  `Lazy.get()` traversal proxied = 10 round-trips; replicated = local).
- **Writer throughput:** unchanged single-writer ceiling (~15.6k obj/s, MaStR eval); all
  contributions serialize through the one writer (the global bottleneck, by design).
- **Contribution latency:** a fan-in `commit()` is a network round-trip + the coordinator's commit
  — batch generously; this is publish-and-move-on, not an interactive write.
- **Delta fan-out is ~O(1) per commit, not O(N):** produced once, each follower pulls
  independently; the ceiling is coordinator egress + held-open connection count (fine for tens of
  followers; a broker/CDN variant is the lever beyond).
- **The trap — write amplification under (future) partial need:** whole deltas to many followers
  that each want a subset; the documented reason partial replication is deferred (§8).

## 11. Maintenance surface (what must be chased)

Deliberately small: an HTTP transport (read tail + submit), a snapshot encoder, the follower loop,
the coordinator-side dedup/key-resolve, and error translation (remote failures → the same local
exception types). **No Raft, no FUSE, no broker, no consensus, no new storage backend** — the four
things that made dqlite/LiteFS/Electric/CouchDB expensive. The wire rides the **locked**
COMMIT-DELTA-v1, so the contract does not drift. Ongoing burden is FastAPI/httpx upkeep and
keeping error-translation in sync — both bounded.

## 12. Open questions (decision-forcing — for review)

1. **Is contribute-writes the right v0 write scope** (insert/own-edge upsert, idempotent by natural
   key) — with concurrent shared-field edits explicitly deferred? Confirms the persona is served
   without opening conflict resolution.
2. **Natural-key requirement:** acceptable to *require* a `Unique` key on contributed types (with a
   batch idempotency key as the keyless fallback)? This is the linchpin of low-risk contribution.
3. **Packaging:** both the coordinator router *and* the `open_follower()` client live in
   `datacrystal[web]` (one extra), correct? Or a separate `datacrystal[replica]` for the client.
4. **Schema:** require the shared Python package and **cut** `/v1/types` synthesis from v0?
5. **Retention horizon default** for the delta window (time / commit-count / both) — and is
   "aged-out follower → hard re-bootstrap" acceptable v0 behaviour?
6. **Snapshot encoding:** reuse the web snapshot-pool view, or a dedicated chunked encoder from the
   start (large stores stall otherwise)? Checksum: xxhash64 vs sha256?
7. **When (if ever) the harder write path (§7)?** Confirm LWW-with-authority + opt-in
   optimistic-reject + opt-in `wait_for(tid)` as the intended model when shared-edit demand arrives.
8. **Does this become scope at all** (item 21 made concrete + a VISION line), or stay an
   exploration while the search/index roadmap takes priority?

## 13. References

- Companion: [fractal-followers-prior-art.md](2026-06-20-fractal-followers-prior-art.md) — the
  systems, experiences, pros/cons, and citations behind every claim here.
- [transport memo](2026-06-11-replication-transports.md) — why writer-served HTTP/SSE.
- [ROADMAP](../design/ROADMAP.md) (items 21, 16; the **Never** list),
  [ADR-001](../design/ADR-001-concurrency-contract.md) (owner confinement + `store.submit` fan-in),
  [SCALING.md](../design/SCALING.md), [COMMIT-DELTA-v1](../design/COMMIT-DELTA-v1.md),
  [deltalog](../../src/datacrystal/deltalog.py), `datacrystal[web]` (`src/datacrystal/web/`).
