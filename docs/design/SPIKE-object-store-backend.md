# SPIKE — Object-store (S3) storage backend

**Status:** research complete · feeds ROADMAP item 16 (S3-primary, *Punted*) · **decision pending (Sven)**
**Date:** 2026-06-14
**Method:** 4 independent research agents (EclipseStore lens · LanceDB/Lance lens · raw-S3-semantics lens ·
datacrystal-protocol-feasibility lens). All four converged — the agreements below are unanimous, not a
single opinion.

> This is the *appendix* (the reasoning). The actionable deliverable — TL;DR, verdict, sized story set,
> experiments — leads on the GitHub spike issue. Read that first; come here for the why.

---

## TL;DR

**Feasible — and the design is already latent in the codebase.** The "atomic durable commit on an object
store" recipe (write immutable data objects, then flip one monotonic manifest pointer with a compare-and-swap
exactly one writer can win) is the *same manifest-LSM pattern datacrystal already ships twice* — `arrow.py`
and `deltalog.py` — with **one substitution**: the local `os.replace()` atomic rename becomes an S3
**conditional `PutObject`** (`If-Match: <etag>`).

**Correctness is the *solved* part**, not the hard part: S3 has been strongly read-after-write consistent for
all operations since **Dec 2020**, and supports conditional writes (`If-None-Match: *` create-once GA Aug
2024; `If-Match: <etag>` CAS) — so the manifest swap is atomic and immediately visible, and a second writer is
fenced by a real primitive, not a workaround. The single-writer case datacrystal already commits to
(invariant 10, Never-list bans multi-writer) is the *easy* case every researched system agonizes over.

**The two hard parts are LATENCY and BOOT-INDEX RELOCATION:**

1. **Latency.** Every commit is ≥1 sequential ~20–100 ms RTT (the manifest CAS-PUT), vs SQLite's ~µs B-tree
   probe / ~4 ms `F_FULLFSYNC` floor. Every cold read is a network hop. This *forces* commit batching
   (`flush_every`, already in arrow/deltalog), off-thread P2 (M2 becomes load-bearing), and aggressive read
   caching (sound, because tid-keyed segments are immutable → cache forever). **This is precisely why ROADMAP
   item 16 gates S3-primary on the retained delta log (item 14/23):** with a durable local log, a commit acks
   on a fast local fsync and ships segments+manifest to S3 *asynchronously* (group-commit), keeping interactive
   latency off the S3 critical path. **`deltalog.py` already shipped — that gate is half-cleared.**

2. **Boot-index relocation.** "The boot index *is* the B-tree" (SQLite) inverts on S3 — there is no B-tree, so
   the manifest must carry an **OID→segment location index**, which at 10M+ records becomes its own
   index-segment LSM with compaction/boot cost. This is ROADMAP item 14, and is exactly why item 16 depends
   on item 14.

**Governance:** item 16 is *Punted*; this grows the **ADR-002-governed storage protocol**, changes the
**invariant-10 single-writer mechanism** (file lock → CAS fence), and touches the **frozen v0.1.0 storage
seam** → a **new ADR (ADR-004) + a Sven scope ruling** are required before any engine code, and it lands
**v0.2+**. `boto3`/an HTTP client stays a strict **`datacrystal[s3]` extra**, lazily imported, never in core
`{msgspec, pyroaring}` (invariant 2).

---

## Verdict

> **FEASIBLE, and the cleanest fit of any *Punted* item** — the protocol seam, the atomic-manifest-LSM
> (shipped twice), the three-phase commit's off-thread P2, and rebuildable-from-`scan_type` indexes all map
> onto S3 with **zero new protocol methods**. Build the **experiments first** (prove the CAS atomicity, the
> zombie-fence, and the crash-recovery on a real bucket + MinIO), ratify **ADR-004**, then the
> `datacrystal[s3]` backend falls out behind the unchanged protocol — **gated on the retained log** so the
> interactive write path never pays the S3 RTT synchronously.

---

## Design shape (the unanimous recipe)

```
datacrystal[s3]  (extra; boto3/HTTP client lazy, never core — invariant 2)
└── ObjectStoreBackend  implements the existing StorageBackend Protocol  (ZERO new methods)
        boot()        → 1 GET of manifest.json → meta + type lineage + live-segment list (no data reads)
        apply(batch)  → P2: PUT immutable tid-keyed record segment(s)   (reuse deltalog's frame format)
                             ── segment durable (PUT acked 200) BEFORE the manifest names it
                                (COMMIT-DELTA-v1 §4.3 "the watermark never lies" — carried over verbatim)
                        then conditional-PUT manifest.json  (If-Match: <prev etag>)  ← THE atomic commit point
                             ── 200 → committed; returned ETag chains the next commit (no HeadObject)
                             ── 412 → a second writer exists = correctness alarm → LeaseLostError
        load_many(oids) → manifest OID→segment index → batched/parallel range-GETs → newest-wins fold
        scan_type(cid)  → GET the cid's segment(s) → stream-decode  (throughput-bound, not latency-bound)
        read_view()     → PIN a manifest version → snapshot isolation FOR FREE (segments never mutate)
```

- **Commit point** = a single conditional manifest PUT. `os.replace` → `PutObject(If-Match)`. Records are
  write-once immutable objects (any key, no conditional header); a crashed commit just orphans them, swept on
  reopen (port `_sweep_orphans`). Genesis manifest uses `If-None-Match: *` so two cold writers can't both
  create version 0.
- **Single-writer = two layers, both CAS:** (a) the `LeaseLock` `used.lock` *file* becomes a `lock` *object*
  advanced by `If-Match` CAS with a monotonic fence/epoch token (Gunnar-Morling-style S3 leader election);
  (b) **the manifest CAS is itself the structural fence** — a writer that paused past its TTL, lost the lease,
  then woke and tried to commit, fails its `If-Match` (412) and **cannot corrupt state**. Invariant 10 is not
  just preserved but *strengthened* (a local file lock can never truly fence a zombie; S3 CAS can).
- **read_view() (ADR-002) becomes trivially superior:** a snapshot is a *value* (a pinned manifest
  etag/version), not a held resource — no WAL pin, nothing to close, no checkpoint-blocking.

### From EclipseStore: ADOPT the architecture, REJECT the S3 connector

| Adopt (the inspiration's good parts)                         | Reject (the trap)                                            |
|--------------------------------------------------------------|-------------------------------------------------------------|
| append-only segments + housekeeping-GC ⇒ our `compact()`     | its `BlobStoreConnector` POSIX-append emulation via numbered |
| lazy-load-by-pointer ⇒ manifest OID→segment map + `Lazy[T]`  | blobs (`file.0`, `file.1`, …): write-amplifying, chatty,     |
| refreshing single-writer lease ⇒ CAS-fenced lease            | fights S3's grain — *visibly breaks in the wild* (their      |
| local read cache (`S3Connector.Caching()`) ⇒ a segment cache | Azure discussion #184: files never merge → slow loads;       |
| dissolve-ratio trigger ⇒ `compact()` heuristic               | corruption on shutdown-ordering). Our immutable-segment +    |
|                                                              | manifest model is strictly better on object storage.        |

---

## What breaks / needs an ADR (ADR-004)

1. **`apply()` shape changes** from random-access (SQLite `INSERT OR REPLACE` / `DELETE` by OID) to
   **append-only LSM** (write immutable segment; fold newest-wins on read; `compact()`). The commit stays
   *one logical atomic step* and the P1/P2/P3 shape is untouched (invariant 4: M2's "P2 off-thread without
   changing the logic" already anticipated this) — but the durability/latency contract differs enough to
   trigger ADR-002's "storage-protocol growth always needs an ADR".
2. **A durable OID→segment location index** lives *in the manifest*. This sits next to invariant 11
   ("indexes are rebuildable derived data, never persisted") — it is **storage metadata, not a rebuildable
   query index**, so it doesn't violate 11, but that distinction wants an explicit ADR sentence.
3. **Single-writer mechanism changes** (file lock → CAS lease + manifest fence): invariant 10 stays, the
   mechanism changes → ADR territory (it's the concurrency contract ADR-001/invariant-10 assume).
4. **Durability semantics:** there is no `fsync`/`F_FULLFSYNC` — "durable" simply *is* "`PutObject` returned
   200". The SQLite durability-triad honesty docstring needs an object-store row.
5. **`read_view()` over a pinned manifest** is a semantic extension of ADR-002 (currently a pinned WAL
   txn / memory copy) → fold into ADR-004.
6. **Conditional-write portability:** `If-Match`/`If-None-Match` are GA on S3/S3-Express/R2/GCS/most MinIO,
   but **not universal** (older MinIO/Ceph, some gateways). The backend must **feature-probe and refuse
   loudly** (format-honesty stance, à la `NewerStoreError`) rather than silently fall back to a non-atomic
   overwrite that corrupts under a race. *Note:* the VISION's "a shared drive" (POSIX) target actually wants
   the **existing `os.replace` path**, not S3 CAS — so the local manifest backend and the S3 backend are
   siblings, not one replacing the other.

---

## Sized story set (the epic this spike unlocks)

*Per the epic convention: these live as a checklist and are cut into their own issue + sprint just-in-time —
not bulk-created. Concerns = Gandalf sizing unit.*

| # | Story | Concerns | Type | Deps |
|---|-------|----------|------|------|
| S1 | **Prove the S3 commit primitives** — MinIO harness + real-S3 smoke: CAS atomicity (two writers race a manifest PUT → exactly one 200, one 412), zombie-writer fence (SIGSTOP past TTL → resumed writer's `If-Match` 412 → `LeaseLostError`), crash-mid-commit (`kill -9` between segment PUT and manifest PUT → orphan swept, watermark unchanged). | 3 | Spike | — |
| S2 | **ADR-004: object-store backend** — storage-protocol growth + CAS-lease mechanism + `read_view` pins a manifest version + durability="PUT 200" + conditional-write feature-probe. | 2 | Spike/decision | S1 |
| S3 | **`ObjectStoreBackend.apply()`** — manifest-LSM over the bucket: PUT tid-keyed record segments (reuse deltalog frame format) + tombstones (ADR-003) + conditional-PUT manifest. Backend-parity in `store_factory` (3rd backend). | 4 | Story | S2 |
| S4 | **Reads** — `boot`/`load_many`/`scan_type` + manifest OID→segment index + immutable-segment cache + `read_view` pins a version. | 4 | Story | S3 |
| S5 | **CAS-fenced lease** — `LeaseLock` S3 variant (`If-None-Match` create / `If-Match` renew + fence token), backstopped by the manifest CAS. | 3 | Story | S3 |
| S6 | **`datacrystal[s3]` packaging + gates** — boto3 strict extra (extend `test_dep_isolation`: not core, not imported at `import datacrystal`), `compact()` + orphan-sweep, request-COUNT fitness gate (PUTs/commit, GETs/read — invariant 12, never wall-clock). | 3 | Story | S3 |

**Gate between S1/S2 and S3+:** no engine code until the experiments pass *and* Sven rules on ADR-004. S3–S6
are the v0.2+ build, and **all of them ride the retained log** (deltalog.py) so commits never pay the S3 RTT
synchronously.

---

## Experiments to run (S1 — the load-bearing proof)

All against **MinIO-in-Docker** (deterministic, CI-able crash matrix) **+ a real-S3 smoke run**. Gates are
**same-run ratios / request counts**, never wall-clock (invariant 12).

**Correctness (prove before any build):**
- **CAS commit-point race** — two writers share a stale manifest ETag, both `PutObject(If-Match)`; assert
  exactly one 200, one 412, never two 200s / a torn manifest. Widen the window with induced latency.
- **Zombie-writer fence** — A holds the lease, SIGSTOP past TTL; B takes over (CAS the lock, bump epoch) and
  commits; resume A → A's manifest `If-Match` 412s → `LeaseLostError`. Proves the fence is at the *commit
  point*, not just the refresher flag.
- **Crash mid-commit** — `kill -9` (a) after segment PUT before manifest PUT, (b) between two segment PUTs;
  reopen → orphan segments swept, watermark = last fully-committed tid, state = exact gapless commit prefix
  (port the existing arrow/deltalog SIGKILL test). COMMIT-DELTA-v1 §4.3 holds on S3.
- **Replay/gapless TID under CAS rejection** — a 412 leaves no manifest, TID stays gapless (invariant 5),
  re-commit reuses the same TID; run the byte-pinned COMMIT-DELTA-v1 replay vectors over the S3 backend →
  identical delta stream to SQLite.
- **Backend-parity** — the full engine suite through `store_factory` with the S3 backend as a 3rd backend;
  memory ≡ sqlite ≡ object-store.
- **Strong-consistency assumption check** — immediately after a manifest CAS returns its ETag, a *second*
  client GETs the same bytes + ETag (validates the no-HeadObject CAS chaining and read-after-write).

**Performance (establish the envelope):**
- **Per-commit latency floor** — PUT(small segment) + CAS-PUT(manifest) p50/p99 vs the ~4 ms `F_FULLFSYNC`
  floor; then with `flush_every` batching → show amortized cost recovers. Gate: is interactive per-`store()`
  commit acceptable, or does it *mandate* coalescing?
- **Boot + index-build cost** — the `benchmarks/_gen.py` scaled mineral cabinet at 1M/10M as per-type
  segments + manifest; boot (GET manifest) + `scan_type` rebuild as a function of segment count + GET
  parallelism. Gate: manifest-carried OID index keeps boot O(manifest), not O(records).
- **Read amplification + cache** — `get(oid)`/`query()` p99 cold vs warm segment cache, `Lazy[T]` residency
  vs full hydration; prove the heap-resident hot path is unaffected by S3 latency.
- **Request-count / cost model** — PUTs/commit + GETs/read over the real generator (priced: PUT ~$0.005/1k,
  GET ~$0.0004/1k); confirm a commit is a *small constant* of PUTs (segments-touched + 1 manifest), not
  O(records). (SlateDB's cited example: a 10 ms batch window turned $65k/mo into $650/mo.)

---

## Risks (carry into ADR-004)

1. **Latency, not correctness, is the project-defining risk** — only viable because the working set is
   root-pinned in RAM (invariant 6) + `Lazy[T]` is the cut point. Honest docs: this backend is for
   blob-store-native / scale-out / archival deployments, **not a faster local store**.
2. **Write API cost is request-count-dominated** — a per-op write pattern is financially ruinous; gate
   PUTs/commit to a small constant; document the `flush_every` durability-vs-cost trade.
3. **412 ≠ retry under single-writer** — a 412 means a *second writer exists* (correctness alarm), not the
   rare 409 concurrent-request retry. The applier must distinguish them, or it masks real lease violations.
4. **`compact()`/housekeeping must be fenced + manifest-mediated** (publish folded segment + new manifest,
   *then* delete old objects) or it races the writer and can delete live data.
5. **Don't copy EclipseStore's blob connector** — its numbered-blob POSIX-append emulation inherits its
   documented failure mode (#184). The immutable-segment + manifest model must win.
6. **Scope discipline** — the CAS fence is a *safety* mechanism (loser refuses, loudly), **not** an enabler of
   concurrent writers. Multi-writer stays on the Never-list; don't drift into distributed-writer territory
   (ADR-001 owner confinement).
7. **Conditional-write portability** — feature-probe + refuse on stores lacking CAS; decline the Lance-style
   external-DynamoDB fallback (it's the multi-writer machinery we deliberately don't build).

---

## How the inspirations do it (the convergence)

Every researched system lands on the **same single-pointer-CAS-flip recipe**, differing only in the flip
primitive and in the multi-writer machinery datacrystal gets to *skip*:

- **EclipseStore** (our inspiration): append-only channel data files + a transaction log as the durable
  index + a housekeeping thread that dissolves gap bytes; lazy-load-by-pointer; a refreshing lock file. Proves
  the *architecture* — but its S3 connector emulates POSIX append via numbered blobs and breaks in the wild.
  We already have the *right* S3-native model in `arrow.py`/`deltalog.py`.
- **Lance / Delta / Iceberg**: immutable data files (fragments/parquet/SSTs) + an atomic manifest/metadata
  pointer flipped by rename-if-not-exists, catalog CAS, or (now) native conditional PUT. Their genuinely hard
  machinery — conflict detection, transaction-file rebase, retry loops, external DynamoDB commit handlers — **is
  exactly the multi-writer problem on our Never-list**, so our backend is dramatically simpler than any of them.
- **SlateDB**: writer fencing via an epoch/token in the manifest; the 10 ms batch window is the canonical
  latency/cost answer. This is the "lease fencing on the manifest" framing ROADMAP item 16 already names.
- **Raw S3**: strong read-after-write (Dec 2020) + conditional writes (2024) make the single-key CAS the only
  atomicity primitive needed — and S3's *only* atomicity is single-key, which is why **all** atomicity routes
  through the **one** manifest pointer.
