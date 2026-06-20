# Fractal followers — a minimal single-writer replication design (design exploration, 2026-06-20)

**Status: design exploration / proposal. Nothing here is shipped. No commitment implied — this
needs a `needs-owner-decision` + a VISION entry before any story.** It rides ROADMAP **punted item
21** (networked replication, `datacrystal[replica]`); failover touches **item 16** (lease
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
full local speed against the *same* API.

**The one decision that makes this minimal: v0 is read-path-only.** A follower is a **read-only
replica**; it cannot `commit()`. This is deliberate, not a limitation — it is the same move
ElectricSQL made when it abandoned its CRDT/active-active core for a read-path sync engine
([prior-art §ElectricSQL](2026-06-20-fractal-followers-prior-art.md)). **Every hard
distributed-systems hazard lives in the write path** (idempotency, conflict resolution, deferred
identity, read-your-writes). Cutting writes from v0 removes essentially all of them, so v0 is
*almost entirely transport + bootstrap over already-locked, already-tested contracts* — not new
distributed-systems correctness. That is why it can be both **super-minimal** and **correct**.

Writes-from-followers (command fan-in) are a real, valuable, but **separate and harder** phase
(§7), shipped only after its non-negotiable seams are decided. Failover and partial replication
are explicitly out (§8).

## 2. The fractal property (roles)

| Role | Lease? | Writes | Reads | `dc.Store.open(...)` |
|---|---|---|---|---|
| **Coordinator** | holds it | local, authoritative; owns OID/TID sequences | local | `open("cabinet.store")` — unchanged; also serves the federation routes |
| **Follower (v0)** | none | **none** (`ReadOnlyStoreError`) | local replica, full-speed | `open("https://coord", api_key=…)` → bootstrap + tail |

Same binary, role chosen by a flag + a coordinator URL. A follower's headless job (e.g. "index
markdown files") reads the shared graph locally and writes its **results** either to an external
sink or to its **own private local store** — never back into the shared graph in v0. Because the
codebase is symmetric, a follower *could* later be promoted to coordinator (failover, §8) — the
architecture enables it; v0 does not ship it.

## 3. v0 — the minimal core (read-only followers)

A follower is a remote-fed replica. Lifecycle:

```
cold start :  GET /v1/snapshot?at=<tid>  -> materialize local replica, watermark = tid
catch up   :  GET /v1/deltas?after=<wm>  -> apply in TID order, advance watermark
steady     :  (same endpoint, held open) -> apply live frames as they arrive
reconnect  :  GET /v1/deltas?after=<wm>  -> reconcile from watermark (idempotent), resume
aged out   :  GET /v1/snapshot?at=<tid>  -> re-bootstrap (watermark < retention horizon, §5)
```

**What is net-new vs reused — the minimality claim, concretely:**

| Need | Provided by (today) | Net-new in v0 |
|---|---|---|
| Ordered, idempotent, gapless change stream | COMMIT-DELTA-v1 (locked) | — |
| Apply a delta into a store, replay from 0, mid-life join | `deltalog` applier + `bootstrap()` | wire it to a remote feed |
| Atomic per-watermark state flip (no torn reads) | commit P3 flip (ADR-001) | — |
| Gap refusal (`tid > watermark+1`) | `DeltaGapError` | surface it as "re-bootstrap" |
| Frozen consistent read state to serialize a snapshot | `Snapshot` + the web snapshot pool | a `/snapshot` encoder |
| Batch reads / schema | `get_many`, TypeInfo, reflection | HTTP framing |

So v0 ≈ **an HTTP transport + a snapshot encoder + a follower loop**, sitting on locked contracts.
That is the whole point: the correctness-critical machinery already exists and is conformance-tested.

**API (v0):** `dc.Store.open("https://…", api_key=…)` returns a read-only follower whose
`query/get/get_many/count/pluck/Lazy.get/incoming` run locally; mutating calls raise
`ReadOnlyStoreError`. A follower exposes its `watermark` and `delta_lag` for observability.

## 4. Transport (v0 = three verbs)

Writer-served HTTP, msgpack frames reusing the COMMIT-DELTA-v1 wire format. **Outbound-only**
(NAT-friendly): followers GET; they never listen.

| Verb | Purpose |
|---|---|
| `GET /v1/snapshot?at=<tid>` | bootstrap / re-bootstrap; returns `{watermark, checksum, state}` |
| `GET /v1/deltas?after=<tid>` | catch-up **and** live tail (same endpoint, held open); ordered frames; `404`/sentinel if `<tid>` is below the retention horizon |
| `GET /v1/types` | type-lineage schema (for the shared-package fallback, §6) |

- **Live tail = the held-open `/deltas` response, not a second mechanism.** The CouchDB `_changes`
  feed is the proven model: `since=<watermark>` for resume, the same call held open for live
  ([prior-art §CouchDB feed](2026-06-20-fractal-followers-prior-art.md)). **SSE is an optional
  packaging of this, deferred** — v0 can ship pull + held-open response and add an `Accept:
  text/event-stream` variant later for one fewer moving part now.
- **The watermark is the source of truth.** A dropped/duplicated frame costs nothing: reconnect
  and reconcile from the watermark; COMMIT-DELTA-v1 is apply-twice ≡ apply-once.
- **Snapshots are never streamed on the delta channel** — separate one-shot GET (Debezium/Kafka
  snapshot-then-stream discipline).
- **Not GraphQL** (that is the *external* surface in `datacrystal[web]`), **not WebSockets**
  (no bidirectional need), **not a broker** (operational cost a solo maintainer should not own;
  the writer is already the distinguished node).

## 5. Correctness seams for the read path (mostly already enforced)

Each is a real failure mode from prior art; most are already guaranteed by datacrystal primitives.

1. **Bootstrap completeness before serving** — Turso shipped a silent zero-filled replica
   (`turso#5971`): a half-materialized snapshot served reads with no error. **The follower must
   verify `watermark` + `checksum` before serving the replica**, never expose partial state.
2. **No apply across a gap** — refuse `tid > watermark+1`, re-bootstrap. (`DeltaGapError` already.)
3. **Atomic per-watermark flip** — reads switch state only at a consistent watermark, never
   mid-delta. (P3 flip already; PowerSync's checkpoint discipline is the same lesson.)
4. **Snapshot↔delta handover** — bootstrap at `snapshot.tid`, then apply strictly `after` it.
   (`deltalog.bootstrap()` already.)
5. **Retention horizon → re-bootstrap** — the coordinator must **not** retain deltas unboundedly
   (Debezium's #1 failure: a slow consumer pins the WAL and fills the disk). It keeps a bounded
   window (operator policy, as `deltalog` already states); a follower older than the window
   re-bootstraps. **Coordinator durability is bounded; replica freshness is the follower's
   problem.**
6. **No re-bootstrap thundering herd** — re-bootstrap is serialized with backoff + jitter; the
   coordinator may rate-limit `/snapshot`. (A known DOS vector; cheap to pre-empt.)

Net new correctness code in v0 is small: the bootstrap checksum guard (#1), surfacing the gap as
re-bootstrap (#2), the retention window + horizon check (#5), and backoff (#6). The rest is reuse.

## 6. Schema distribution

- **Common types = a shared Python package** both coordinator and followers import. Best DX; keeps
  static typing, pyright, and the magic query syntax. **Recommended as the only v0 path.**
- *Fallback (defer / maybe cut):* synthesize read-only classes from `GET /v1/types`. Possible (the
  schema-evolution tests already fabricate classes dynamically) but costs static typing. The
  minimality review's recommendation is to **cut it from v0** and require the shared package.
- **Private types** live in the follower's own local store. In v0, with read-only followers, the
  "two-store routing" question is mild: the replica is read-only, the private store is the
  follower's own writer. A small façade routes `query()` by type — but if even this is too much
  for v0, the private store can simply be a *separate* `Store` the app opens itself. **Cut the
  unified façade from v0.**

## 7. The write path (deferred — and where all the hazards are)

Fan-in writes are valuable (followers enriching the central graph) but are a **separate phase**,
because the four hazards below are exactly what v0 avoids. None is shippable without its seam:

1. **Idempotency is mandatory.** At-least-once is the only network guarantee; a follower whose
   `POST /v1/submit` times out cannot know if it applied. Every command carries a
   **caller-generated idempotency key**; the writer records `(key → result/tid)` **in the same
   commit txn** as the write, and a retry returns the recorded result. Without this, retries
   silently double-apply (Stripe/Brandur; the #1 distributed-write bug).
2. **Conflict policy = LWW-with-authority, but unique fields need the old key.** The coordinator is
   the authority; field-level last-write-wins is the default (`upsert` already writes only changed
   fields). **But** for fields under a `Unique` index, the delta must carry the **old key** so a
   replica can validate the transition — silent LWW on a unique field corrupts the index
   (LiteFS uniqueness lesson; ADR-003 already carries prior payloads for deletes — extend it).
3. **Deferred identity for new objects.** The writer owns the OID/TID sequence; a follower cannot
   mint OIDs. A new object's OID returns via the delta; the follower must not cross-reference a
   not-yet-assigned object in the same batch (`NewObjectRefError`).
4. **Read-your-writes is opt-in, not default.** After a successful fan-in, the follower's replica
   reflects the write only when the delta loops back. Offer a **consistency dial** (rqlite's proven
   pattern): `none` (return on ack), `read_your_writes` (wait until `watermark ≥ applied_tid`,
   LiteFS's TXID-cookie trick), optionally `confirmed`. Making RYW the *default* hides the latency
   and surprises developers; making it *unavailable* breaks basic CRUD UX — so expose it.

These are well-understood; they are simply not "minimal," so they wait for an explicit decision.

## 8. Out of scope (the cut-list)

- **Automatic failover / promotion** — needs network lease fencing (item 16). v0 = single
  coordinator + Litestream PITR (today's answer). Manual promotion without fencing risks
  split-brain; do not ship a half-measure. (LiteFS pays for this with FUSE + Consul; we decline.)
- **Partial / filtered replication** — followers replicate the whole shared graph in v0. Filtered
  subsets are the recurring tax in *every* prior-art system (ElectricSQL shapes, PowerSync buckets,
  CouchDB filter functions — all needed storage-engine rewrites or became CPU sinks). Defer; the
  escape hatch is the proven one: **many small stores** (Turso/LiteFS "DB-per-tenant"), replicate
  the small one you need.
- **Multi-writer / CRDT** — **Never** (charter). The one unique thing the design owns. Refuse loudly.
- **Broker transport** (NATS/Kafka), **S3-primary backend** (item 16) — later variants behind the
  same follower interface; not v0.
- **Built-in monitoring/alerting** — v0 exposes `watermark`/`delta_lag` hooks; dashboards are L3
  docs, not core.

## 9. Critical review — is the architecture effective?

**Verdict: the topology is proven and the design is right to be single-writer; the risk is not
architecture, it is seam-detail discipline — and read-path-only v0 sidesteps the riskiest seams.**

- **The single-writer choice is validated by others' scars, not just theory.** ElectricSQL — the
  team that *invented* Rich-CRDTs — abandoned its CRDT/active-active core because preserving
  relational invariants (FK, uniqueness, sequences) under merge needs ever-growing per-invariant
  machinery; they rebuilt as read-path-only with Postgres as sole authority. CouchDB's multi-master
  model silently hides conflicting writes as non-winning revisions ("not lost, just hidden") —
  apps that never check lose user edits. dqlite's custom embedded Raft is a documented maintenance
  sink (write amplification — TBs for GBs, CPU spikes, fragile "no known leader" failover). **Our
  Never-list (no CRDT, no multi-writer, no homegrown consensus) is these lessons, a priori.**
- **Reuse of COMMIT-DELTA-v1 starts where Turso *ended up.*** Turso's first embedded-replica design
  shipped physical 4 kB-page replication and rebuilt it as logical CDC (8.9×–312× less data/faster)
  after page-waste and checkpoint-divergence bit them. datacrystal ships *logical* deltas already;
  it does not have to make that mistake.
- **Honest cons (do not hide them):**
  - **Eventual consistency is the only guarantee.** Followers are stale by delta-lag. Apps must
    design for it; "write then read your own write" needs the opt-in dial (§7.4).
  - **Re-bootstrap of a large store from one coordinator is a real stall** (snapshot transfer +
    index rebuild; cf. MaStR eval ~15.6k obj/s ingest, ~32 s cold reopen for 6.2 M objects). A
    multi-follower recovery can serialize. Bounded retention + chunked snapshots (later) mitigate.
  - **"Same `commit()`/API shape" is a partial truth** (Waldo, *A Note on Distributed Computing*):
    reads as a local replica are honestly transparent; the *write path is not* and must surface its
    different failure envelope explicitly. v0 makes this honest by refusing follower writes outright.
  - **Manual failover is a footgun** if offered without fencing — hence cut (§8).

## 10. Performance envelope (honest)

- **Reads:** local-replica reads are sub-ms — the entire reason to mirror rather than RPC-proxy (a
  10-edge `Lazy.get()` traversal proxied = 10 round-trips; replicated = local).
- **Writer throughput:** unchanged single-writer ceiling (~15.6k obj/s, MaStR eval). Replicas do
  not slow it.
- **Delta fan-out is ~O(1) per commit, not O(N):** the delta is produced once; each follower pulls
  independently. The realistic ceiling is the coordinator's egress + held-open connection count,
  not commit cost — fine for tens of followers; a broker/CDN variant is the lever beyond that.
- **The trap — write amplification under (future) partial need:** shipping whole deltas to many
  followers that each want a subset blows up bandwidth. This is the documented reason partial
  replication is hard and deferred (§8), not assumed away.
- **Eventual-consistency lag** for the indexer persona is acceptable by construction (an index is
  stale by design); make the poll/tail interval a first-class knob.

## 11. Maintenance surface (what must be chased)

Deliberately small: an HTTP transport, a snapshot encoder, the follower loop, and error
translation (remote failures → the same local exception types). **No Raft, no FUSE, no broker, no
consensus, no new storage backend** — the four things that made dqlite/LiteFS/Electric/CouchDB
expensive to operate. The wire rides the **locked** COMMIT-DELTA-v1, so the contract does not
drift. The ongoing burden is FastAPI/httpx version upkeep and keeping error-translation in sync —
both bounded.

## 12. Open questions (decision-forcing — for review)

1. **Read-path-only v0 — agreed?** Is "followers cannot write in v0; results go to a private store
   or external sink" acceptable for the markdown-indexer-fleet persona, or is fan-in a v0
   must-have (which pulls in §7 wholesale)?
2. **Packaging:** new `datacrystal[replica]` extra for the follower client, with serving routes in
   `datacrystal[web]`? Or fold the v0 client into `datacrystal[web]` too (one extra, not two)?
3. **Schema:** require the shared Python package (cut `/v1/types` synthesis from v0)?
4. **Retention horizon default** for the coordinator's delta window (time-based, commit-count, or
   both) — and is "follower aged out → hard re-bootstrap" an acceptable v0 behaviour?
5. **Snapshot encoding:** reuse the web snapshot pool's view, or a dedicated chunked encoder from
   the start (large stores stall otherwise)? Checksum algorithm (xxhash64 vs sha256)?
6. **When (if ever) write fan-in?** If yes, the four seams in §7 become the gating spec — confirm
   LWW-with-authority + opt-in consistency dial as the intended model before any build.
7. **Does this even become scope?** It is item 21 made concrete and needs a VISION line; or does it
   stay an exploration while the search/index roadmap takes priority?

## 13. References

- Companion: [fractal-followers-prior-art.md](2026-06-20-fractal-followers-prior-art.md) — the
  systems, experiences, pros/cons, and citations behind every claim here.
- [transport memo](2026-06-11-replication-transports.md) — why writer-served HTTP/SSE.
- [ROADMAP](../design/ROADMAP.md) (items 21, 16; the **Never** list),
  [ADR-001](../design/ADR-001-concurrency-contract.md), [SCALING.md](../design/SCALING.md),
  [COMMIT-DELTA-v1](../design/COMMIT-DELTA-v1.md),
  [deltalog](../../src/datacrystal/deltalog.py), `datacrystal[web]` (`src/datacrystal/web/`).
