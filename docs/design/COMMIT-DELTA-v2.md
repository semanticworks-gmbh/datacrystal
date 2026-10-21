# COMMIT-DELTA-v2 — the commit-delta / watermark contract, second version

Status: **v2 — LOCKED at the Permissions-W1 merge** (ratified 2026-07-12,
ADR-008 batch-1 rulings on #170; epic #168). Revisions from here are a new
contract version, never an edit. **Supersedes
[COMMIT-DELTA-v1](COMMIT-DELTA-v1.md) with no coexistence period** (§7) —
the owner's no-compat ruling for the v0.x stage: one live contract version
at a time, incompatibility is loud, never silent.

## 1. What changed vs v1

Two new **required** top-level keys — `actor` and `at` — stamping every
delta with *who* committed it and *when*. Everything else — the format
marker, ops vocabulary, types rows, prior semantics, the five consumer
obligations — is carried over from v1 verbatim. A commit has exactly one
actor and one instant, so the keys live per delta, never per op.

The keys are required, not optional (owner ruling, 2026-07-12): every v2
delta has the same shape, consumers never branch on presence, and the
conformance kit tests one format. A store opened without a principal stamps
the **anonymous actor `0`**.

## 2. Encoding

One delta = one msgpack map. Keys are short ASCII strings. Unknown keys MUST
be ignored by consumers (forward compatibility within a version); missing
required keys are a format error.

| key | type | meaning |
|---|---|---|
| `f` | str | format marker, exactly `"datacrystal-delta"` |
| `v` | int | contract version; this document specifies `2` |
| `tid` | int | the commit TID (strictly monotonic, gapless) |
| `actor` | int | uid of the committing principal; `0` = anonymous (no principal) |
| `at` | int | commit instant, integer **nanoseconds since the Unix epoch**, from the engine's injectable clock (§5) |
| `ops` | array | record operations, in capture order — v1 §3 verbatim |
| `types` | array | new type-lineage rows: `[cid, typename, [field, …]]` |
| `root` | int / nil | the root holder OID after this commit |

Ops (`upsert` / `delete`, payload/prior semantics, ADR-003 tombstones) are
unchanged from v1 §3 — this document does not restate them; the reference
applier remains normative where prose and code disagree.

## 3. Who the actor is

`actor` is the uid of the **committing** principal — the session identity in
effect when `commit()` ran, not when writes were buffered. It resolves
through the shipped `dc.Actor` registry (or an app-side identity source) to
a human or sponsored technical user; the delta stream itself carries only
the integer. Accountability semantics — sponsorship, membership history,
"who let 900 act" — live in the permissions concept
(`docs/research/2026-07-11-permissions/concept.md`) and ADR-008, not here:
the contract only promises the stamp is present and truthful.

## 4. Consumer obligations

Obligations 1–4 (watermark, idempotency, ordering, gap refusal) are v1 §4
verbatim. Obligation 5 sharpens under the no-compat stance:

5. **Version exactness.** A v2 consumer accepts exactly `v == 2`.
   `v > 2` MUST raise "newer than this consumer supports — upgrade the
   consumer" (unchanged from v1). `v < 2` MUST raise "pre-v2 delta —
   incompatible; recreate the stream" (new). Both directions fail loudly;
   neither is ever silently skipped or partially applied.

## 5. Determinism and the clock

- **TID stays the only ordering truth.** Wall clocks step and skew; `at` is
  informational local-clock time (the Datomic `txInstant` stance). GoBD
  date-time attribution is satisfied; tamper evidence remains the ledger's
  job (ROADMAP #19).
- **The clock is injectable** — an internal engine seam (`Store` clock,
  default `time.time_ns`), pinnable by tests, golden generators, and the
  determinism fitness gate. It is deliberately NOT public API (ADR-008
  batch-1 ruling); promoting it later is additive.
- **Fitness #5, amended** (KICKOFF): the delta stream is byte-for-byte
  deterministic **given the same injected clock and the same acting
  principals**. The replayed **state digest** stays unconditionally
  deterministic: `state_digest()` covers watermark, root, types and payload
  bytes only — `actor` and `at` are deliberately excluded, so audit stamps
  never perturb derived state.

## 6. Replay vectors

`src/datacrystal/contract/vectors/` holds the byte-pinned v2 vectors plus
`expected.json`. Under the no-compat ruling the vector set was
**regenerated wholesale for v2** (the v1 vectors retired with their
contract; v1's "regenerating is a draft-rev bump" rule is exactly what a
version bump is). The set pins: the standard lifecycle vectors re-authored
under v2 with a **pinned clock and fixed principals**, one vector stamped by
a real actor, and one stamped anonymous (`actor=0`) — the two ends of §1's
"always stamped" rule, documented in bytes. `tests/contract/` replays them
through the reference applier and asserts: final digest, apply-twice ≡
apply-once, gap refusal, and version refusal **in both directions** (§4.5).

## 7. No coexistence (the v0.x hard cut)

- **Pre-v2 delta logs are incompatible.** A `DeltaLog` written before this
  version does not replay: the applier refuses `v == 1` loudly (§4.5).
  Recreate retained logs from the live store (attach a fresh `DeltaLog`;
  the store can always rebuild any sidecar — invariant 11). There is no
  mixed-version log, no upgrade choreography, no dual-emission mode.
- **The federation wire carries v2 frames unchanged.** The
  [FEDERATION-WIRE-v1](FEDERATION-WIRE-v1.md) frame layout (`>Q`
  length-prefixed `encode_delta` bytes) and endpoints are untouched;
  `GET /v1/head` self-describes the carried contract version (it serves the
  shared constant). Follower-side version refusal is a **v2-build
  guarantee**: this build refuses mismatched frames loudly on bootstrap
  *and* on catch-up (`sync()` — the `_apply_catchup` version guard added in
  W1). A **pre-v2 build** refuses only on bootstrap; its catch-up path had
  no version check and applies v2 frames **silently** — so the upgrade rule
  is operational, not code: stop or upgrade every follower **before** the
  coordinator starts emitting v2, and never leave a pre-v2 replica syncing.
  The wire doc carries a dated amendment note recording the carried-version
  advance.
- **Why a bump at all** (and not v1's "unknown keys are ignored" loophole):
  emitting stamps under v1 would make audit fields droppable by contract
  and would break v1's byte-pinned golden vectors silently. An honest
  version bump is affordable at the library's stage — and with the
  no-compat ruling it costs one hard cut instead of a compatibility matrix.

## 8. Consumers: attach, DeltaLog, follower

- **Attach.** Deltas are not retained (v1 section 5), so a consumer that is behind the store cannot be caught up; it rebuilds from `store.snapshot()`, whose `tid` and `types` are exactly the bootstrap it needs. A consumer ahead of the store means the store was restored to an older point: the sidecar is stale and must be rebuilt (fitness function 13).
- **Prior payloads.** While consumers watch, the engine reads back the last durable payload of every already-persisted record, O(delta) reads, strictly before the TID is allocated, so a failed read rejects the commit without consuming a TID. Deletes always have a persisted record, and their tombstones carry it as `prior`.
- **Indexes are not a delta consumer.** The KICKOFF plan sketched the indexes as the second consumer; they deliberately are not (decided at M4), because a consumer would force prior-payload reads and delta builds on every commit while an unwatched store must pay nothing. The index keeps its own `oid -> last-indexed-values` memory and is folded in directly at P3.
- **Follower contribute.** A follower serializes its buffered NEW and DIRTY entities (a NEW one carries `base=None`, an edited one the SHA-256 of the payload it was read at) and POSTs them to `/v1/submit`. On success the local buffers clear and a `sync()` pulls the committed delta back; identity is by OID, so re-query for the coordinator-minted instance. A coordinator reject leaves the buffers intact. The payload digest reads the authoritative persisted bytes, not a re-encode of the live entity, whose lineage and defaults could diverge.
- **Follower sync.** A live reference to a CLEAN entity read before a sync sees stale values afterwards; re-query. The OCC recovery loop is `discard()`, `sync()`, re-read, re-apply, `commit()` (#153); `discard()` re-derives the live store from the backend's committed records.
- **Follower constructor.** `Store.open(path)` opens a local single writer, `Store.follower(url)` a replica of a remote coordinator; `api_key` is sent as `x-api-key`, and a custom `client` (an `httpx.Client`-compatible transport) takes over `url` and `api_key`.

## 9. DeltaLog (the retained delta consumer)

The engine never retains deltas (v1 section 5 stands); `datacrystal.deltalog.DeltaLog` is the consumer that does.
It rides `store.attach()` like every sidecar, needs no engine change and no new contract version, and uses only the standard library and msgspec, so the core dependency budget is untouched.

- **Storage.** Each flush appends the buffered deltas as length-prefixed frames (`>Q` byte count plus `encode_delta` bytes) to the current segment, which rolls once it passes `max_segment_bytes`.
- **Commit point.** `manifest.json` (temp file plus `os.replace`) names the durable watermark and the live segments with each one's committed byte length. It is fsynced strictly after the segment bytes, so the watermark never names bytes that did not land (v1 section 4.3).
- **Crash recovery.** A crash mid-flush leaves an unnamed orphan segment or trailing bytes past a named segment's committed length. The next open sweeps the orphans and truncates the trailing bytes, so the log is always an exact gapless commit prefix.
- **Durability against the store.** With `flush_every=1` the log is as durable as the store. With `flush_every=N` the durable watermark trails the live one by up to N-1 commits, and a crash in that window means a rebuild (the engine refuses a behind-the-watermark re-attach, so `bootstrap()` again).
- **Replay completeness.** A log that recorded from a fresh store (`watermark == 0` at attach) holds the whole history, and `replay()` plus the reference applier reproduce the committed state (`DeltaLog.replayed_state`). A log that bootstrap-attached to a lived-in store replays only the changes since it joined; a periodic checkpoint that would make it self-contained is a deliberate later enhancement.
- **Owner confinement.** `apply()` and `replay()` run on the owner thread. A log directory has one owner process; opening it twice concurrently is unsupported, because the orphan sweep would treat the other instance's segments as crash debris.
