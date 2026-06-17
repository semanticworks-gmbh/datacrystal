# How-to: store binary blobs (PDFs, scans, invoices)

Goal: keep large binaries out of your entity records so scans and commits stay fast. The reference
summary of `dc.Blob` (what it does / does NOT do / cost) is in
[Storing binary blobs](../reference.md#storing-binary-blobs); the typing asymmetry is in
[Typing](../reference.md#typing); the full rationale is
[ADR-007](../design/ADR-007-blob-fields.md).

## Pick a tier

A `bytes` field is fine for *small* binaries — but it is stored **inline**, inside the entity's
record, so every hydration, every commit, and every scan over that type drags the bytes along.
For large binaries (PDFs, scanned invoices, images) that is a liability. datacrystal gives you
three tiers; pick by the question you are answering.

| You have… | Use | Where the bytes live | Read as |
|---|---|---|---|
| a *small* binary (≲ a few hundred KB) | a plain `bytes` field | **inline** in the record | `bytes` |
| a *large* binary you reference from one entity | **`Annotated[bytes, dc.Blob]`** | **out-of-line, raw**, in a sibling `blobs` table | a `dc.BlobHandle` (lazy) |
| a binary that is itself a first-class thing you query/dedup | a **`Blob` `@entity`** reached via `dc.Lazy` | its own record (+ a `dc.Blob` field for the bytes) | the entity, on `.get()` |

The rule of thumb: **mark it `dc.Blob` the moment the bytes would bloat a scan of the owning type.**
There is no automatic spill threshold — the data follows your code, explicitly (no raindances).

## dc.Blob — out-of-line raw bytes

```python
@dc.entity
class Invoice:
    number: Annotated[str, dc.Unique]
    pdf: Annotated[bytes, dc.Blob]            # stored out-of-line, raw
    thumbnail: Annotated[bytes | None, dc.Blob] = None

store.store(Invoice(number="2026-0042", pdf=pdf_bytes))
store.commit()
```

The record keeps only a 48-byte descriptor (`blob_oid` + size + sha256); the bytes go to a
sibling `blobs` table in the **same commit transaction** — a SIGKILL leaves the record and its
blob both present or both absent, never torn. A `query`/`count`/`pluck`/scan over `Invoice`
**never touches the blob bytes** (measured: a 5 MB blob → a 62-byte record). A blob is
**immutable**: reassigning the field writes a *new* blob (a new OID); the old bytes are untouched,
which is what makes archival and tear-free concurrent reads natural.

On read the field is a **`dc.BlobHandle`**, not raw `bytes` — `.size` and `.hash` are free (from
the descriptor, no fetch), and you choose **whole** vs **streamed** per access:

```python
inv = store.get(Invoice, number="2026-0042")
inv.pdf.size                       # free — no bytes read
data = inv.pdf.bytes()             # WHOLE: one fetch, cached, idle-demotable (small/medium)

with store.open_blob(inv, "pdf") as f:   # STREAMED: file-like, range-read, RSS-bounded
    header = f.read(8)                   # reads only those 8 bytes off disk
    f.seek(-1024, 2); tail = f.read()    # seek/tell/read(n); never loads the whole PDF
```

`store.open_blob()` returns an `io.BufferedReader` over a private read view, so once opened you
may keep **reading it from another thread** while the owner commits, and a concurrent write can
never tear it. Close it promptly (it pins a read transaction) — it is a context manager. A
snapshot has the fully off-owner twin: `snapshot.open_blob(view, "pdf")`.

## Writing a big blob without holding it whole in RAM

Assigning `bytes` materializes the value once. To write a large blob from a stream — the
invoice/scan-archival shape — assign a **`dc.BlobSource(size, open_chunks)`** instead: the engine
fills a pre-sized cell chunk-by-chunk inside the commit, so the bytes are never whole in memory.

```python
inv.pdf = dc.blob_from_path("/tmp/2026-0042.pdf")   # convenience: a file-backed source
# or, from any sized producer:
inv.pdf = dc.BlobSource(size_in_bytes, lambda: my_chunk_iterator())
store.commit()
inv.pdf.bytes()                                     # after commit it is a readable handle
```

Two rules make it correct: the **size must be known up front** (the cell is pre-allocated), and
`open_chunks` must return a **fresh** iterable each call — the engine reads the source *twice*
(once to hash and length-check *before* the commit's TID is taken, so a wrong size rejects the
commit gaplessly; once to fill the cell). A one-shot iterator, or a size that doesn't match the
bytes, fails loudly and changes nothing. Unknown-length producers buffer to a temp file first and
stream that (`blob_from_path`); a genuinely unbounded stream is `[planned — #76]` (a chunked
layout). One v1 ceiling: a single blob caps at SQLite's ~954 MiB cell limit (it fails loudly,
never truncates).

> **Typing note (honest):** assigning a `dc.BlobSource` to a `bytes`-typed `dc.Blob` field is the
> same kind of write-asymmetry as assigning `bytes` and reading back a `BlobHandle` — a type
> checker sees `bytes` and flags the `BlobSource`. Add a `# type: ignore[assignment]` at that line
> (or a per-file `# pyright: reportArgumentType=false` in code that writes many). The runtime is
> exact; only the static type is approximate, by design. This and the other checker quirks are
> collected in one place — see [Typing](../reference.md#typing).

## When to reach for a Blob entity + dc.Lazy instead

If the binary is a *thing in its own right* — you want to dedup it by content hash, attach metadata,
or share it between several owners — give it its own `@entity` (with a `dc.Blob` field for the
bytes and, say, a `Annotated[str, dc.Unique]` sha256 for dedup) and reference it via `dc.Lazy`.
That keeps the parent record tiny *and* makes the blob queryable. Core stays out of
content-addressing on purpose (it would force refcount/GC); the `Unique`-hash-field pattern is the
supported way to dedup. See [ADR-007](../design/ADR-007-blob-fields.md) for the full rationale.
