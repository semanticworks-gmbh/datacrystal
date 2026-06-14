# ADR-005: Index cache (persist indexes as a rebuildable, never-authoritative cache)

Status: **accepted** (2026-06-14, Sven). **Amends invariant 11.** Storage-side growth → governed
here per the [ADR-002](ADR-002-storage-read-views.md) "storage-protocol growth needs an ADR" rule.
(ROADMAP item 12 / GitHub #12.)

## Context

Indexes — the bitmap equality postings, the unique map, and [ADR-004](ADR-004-sorted-range-index.md)'s
sorted runs — are rebuildable derived data, built **lazily on first query** by scanning every record
of the class. Measured on the **MaStR proving ground** (6.2M real records, eval #4):

- `Store.open()` itself is **~instant — 5 ms** (boot reads only the meta + type lineage, no records);
- the **first query rebuilds the index by scanning all 6.2M records = 35 s**, and the in-RAM index
  maps reach ~3.9 GB.

So *opening* is already instant; the cost is the **first query after a restart**. For an interactive
service (the FastAPI flagship — "ideally startup is instant"), paying 35 s before the first answer on
every process start is unacceptable.

Invariant 11 today reads: *"Indexes are rebuildable derived data — never persisted, never inside the
commit txn."* The **"never persisted"** clause is exactly what forces the cold rebuild.

## Decision

**Persist indexes as an on-disk cache — watermark-stamped, rebuilt on any mismatch, never the source
of truth. Amend invariant 11 accordingly.**

1. **Invariant 11 amended** from *"...never persisted, never inside the commit txn"* to:

   > *Indexes are rebuildable derived data. They **may be cached on disk**, stamped with the commit
   > watermark they reflect; the cache is **never the source of truth** — on any watermark mismatch,
   > format/version change, corruption, or index-config change it is discarded and rebuilt from the
   > records. Indexes are still **never inside the store's commit transaction.***

   The records remain the single source of truth; the cache is a reconstructible accelerator. The
   spirit (rebuildable; records authoritative) is preserved — only "never persisted" relaxes to
   "cache allowed."

2. **A sidecar, outside the commit txn.** The cache lives in its own sidecar beside the store (like
   the `[fts]`/`[arrow]` sidecars), written **outside** the store's commit transaction (invariant
   11's second clause holds). It is a **core** cache (the bitmap/unique/sorted indexes are core), not
   an extra — no new core dependency.

3. **Manifest-LSM substrate (reuse, don't reinvent).** The cache is an LSM of index segments behind
   an atomic fsync-ordered `manifest.json` — the exact pattern `arrow.py` and `deltalog.py` already
   ship (segment fsynced *before* the manifest names it; orphan-sweep on reopen). The manifest stamps
   the watermark (the TID the cache reflects) and the index config (which types, which fields, which
   index kinds).

4. **Watermark validation + catch-up on open.** Compare the cache watermark to the store's:
   - **equal** → load the indexes from the cache (fast startup, no scan);
   - **behind** (the store committed past the cache) → close the gap by replaying the retained delta
     forward (O(delta)) if available, else **discard and rebuild from records** (today's behavior);
   - **ahead / format mismatch / corrupt** → impossible-or-untrusted → discard and rebuild.

   Correctness therefore **never depends on the cache**: a wrong, stale, or corrupt cache is always
   detected and rebuilt from the authoritative records.

5. **Default-on, owner-maintained.** Because the cache is "a must" for this library, it is **on by
   default** (with an opt-out for tiny stores whose rebuild is already instant). Writing/maintaining
   it is owner-confined and lease-held — only the single writer updates it (invariant 10); a reader
   that finds no usable cache simply rebuilds in RAM, unchanged.

6. **Crash safety.** The manifest is the cache's commit point (fsync-ordered, like `deltalog`): a
   crash mid-write leaves an older-but-valid manifest (the cache is merely *behind* → caught up or
   rebuilt next open). The cache can **never corrupt the store** — it is never authoritative.

7. **One mechanism, all index types.** Bitmap postings, the unique map, and ADR-004's sorted runs are
   all cached by the same manifest-LSM — so ADR-004's sorted index gets persistence for free, and a
   *persisted sorted index* (sorted runs + zone-maps + bloom filters) becomes the converged
   Bigtable-shape tier.

## Consequences

- Startup-to-first-query after a restart goes from **O(corpus) rebuild (35 s on 6.2M) to O(cache
  read)** — the interactive-service first query is fast; `Store.open()` was already instant.
- Invariant 11 is amended — a load-bearing contract change (the reason this is an ADR). The
  records-are-authoritative spirit is explicitly preserved, so **no correctness property changes**: a
  stale/wrong/corrupt cache is always detected and rebuilt.
- New sidecar format with its own version + `NewerStoreError`-style honesty (a newer cache format is
  discarded + rebuilt, never mis-read — invariant 9 stance).
- Reuses the shipped manifest-LSM machinery (`arrow.py`/`deltalog.py`) → low new surface area; the
  retained delta log (`deltalog`) is the natural catch-up source for a behind-cache.
- **Ingest-time RAM is unchanged by this ADR** (the in-RAM index is still built during ingest) — the
  ingest-RAM ceiling (#50) is a separate "bulk-load mode" concern; a later follow-on could spill cold
  cached segments to bound that too.
- **Converges with ADR-004**: bloom filters (point-lookup segment-skip) and zone-maps (range-skip)
  for a *persisted sorted* index ride this same substrate — the Bigtable/HBase/Accumulo SSTable model.
- Fitness: a same-run gate that a warm reopen reads the cache (no full scan) and that a
  stale/corrupt cache is detected + rebuilt (correctness independent of the cache).

## Amendment — cardinality-matched representation (2026-06-14, #12 Design A)

The first cut shipped opt-in and measured only **~1.3×** at 6.2M (35.0 s → 26.4 s): `load()`
*reproduced* the rebuild's per-row Python work instead of skipping it. A 13-agent assessment (code
diagnosis + empirical probe + best-practice research + adversarial review, on issue #12) found two
O(corpus) culprits, both now fixed — **the records stay authoritative and every property above is
preserved** (this amends *how* the cache is represented, not the contract):

1. **A pure-Unique field carries no eq postings.** It was stored as one single-element `BitMap64` per
   distinct key (6.2M containers for `mastr_nr`; a 30 B/key serialized floor) **and** the flat
   `unique` value→oid map — redundantly, since `==`/`in_`/`contains`/`startswith` and the natural-key
   `get()` path are all answerable from the map alone. The eq half is now dropped: eq-membership is
   **Index/SortedIndex only** (a `Unique+Index`/`Unique+SortedIndex` field keeps eq); `plan()` answers
   a Unique-only field from the map. Net: ~5× cheaper dump, smaller cache, and the in-RAM single-
   element bitmaps (a chunk of the 3.9 GB at 6.2M) are gone.

2. **The `_last_values` un-index memory is rebuilt lazily, not at open.** `load()` walked every
   posting (`for oid in bm`) to rebuild the per-oid memory — the same O(corpus) loop the rebuild pays.
   It is now deferred to the **first write** after a load (rebuilt from the loaded postings + unique
   map, so additive schema evolution is already baked in — no record re-read, no fill logic). A
   read-only reopen pays nothing. Measured: read-only reopen ~**4.1×** faster than the old design
   (200k high-cardinality-unique probe), approaching the flat-map ceiling.

A true zero-copy/mmap tier is **infeasible** with pyroaring 1.1.0 (no frozen-view), and a C/cffi shim
would breach the `{msgspec, pyroaring}` budget — explicitly out of scope. The lazy key→offset
directory (load-on-open, O(touched keys) for *any* index field) is the separate **ADR-006 / #69**
epic, justified by a future high-cardinality non-unique workload, not the MaStR shape.
