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
- **Concurrent edit of shared state (resolution deferred):** two writers mutating the *same field of
  the same existing entity*. v0 **detects this and rejects it loudly** (§3c) — it never silently
  last-write-wins; what it defers is *automatic resolution* (auto-merge/LWW/CRDT) and the
  read-your-writes dial. The indexer persona doesn't need those, so v0 ships detect-and-reject only.

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

**Same code shape as a local write.** The app calls `store()`/`upsert()` to *earmark* (buffer)
and `commit()` to flush — identical to a local store. The one honest difference (Waldo): a local
`commit()` writes locally and always succeeds barring validation; a follower `commit()` is a
**network fan-in** that can be rejected loudly (§3c). The buffering, dirty-tracking and graph
discovery are reused unchanged.

**What makes this the *easy* write case** (see §1): contributed types carry a natural key, so step 2
is idempotent (re-`upsert` by path/URL/hash is a no-op, not a duplicate — a network blip after
apply-before-ACK is safe to retry); existing cloud entities are referenced by their real OID (read
from the replica); new own-entities are referenced by natural key and resolved by the coordinator.
No conflict-*resolution* machinery is needed — but the coordinator still *detects* a genuine
collision and refuses loudly rather than silently overwriting (§3c).

### 3c. Loud conflict detection (not silent last-write-wins)

A contribution that would overwrite a shared field someone else changed must **fail loudly**, never
silently last-write-win — silent LWW is the CouchDB "hidden conflict = data loss" anti-pattern and
violates datacrystal's *"make any lossy decision loud"* invariant. So v0 ships conflict
**detection**, and defers only conflict **resolution policy** (auto-merge/LWW/CRDT — §7).

**v0 chooses optimistic concurrency (OCC) as the detection rule** — it is a strict superset of
insert-only and handles both cases under one uniform check:

- The follower sends, per contributed entity, the **base version** it read — either *"new"* (this
  key should not exist) or the entity's last-write TID (`StoredRecord.tid` already records "the
  commit that wrote it").
- The coordinator compares and applies only if the base still holds; otherwise it raises loudly —
  `UniqueViolationError` (a *"new"* key already exists) or `ConflictError` (the entity moved since
  the follower read it). The app re-reads and retries.

This is the rule the indexer persona actually needs: it covers **insert** (base *"new"*) *and*
**safe update** (re-index a changed file → update `Document.text` only if it hasn't moved), without
the v0→later **semantic break** that insert-only would force the day an update is required.
Insert-only is just the special case `base = "new"` always — appropriate for genuinely append-only
data (events, immutable docs), and a possible *per-type* refinement later, but not the v0 default.
The new plumbing is small (carry the base version out and back); no engine surgery — the
coordinator is a normal datacrystal writer, so detection is just `get(Type, key=…)` → compare →
`upsert`/`commit` or raise.

### 3d. Why this is a facade, not a new storage backend

Tempting but **wrong**: implementing the federation as an alternative `StorageBackend` (the
`boot/load_many/scan_type/apply/read_view` Protocol). By the time `apply(CommitBatch)` runs, OIDs
are **already minted** (at `store()` time — `_register_graph`, `_store.py:2056`) and the **TID is
already allocated** (P1) — from the *follower's* sequence, which the coordinator does not own. A
fan-in backend would ship follower-local OIDs/TIDs that diverge from the authoritative ones.

So write-redirect must sit **above** the engine, at the `Store` facade's `commit()`, where it can
serialize *intent* (new entities by natural key + fields + base version; existing refs by real OID)
and let the coordinator allocate. Consequence — the reuse/new split:

- **Read replica = a normal local store** (sqlite/memory backend) fed by the existing `deltalog`
  applier over the network tail. ~100% reuse; reads work unchanged; **not** a new backend.
- **Follower = a thin facade** that overrides `commit()` to fan in (and runs a delta-apply loop).
  The object engine, dirty tracking, codec, indexes, query planner, snapshots, delta format,
  delta applier and storage backends are all **reused**. Net-new is the transport, the snapshot
  encoder, the follower's `commit()` override, and the coordinator-side dedup/key-resolve/detect.

(If ever built, the follower's "new entity gets a local OID at `store()`, coordinator reassigns the
real one" remapping deserves an ADR — it is facade-level, not engine-level.)

### 3e. What is net-new vs reused — the minimality claim

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

## 4. Transport (v0 = four data verbs + a trivial `/head`)

Writer-served HTTP, msgpack frames reusing the COMMIT-DELTA-v1 wire format. **Outbound-only**
(NAT-friendly): the edge follower GETs the tail and POSTs contributions; it never listens.

| Verb | Purpose |
|---|---|
| `GET /v1/snapshot?at=<tid>` | bootstrap / re-bootstrap; returns `{watermark, checksum, state}` |
| `GET /v1/deltas?after=<tid>` | catch-up **and** live tail (same endpoint, held open); ordered frames; re-bootstrap signal if `<tid>` is below the retention horizon |
| `POST /v1/submit` | contribute-write fan-in; idempotency-keyed batch; returns `{applied_tid, key→oid}` |
| `GET /v1/types` | type-lineage schema (for the shared-package fallback, §6) |
| `GET /v1/head` | the coordinator's current `last_tid` — a trivial liveness probe **and** the followers' "how far behind am I" reference |

**Three delivery modes, one endpoint — start simple, upgrade transparently.** `?after=<tid>` is the
whole contract; the follower's logic ("apply after my watermark, advance") is identical across all
three, so you can begin with plain polling and move to long-poll or SSE later with **zero** protocol
or follower-logic change:

| Mode | v0? | Latency | Moving parts |
|---|---|---|---|
| **Plain poll** | ✅ default | = poll interval | fewest — periodic GET, empty if nothing new |
| **Long-poll** (held-open GET until a delta or timeout) | cheap upgrade | near-real-time | one held connection; CouchDB `feed=longpoll` |
| **SSE** (server pushes frames) | deferred | lowest | streaming-connection management |

For an indexer, eventual-consistency lag is fine by construction, so **plain polling is enough for
v0**; long-poll is the cheap middle if near-real-time is wanted before committing to SSE.

- **Observability falls out of the poll** — the sync poll already tells the follower its
  `delta_lag` (local watermark vs `/v1/head`) and its connectivity, so a follower needs **no
  separate healthcheck loop**. The coordinator stays **stateless re: followers** in v0 (no follower
  registry — cut-list §8); follower liveness is the follower's/orchestrator's concern.
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
8. **Loud conflict detection via OCC (§3c).** Each contribution carries the base version it read;
   the coordinator rejects with `UniqueViolationError` (a *"new"* key already exists) or
   `ConflictError` (the entity moved) — never a silent overwrite. Resolution policy is deferred
   (§7); detection is in v0.

Net-new correctness code in v0 is small: the bootstrap checksum guard (#1), surfacing gaps as
re-bootstrap (#2), the retention window + horizon check (#5), backoff (#6), the coordinator-side
dedup/key-resolve (#7) and conflict detection (#8). The rest is reuse.

## 6. Schema distribution

- **Common types = a shared Python package** both coordinator and followers import. Best DX; keeps
  static typing, pyright, and the magic query syntax. **Recommended as the only v0 path.**
- *Fallback (defer / maybe cut):* synthesize read-only classes from `GET /v1/types` (possible — the
  schema-evolution tests already fabricate classes dynamically — but costs static typing). The
  minimality recommendation is to **cut it from v0** and require the shared package.
- **Private types** live in the follower's own local store (a separate `Store` the app opens). No
  unified two-store façade in v0 — keep it out until demand is real.

## 6a. Developer experience — a library, not a framework

The whole point of L2 (helpers, in `datacrystal[web]`) is that an app developer writes **their
domain + their indexing logic**, and *nothing* of the replication machinery (no sync loop, no delta
plumbing, no submit endpoint, no OID reassignment). Two entry points:

```python
# COORDINATOR app — a normal datacrystal app; mount one router to federate
store = dc.Store.open("cabinet.store")
app = FastAPI()
app.include_router(dc.web.federation_router(store))   # serves /v1/snapshot, /deltas, /submit
# (optionally also dc.web's existing reflection router for browser REST/GraphQL)

# FOLLOWER app (e.g. a MacBook markdown indexer behind NAT) — one call gets a synced replica
store = dc.open_follower("https://coordinator", api_key=...)
known = {d.path for d in store.query(Document)}       # fast LOCAL read of the shared graph
for path in scan_local_files():                       # specialised data only this node sees
    if path not in known:
        store.upsert(Document(path=path, text=extract(path)))
store.commit()                                        # fan-in to the coordinator (loud on conflict)
```

This mirrors the precedent already in `datacrystal[web]`: reflection turns an `@entity` into
REST/GraphQL with one call. Federation is the same move for the *machine-to-machine* path — the
mountable router + `open_follower()` are the public surface; the L1 engine sits behind them. A
**library you call into**, not a framework that owns your app's shape.

**Polling — automatic by default, explicit if you want control.** `open_follower()` runs the
sync poll loop for you (plain poll in v0, §4); reads are always served from the local replica at
the latest applied watermark, and `delta_lag`/`watermark` are exposed so no separate healthcheck
loop is needed. An app that prefers to drive sync itself (sync-on-demand, or to interleave with its
own work) can turn the loop off and call `sync()`:

```python
# Automatic (default): background poll keeps the replica current
store = dc.open_follower("https://coordinator", api_key=..., poll_interval=2.0)
...
if store.delta_lag > 0:
    log.info("behind coordinator by %d commits", store.delta_lag)   # observability for free

# Explicit: drive the poll yourself (one GET /v1/deltas?after=<watermark> per call)
store = dc.open_follower("https://coordinator", api_key=..., auto_poll=False)
while running:
    applied = store.sync()          # fetch+apply deltas after the watermark; returns count
    reindex_changed(store)          # your indexing work against the now-current replica
    store.commit()                  # contribute results back (fan-in; loud on conflict/skew)
    time.sleep(2.0)
```

(Illustrative names — nothing shipped.) Upgrading to long-poll or SSE later changes only the
transport inside `open_follower`/`sync`, not this code.

**Auth & extensibility — you bring authentication; the router stays agnostic.** Following the
existing web extra's seam (dependency injection — `get_store`/`read_snapshot`/`submit_write` are all
`Depends`, and `create_app(path, routers=[...])` only composes routers), the federation router
carries **no** built-in auth. You inject your own — API key, JWT, OAuth introspection, mTLS header —
as a FastAPI dependency:

```python
async def verify(request: Request) -> Principal:
    return await my_auth.check(request.headers.get("authorization"))   # raise 401/403 yourself

app.include_router(dc.web.federation_router(store), dependencies=[Depends(verify)])
```

Three levers, none requiring a fork: **route/router-level `dependencies=[Depends(...)]`** (your
auth + principal); **middleware** (rate-limit, CORS, IP allowlist); **selective mount** (it is a
plain `APIRouter` — mount under a prefix, skip a route and supply your own, or wrap `/submit` to
enforce per-principal authorization, e.g. "this key may contribute `Document` but not `Mineral`").
Symmetric on the client: `open_follower(url, …)` takes configurable credentials (`api_key=` /
`headers=` / an `auth=` callable) so the follower sends whatever the coordinator expects. The
commitment: **the router ships the federation *mechanism*; the app owns authentication and
authorization.**

## 7. The harder write path (deferred — what v0 deliberately omits)

Contribute-writes (§3b) is **in**, and v0 *detects* conflicts loudly (§3c). What stays out is
conflict **resolution** — automatically reconciling **concurrent edits of the same shared field** —
because that is where the genuinely hard distributed-systems decisions live:

1. **Conflict resolution policy for shared fields.** When two writers mutate the same field of the
   same existing entity, v0 rejects loudly and the app retries. *Automatic* reconciliation is
   deferred; the intended model when it lands is **LWW-with-authority** (the coordinator is
   authoritative; `upsert` already writes only changed fields) — *not* CRDT (ElectricSQL abandoned
   that; preserving invariants under merge needs unbounded machinery). The opt-in optimistic-reject
   (`expected_prior_tid` → `ConflictError`) is already the v0 *detection* lever (§3c); what's
   deferred is any auto-merge on top of it.
2. **A read-your-writes / freshness dial.** After a fan-in, the replica reflects the change only
   when the delta loops back. v0's indexer pattern does not need synchronous self-reads; when
   shared-edit workflows arrive, offer `submit()`-returns-TID + `wait_for(tid)` (LiteFS's
   TXID-cookie trick) as an opt-in, never the default.
3. **Cross-entity transactional contributions spanning shared mutations** — beyond the
   append-mostly batch v0 supports.

These are well-understood but not "minimal," and the indexer persona doesn't need them — so they
wait for an explicit decision (§12).

## 7a. Schema evolution under rollout skew

Schema changes reach nodes at different times. datacrystal's evolution is **additive type-lineage,
not strict version-equality** (invariant 8): each shape change mints a new `cid`; records decode by
name, filling missing fields from defaults and silently ignoring unknown ones. So a coordinator and
follower on different versions of the same typename do **not** reject each other on sight — but the
two rollout directions are **asymmetric**, and one hides a silent-loss trap.

- **Coordinator-first (coordinator newer) — safe.** A follower submits an old-shape record; the
  coordinator hydrates through its newer class. A new field **with a default** fills silently
  (*accepted*); a new field **without a default** raises `SchemaMismatchError` in P1 — a **loud
  reject** until the follower upgrades. An old follower reading the coordinator's newer deltas just
  *ignores* fields it doesn't know → a truncated **view** (read-blindness), not data loss. So
  coordinator-first only rejects for *non-additive* changes; additive ones flow through.
- **Follower-first (follower newer) — silent-loss trap.** A newer follower contributes a field the
  coordinator's class lacks. The naive fan-in (coordinator reconstructs through its own class +
  `upsert`, which iterates only the coordinator's field names — `_store.py:690`) **silently drops
  the extra field; the commit succeeds, no error.** This is the CouchDB hidden-loss anti-pattern,
  and it is **not** recovered when everyone upgrades — the data was never written.

So **schema compatibility self-heals** once all nodes upgrade, but **data silently truncated in a
follower-first window is gone** — this is *not* free; it needs a guard. Two safe designs:

- **Guard + coordinator-first discipline (minimal, v0):** the coordinator validates the submitted
  record's `cid`/field-set against its known lineage for that typename (`_cids_by_typename` already
  exists; the delta applier already rejects unknown cids — `applier.py:126`). Unknown →
  `SchemaSkewError` **loudly**, instead of dropping. Turns follower-first into a clean "upgrade the
  coordinator first" error. O(1) lookups.
- **Store-by-lineage (fuller fidelity, later):** the coordinator persists the submitted record under
  its shipped lineage (as the delta applier already does for reads), preserving forward fields it
  can't yet interpret — follower-first then also works, the coordinator merely read-blind to the new
  field until it upgrades. More work (persist a record whose class it lacks; decode only the known
  key fields for OID/key resolution).

**Recommendation:** roll out **coordinator-first** and build the **cid-lineage guard** so
follower-first fails loudly, never silently. Then self-healing holds for the schema in both
directions and no write is ever silently truncated. (Store-by-lineage is the optional upgrade for
true follower-first fidelity.) If this becomes scope, the guard + the fan-in decode policy warrant
an ADR (storage/contract-adjacent).

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
   key) — with conflict **resolution** deferred but loud **detection** (§3c) in? Confirms the
   persona is served without a silent-LWW footgun.
2. **Natural-key requirement:** acceptable to *require* a `Unique` key on contributed types (with a
   batch idempotency key as the keyless fallback)? This is the linchpin of low-risk contribution.
2b. **Detection rule — DECIDED: OCC (§3c).** v0 uses optimistic concurrency (carry the base
   version, reject on staleness) rather than insert-only, because the indexer re-indexes changed
   files and so needs safe *update*, not just insert. Insert-only stays a possible per-type
   refinement, not the default.
3. **Packaging:** both the coordinator router *and* the `open_follower()` client live in
   `datacrystal[web]` (one extra), correct? Or a separate `datacrystal[replica]` for the client.
4. **Schema:** require the shared Python package and **cut** `/v1/types` synthesis from v0?
4b. **Schema-skew guard (§7a):** confirm coordinator-first rollout + a cid-lineage guard on `/submit`
   (loud `SchemaSkewError` on unknown shape) for v0 — with store-by-lineage as a later fidelity
   upgrade? Without the guard, follower-first rollout silently truncates contributed fields.
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
