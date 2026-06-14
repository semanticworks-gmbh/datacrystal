# ADR-007 — `dc.Blob` fields: out-of-line raw bytes with whole + streamed access

**Status:** Accepted (2026-06-14). Ratified by Sven via the blob design dialogue (issue #75);
gates all engine code (no implementation before this ADR, per the ADR-002 "storage-protocol
growth needs an ADR" rule). Frozen-API: adds a public `dc.Blob` marker → additive v0.x surface.
(ADR-006 is reserved for the index-cache lazy key→offset directory, #69.)

## Context

A `bytes` field is a first-class msgpack scalar today, stored **inline** in the entity's record.
That is fine for small binaries (≲ a few hundred KB) but a liability for large ones (PDFs, scans,
invoices): every hydration materializes the whole blob, every commit holds it in RAM at least once,
and a `query`/`scan`/index-build over the type drags the bytes through the B-tree it walks.

The ratified `Lazy[Blob-entity]` idiom (SDA-LAYERING) already solves *storage separation* — put the
bytes on their own `@entity`, reference it via `Lazy[Blob]`; the parent record stays tiny (**measured
14 bytes for a 5 MB blob**), and parent-type scans never touch the bytes. So **inline (plain `bytes`)
and lazy-whole (`Lazy[Blob]`) are already covered.**

The one genuine gap is **streaming**: `Lazy.get()` loads the *whole* blob, and the Blob entity's bytes
are **msgpack-framed inside its record payload** — you cannot seek into a frame, so there is no
range/chunked read, and a write needs the whole bytes in RAM. Confirmed real workloads (Sven):
enterprise-search PDFs and SOR invoice store **+ archive**.

The decisive fact (measured): bytes stored **raw** in a dedicated SQLite cell can be range-read via
`sqlite3.Connection.blobopen` — *"read 100 bytes at offset 5 MB of a 10 MB blob → 0.9 ms, never
loaded 10 MB"* — whereas the framed-payload shape must decode the whole frame. **Streaming is a
storage-representation decision, not an API flag.**

## Decision

1. **`Annotated[bytes, dc.Blob]` — a value-type field marker.** A field so marked is **always**
   stored **out-of-line as raw bytes** (no auto-spill threshold — explicit, "data follows your code,
   no raindances"; plain `bytes` stays inline for small values). `dc.Blob` is a **value/leaf**, never
   an `@entity` (invariant 6), never pickled (invariant 1). **No `stream=` flag**: out-of-line-ness is
   the only field-level decision; **whole-vs-streamed is a read-time choice** on the handle.

2. **At rest (SQLite):** a sibling table, NOT a widening of `objects` (so object scans stay fast — the
   TOAST rationale):
   ```sql
   CREATE TABLE blobs(
     oid  INTEGER PRIMARY KEY,   -- the blob's own OID = rowid alias (required for blobopen)
     cid, tid, size, hash, crc,  -- type lineage · immutability · sha256 integrity · torn-blob guard
     data BLOB NOT NULL          -- RAW bytes (zeroblob(size)-allocated for streamed writes)
   );
   ```
   The entity record carries only a **descriptor** — a new msgpack ext `BLOB_EXT(blob_oid, size, hash)`,
   parallel to the OID ref ext (tens of bytes). The descriptor split is **spec-driven** (a
   `FieldSpec.blob` flag in `encode_payload`), NOT an `isinstance(value, bytes)` branch in `swizzle()`
   (a `bytes` value alone cannot be told apart). The `blobs` INSERT rides **the same `apply()`
   transaction** as the record → atomic, SIGKILL-clean, no second write path (invariant 4).
   A blob is **immutable**: changing it mints a new OID (the old cell is never edited), which keeps a
   concurrent streamed read tear-free and makes archival (write-once, content-hashed) natural.

3. **Read — per-access choice (no field flag):**
   - `entity.field` hydrates to a **`Blob` handle** that reuses `Lazy`'s fetch-on-touch +
     `LazyReferenceManager` idle-demotion (it *is* "Lazy for opaque bytes"). `.size`/`.hash` need no
     fetch.
   - `handle.bytes()` → one whole-value fetch (CRC-checked, cached, demotable) — small/medium.
   - `store.open_blob(entity, field) -> BinaryIO` → a file-like `io.BufferedReader` over
     `conn.blobopen('blobs','data', oid, readonly=True)` on a **read_view's pinned connection**
     (ADR-002), callable from any thread; `read(n)`/`seek`/`tell`, context-managed close — big/range.

4. **Write — per-operation choice:** assign `bytes` (whole-write, split out in P1) **or** a streaming
   writer with a **known size** (`INSERT … zeroblob(size)` then incremental `blobopen` fill inside the
   commit txn, never whole in RAM). Owner-thread checked pre-mutation; size/crc/hash computed
   incrementally; writing past `size` fails loudly.

5. **v1 = single raw cell (size-known), ~954 MiB ceiling** (a blob at/over `SQLITE_LIMIT_LENGTH` fails
   loudly, never truncates). Unknown-length producers buffer-to-temp first (documented recipe). A
   **chunked-page layout** (pg_largeobject-style `(blob_oid, seq, data)`) for unbounded producers / >1
   cell is a **backlog story (#76) with a single-cell → chunked MIGRATION PATH** — we will not maintain
   both representations; we migrate when a real unbounded-stream workload appears.

6. **Content-hash dedup is application-layer** (a `Unique` sha256 field on a user `Blob` entity, the
   SDA-LAYERING pattern), not core — core content-addressing would force refcount/mark-sweep GC and a
   delete contract distinct from ADR-003.

7. **S3 (ROADMAP item 16, when greenlit):** the *same* descriptor maps to an object key — whole = GET,
   streamed-read = **Range GET**, streamed-write = multipart PUT (own sha256 as identity, never the S3
   ETag). The SQLite cell and the S3 object are one logical shape, so `open_blob` is backend-agnostic.
   This mapping is folded into the object-store backend's own ADR, not built here.

## Consequences

- **Storage-protocol growth, authorized here:** `CommitBatch` gains a `blobs` list + `StoredBlob`
  (ADR-003-style, no `apply()` signature break); a `blobs` DDL table; `Store.open_blob`; a
  `StorageReadView` blob-open seam. The memory backend stores a `blobs` dict and falls back to
  `io.BytesIO` for streaming (the only behavioral difference between backends — streaming is
  SQLite/S3 only).
- **No new core dep:** `sqlite3` (blobopen/zeroblob, stdlib 3.11+, repo on 3.14), `hashlib`, `io` are
  stdlib; an S3 client stays a `datacrystal[s3]` extra (dep-isolation gate).
- **Schema evolution (honest v1 behaviour):** marking a field `dc.Blob` does **not** mint a new cid —
  the cid splits on the field-NAME shape, which is unchanged — so the decode is **value-driven**: a
  pre-marker payload carries inline bytes and reads back as `bytes`, a post-marker payload carries a
  descriptor and reads back as a `BlobHandle`. No data is lost, but the same field can read either way
  until normalized. `migrate()` does **not** yet rewrite old inline records into blob rows (the cid never
  changes, so its stale-lineage scan finds nothing). **Un-marking** a `dc.Blob` field that still has
  out-of-line data fails loudly on re-commit (the descriptor can't be inlined) — keep the marker or
  migrate. Making the blob flag part of the cid shape signature (so marking/unmarking splits the lineage
  and `migrate()` normalizes) is a follow-on; the re-commit path correctly re-emits a hydrated blob's
  existing descriptor so editing a sibling field never re-stores or wedges the blob.
- **No-pickle holds:** the descriptor decodes to an inert `BlobToken` (RefToken precedent), structurally
  incapable of executing the bytes it addresses. `BlobToken` gets `__eq__`/`__hash__` by `blob_oid` so
  `count()`/`pluck()` match/skip a blob field without fetching bytes.
- **COMMIT-DELTA-v1 (LOCKED) untouched:** the delta carries the opaque payload (now descriptor-bearing);
  **blob bytes are NOT in the delta** — replay reconstructs descriptors, not bytes (an attached
  consumer must fetch bytes itself). Stated so the lock is not mistaken for changed.
- **N+1 on lazy-whole over collections:** each `.bytes()` is one SELECT; a `get_many`-style batch blob
  fetch is a follow-on for collection scans (the SQLAlchemy `deferred` lesson).
- **Build order:** (1) this ADR · (2) `BLOB_EXT` + spec-driven descriptor in the codec (lazy-whole
  only) · (3) `blobs` table + `CommitBatch.blobs` in `apply()`'s txn · (4) lazy-whole `Blob` handle
  `.bytes()`/`.size`/`.hash` + demotion · (5) streamed read `open_blob` over a read_view · (6) streamed
  write (zeroblob + incremental fill, size-known). Each slice is independently shippable; S3 (7) and the
  chunked layout are separate, deferred.
