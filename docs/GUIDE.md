# datacrystal user guide

This guide documents what **exists today** (`0.6.0`, 2026-06). Everything that does not
exist yet is explicitly marked **`[planned — …]`** with its place on the
[roadmap](design/ROADMAP.md); nothing here describes vapor as if it were real. The public API
freezes at the v0.1.0 tag.

- [Open a store](#open-a-store)
- [Define entities](#define-entities)
- [The root](#the-root)
  - [Self-referential adjacency (trees and graphs)](#self-referential-adjacency-trees-and-graphs)
- [Writing: mutate, then commit](#writing-mutate-then-commit)
- [Deleting](#deleting)
- [Lists and dicts inside entities](#lists-and-dicts-inside-entities)
- [What can be persisted](#what-can-be-persisted)
- [Reading: get, query, lazy references](#reading-get-query-lazy-references)
- [Identity and memory](#identity-and-memory)
- [Big data: keeping memory bounded](#big-data-keeping-memory-bounded)
  - [Recipe: parallel ingest (parse in a pool → single writer)](#recipe-parallel-ingest-parse-in-a-pool--single-writer)
- [Schema evolution](#schema-evolution)
- [Frozen entities](#frozen-entities)
- [Concurrency and deployment](#concurrency-and-deployment)
- [Snapshots and the commit-delta pipeline](#snapshots-and-the-commit-delta-pipeline)
- [Full-text search: datacrystal[fts]](#full-text-search-datacrystalfts)
- [Arrow mirrors: datacrystal[arrow]](#arrow-mirrors-datacrystalarrow)
- [Transactional guarantees (A/C/I/D)](#transactional-guarantees-acid)
- [Durability and crash safety](#durability-and-crash-safety)
- [Typing](#typing)
- [Glossary](#glossary)
- [Errors](#errors)
- [Planned features and when they land](#planned-features-and-when-they-land)

## Open a store

```python
import datacrystal as dc

store = dc.Store.open("cabinet.store")        # a directory; created if needed
...
store.close()                                  # or: with dc.Store.open(...) as store:
```

`Store.open(path, *, durability="interval", lock_ttl=10.0, debug=False, strict_deletes=False,
lazy_timeout=None, cache_index=True)` (async: `await dc.aopen(...)`, same keywords **except
`strict_deletes`** — the eager dangling-ref check is sync-only — see
[Concurrency and deployment](#concurrency-and-deployment)):

- The directory holds `data.sqlite` (records as msgpack blobs, riding SQLite's journal),
  `used.lock` (the **single-writer lease**: a second process opening the same store gets a loud
  `StoreLockedError` instead of silent corruption), and `index.cache` (below).
- Secondary indexes are rebuildable derived data, built lazily on first use with a one-time
  O(extent) scan of that class's records (the same is true of the `incoming()` reverse index).
- `cache_index=True` (**on by default**, [ADR-005](design/ADR-005-index-cache.md)) **persists the
  built indexes to a watermark-stamped sidecar and loads them at boot instead of rescanning** — so
  a warm reopen of a large store skips that O(extent) first-query rebuild (measured **~14× faster**
  on a 6.2M-row store; the sidecar is ~2.5× smaller than a naïve one because a `Unique` field is
  stored as a flat key→oid map, not per-key bitmaps). The cache is **never authoritative**
  (invariant 11): any watermark or index-marker mismatch, or a stale/corrupt/newer sidecar,
  silently rebuilds from the records — it can never return a wrong answer (SIGKILL-tested). Pass
  `cache_index=False` for a scratch store, or one you never reopen.
- `durability` is a triad. `"commit"` fsyncs every commit (plus `F_FULLFSYNC` on macOS —
  honest, so a commit costs ~4 ms there); an acked commit survives even power loss.
  `"interval"` (default) group-commits: fsync happens at WAL checkpoints, so a **process**
  crash (kill -9) loses nothing, while an OS crash or power loss may lose the last commits —
  but never corrupts the file. `"never"` skips fsync entirely: benchmarks and scratch stores
  only, an OS crash can corrupt it.
- `close()` **discards uncommitted changes** — commit first. Closing releases the lock and the
  in-memory graph.
- `debug=True` arms the **fingerprint safety net**: every commit re-encodes the live CLEAN
  entities and, for any that changed without the dirty tracking noticing (e.g. an
  `object.__setattr__` bypass, or in-place mutation of a `bytearray`), emits an
  `UntrackedMutationWarning` **and commits the change anyway** — detection plus rescue. It
  costs O(live entities) per commit; use it in development and tests.
- `strict_deletes=True` arms the **eager dangling-ref check** (#110, the ADR-003 dev-time
  bridge): a `commit()` that deletes an entity another record still references **raises**
  `DanglingRefError` at the offending delete, rather than letting the follow fail later — see
  [Deleting](#deleting). (`aopen()` does not take this keyword.)
- `lazy_timeout=<seconds>` enables the **LazyReferenceManager** — see
  [Identity and memory](#identity-and-memory).

## Define entities

An entity is a typed Python class; the decorator turns it into a slots dataclass and registers
it with the engine:

```python
from dataclasses import field
from typing import Annotated
import datacrystal as dc

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]                 # unique secondary key → store.get()
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None   # bitmap-indexed → store.query()
    mohs: float | None = None
    type_locality: dc.Lazy["Locality"] | None = None         # lazy reference
    tags: Annotated[list[str], dc.Index] = field(default_factory=list)  # multi-valued index
                                                             # → query(Mineral.tags.contains("x"))
```

- `@dc.entity` applies `@dataclass(slots=True, weakref_slot=True, eq=False)`. Entity equality
  is **identity** — there is exactly one live instance per stored object.
- Field markers go inside `typing.Annotated`:
  - `dc.Index` — adds the field to the roaring-bitmap indexes; `==` and `.in_()` queries on it
    answer from bitmaps. Index/Unique fields must be scalar (`str | int | float | bool`,
    optionally `| None`; `datetime`/`date` are supported as `dc.SortedIndex` keys — see the
    `dc.SortedIndex` notes below) — or a **`list` of scalars** for a multi-valued (inverted) index
    (`Annotated[list[str], dc.Index]`), queried with `.contains(elem)` for exact element
    membership. A bad index type is rejected at `@dc.entity` definition, not first `commit()`.
  - `dc.Unique` — unique secondary key (e.g. URIs, slugs, external ids). Duplicates are
    rejected at commit (`UniqueViolationError`); `None` never collides (SQL-NULL-style). A
    `Unique` field cannot be a list (a multi-valued field has no single key).
  - `dc.FullText` — declares a prose field for full-text search, optionally with its
    language: `Annotated[str, dc.FullText(language="de")]` (lowercase short codes; bare
    `dc.FullText` = fold-only exact matching, no stemming). **Inert in the core engine** —
    indexing, stemming and ranked search are `datacrystal[fts]`'s job; see
    [Full-text search](#full-text-search-datacrystalfts).
- `@dc.entity(frozen=True)` declares an append-only record — see [Frozen entities](#frozen-entities).
- Entity classes are identified by `module:qualname` in the store. Keep an entity class
  importable under the same module path, or opening old data raises `UnregisteredTypeError`.

## The root

`store.root` is the entry point of the graph — **any persistable value**: an entity, a list, a
dict (including an empty one), even a scalar. It is `None` until you assign it; that is the
first-run check:

```python
store = dc.Store.open("cabinet.store")
if store.root is None:          # first run only
    store.root = {"minerals": [], "runs": 0}
store.root["minerals"].append(Mineral(qid="Q43010", name="quartz"))
store.commit()
```

- Everything reachable from the root is **pinned in memory and identity-stable**:
  `store.root is store.root` always holds, no strong reference of your own required. (Versions
  before 2026-06-11 had a bug here — the root graph could be garbage-collected and silently
  rehydrated. Fixed; you do not need to hold your own reference anymore.)
- `dc.Lazy[T]` is the explicit cut point where pinning (and loading, and memory) stops — use it
  for the parts of the graph that should not live in RAM permanently. A **collection** of cut
  points is `list[dc.Lazy[T]]` / `dict[K, dc.Lazy[T]]` — each element reloads as its own unloaded
  handle, so a graph node can hold many edges that hydrate **on demand**, one `.get()` at a time
  (the model for adjacency / edge lists; a plain `list[T]` reloads its elements *eagerly*, so
  laziness follows the declared element type, not what you put in at write time).
  **Self-reference is supported — this is how you model trees and graphs** (a node whose `T` is its
  own type); see [Self-referential adjacency](#self-referential-adjacency-trees-and-graphs) below.
- Assigning `store.root = value` captures the value immediately: new entities in it are
  registered, and lists/dicts come back from `store.root` as tracked containers.

**"Multiple roots?"** There is exactly one root by design — but it can be a dict, which *is*
the named-roots pattern:

```python
store.root = {"minerals": [], "settings": {}, "log": []}     # plain dict as named roots
```

An entrypoint entity works just as well and gives you attribute access and types:

```python
@dc.entity
class Cabinet:
    minerals: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)

store.root = Cabinet()
```

Both are equivalent to the engine; pick by taste. And note that entities do **not** have to
hang off the root at all — see the next-but-one section.

### Self-referential adjacency (trees and graphs)

The flagship object-graph shape is a **self-referential** entity: a node whose lazy edges point at
its own type. A `list[dc.Lazy["Node"]]` of children plus a lazy `parent` backlink is exactly how
you model a tree, a DAG, or an adjacency list — each edge stays off the RAM/read budget and
hydrates one `.get()` at a time. Spell the self-reference as a **forward-ref string** under
`from __future__ import annotations` (the entity's own name isn't bound yet while the class body
runs; the string resolves lazily):

```python
from __future__ import annotations
from dataclasses import field
from typing import Annotated
import datacrystal as dc

@dc.entity
class Region:                                       # a geographic containment tree
    qid: Annotated[str, dc.Unique]
    name: str
    children: list[dc.Lazy["Region"]] = field(default_factory=list)   # self-referential edges
    parent: dc.Lazy["Region"] | None = None                           # lazy backlink

# write: continent -> country -> two regions
africa = Region(qid="R-AF", name="Africa")
namibia = Region(qid="R-NA", name="Namibia")
erongo = Region(qid="R-ER", name="Erongo")
namibia.parent = dc.Lazy.of(africa)
africa.children = [dc.Lazy.of(namibia)]
erongo.parent = dc.Lazy.of(namibia)
namibia.children = [dc.Lazy.of(erongo)]
store.root = africa
store.commit()
```

After a reopen the tree is **cold** — children rehydrate as *unloaded* `dc.Lazy` handles, and you
traverse on demand, identity preserved:

```python
root = store.root                          # Region "Africa", nothing below it loaded
na = root.children[0]                       # an unloaded handle: na.loaded is False, na.oid is set
namibia = na.get()                          # loads just this node (siblings untouched)
er = namibia.children[0].get()
assert er.parent.get() is namibia           # the parent backlink resolves to the SAME instance
assert er.parent.get().parent.get() is root # ...all the way up — one live object per OID
```

The parent↔child cycle round-trips with no `RecursionError`, and identity is stable: every path to
a node yields the same Python object (the registry contract — [Identity and memory](#identity-and-memory)).
This is pinned by `tests/unit/test_selfref_adjacency.py` over both backends.

## Writing: mutate, then commit

datacrystal buffers until commit — there is no session object, no `save()`, no dirty flag to
set:

```python
quartz = store.get(Mineral, qid="Q43010")
quartz.crystal_system = "trigonal"     # attribute write → tracked
quartz.tags.append("display-case")     # in-place container mutation → tracked
tid = store.commit()                   # atomically persists everything buffered
```

- `store.commit()` returns the new commit id (`int`), or `None` if nothing changed. A commit is
  atomic: after a crash you see an exact prefix of your commits, never a torn one.
- New entities are discovered automatically through reachability: assign them into the graph
  (or `store.root`) and commit. For a graph **not** reachable from the root, register its top
  explicitly with `store.store(obj)`.
- `store.mark_dirty(obj)` exists as an escape hatch but is rarely needed — attribute writes and
  in-place list/dict mutation are both tracked.
- `store.last_tid` is the current commit watermark.

### Upserting by natural key

`store.upsert(obj)` inserts, or merges into the entity that already owns the same unique key —
the shape of every sync-against-a-source loop:

```python
for row in feed:                                   # initial import AND refresh
    store.upsert(Mineral(qid=row["qid"], name=row["name"], mohs=row["mohs"]))
store.commit()
```

- On a match the **existing live instance survives** (identity is never broken) and every
  field is overwritten with the new object's values — but only fields that actually changed
  are written. Re-importing an unchanged dataset buffers nothing: the refresh commit is
  O(changed rows), not O(rows).
- `upsert(obj, key="qid")` picks the natural key explicitly; with exactly one `dc.Unique`
  field on the class, `key=` is optional. The return value is the canonical instance — use
  it, not your argument, after the call.
- One batch may upsert the same key many times (later calls merge into the first). Duplicates
  created via plain `store()` are *not* matched and keep their loud `UniqueViolationError`
  at commit.

## Deleting

`store.delete()` buffers like every other write and executes at `commit()`
([ADR-003](design/ADR-003-delete-semantics.md)). The record's row is physically removed,
the indexes and the unique map forget it, and attached delta consumers receive a tombstone:

```python
store.delete(quartz)                       # by live instance
store.delete(Mineral, qid="Q43010")        # by unique key — no hydration needed
store.commit()
```

- **Idempotent**: deleting an unknown key or an already-deleted entity returns `False`,
  never raises. Deleting a NEW (never-committed) entity just cancels its pending insert.
- A live instance you still hold becomes a **detached plain object**: reads keep working,
  writes (and `store()`/`mark_dirty()`) raise `DeletedEntityError`. Create a new entity
  instead — OIDs are never reused.
- A buffered delete **wins** over a buffered write to the same object in the same commit.
- A unique-key value freed by a delete is reusable in the same commit (the
  sync-against-a-changing-source pattern: delete the withdrawn record, insert its successor).
- **Deletes are unchecked in v0.x.** Nothing stops you deleting an entity other records
  still reference; *following* such a stale reference (eager hydration, `Lazy.get()`,
  `get_many`, snapshot `get`) raises `DanglingRefError` — loudly, never a silent `None`.
  Checked deletes (refuse-if-referenced, cascades) arrive with the v1 reverse-reference
  index. **Dev-time bridge until then:** `Store.open(strict_deletes=True)` **raises**
  `DanglingRefError` at the offending `commit()`, naming the referrers — turning a deferred,
  spooky failure into an at-the-delete one; `Store.open(debug=True)` runs the same check but
  only **warns** (`DanglingDeleteWarning`) and commits anyway, so a bulk re-import isn't
  bricked. The check runs in P1 before the TID is allocated (a rejected strict commit stays
  gapless and retryable); unarmed — the default — pays nothing. It is a *diagnostic*, not
  referential integrity. If the *root graph* ends up referencing a deleted entity, reading `store.root`
  raises after a reopen — assigning `store.root` replaces the root and recovers the store.
- Disk space: the SQLite pages are freed for reuse immediately; the file itself shrinks
  only on `VACUUM` (run it offline if you need the bytes back).

## Lists and dicts inside entities

Every `list`/`dict` that enters an entity field (or the root) is wrapped in an owner-bound
`dc.PersistentList`/`dc.PersistentDict`. They behave like normal lists/dicts, plus:

- any mutation (`append`, `__setitem__`, `update`, `sort`, …) marks the owning entity dirty;
- mutating a container that belongs to a frozen entity raises `FrozenEntityError`;
- a container keeps its owning entity alive — holding just `e.tags` is enough to commit through it.

One semantic to know: **assignment copies.** Containers are by-value parts of their owner —
after `e.tags = data`, mutate through `e.tags`; later changes to the original `data` object do
not reach the entity. (They also round-trip by value: two entities sharing one list become two
lists after reopen.)

## What can be persisted

| Value | Persisted as |
|---|---|
| `None`, `bool`, `int`, `float`, `str`, `bytes` | itself (msgpack) |
| timezone-aware `datetime` | itself (msgpack timestamp; the instant is preserved, a non-UTC offset comes back as UTC) |
| naive `datetime`, `date`, `time` | itself (datacrystal extension codes, format v2) |
| `timedelta` | **as an ISO-8601 duration string** — it comes back as `str`; store seconds, or convert at the edge |
| `list`, `dict` | by value; nested fine; dict keys must be scalars |
| `tuple` | **as a list** — it comes back as a list (msgpack has no tuple type) |
| `@dc.entity` instance | by reference (8-byte OID), eagerly loaded on access |
| `dc.Lazy[T]` | by reference, loaded on first `.get()` |
| plain (non-entity) dataclass | **rejected loudly** — it would round-trip as a dict; make it an entity |
| `set` / `frozenset` | **rejected loudly** — use a list (v0.1) |

There is no pickle anywhere: records are msgspec msgpack, and decoding is structurally
incapable of executing code.

## Storing binary blobs (PDFs, scans, invoices)

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

### `dc.Blob` — out-of-line raw bytes

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

### Writing a big blob without holding it whole in RAM

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
> collected in one place — see [Typing](#typing).

### When to reach for a `Blob` entity + `dc.Lazy` instead

If the binary is a *thing in its own right* — you want to dedup it by content hash, attach metadata,
or share it between several owners — give it its own `@entity` (with a `dc.Blob` field for the
bytes and, say, a `Annotated[str, dc.Unique]` sha256 for dedup) and reference it via `dc.Lazy`.
That keeps the parent record tiny *and* makes the blob queryable. Core stays out of
content-addressing on purpose (it would force refcount/GC); the `Unique`-hash-field pattern is the
supported way to dedup. See [ADR-007](design/ADR-007-blob-fields.md) for the full rationale.

## Reading: get, query, lazy references

```python
# unique-key lookup (exactly one Unique field as keyword)
azurite = store.get(Mineral, qid="Q193563")          # entity or None

# bitmap-indexed queries with a composable condition AST
hits = store.query(
    (Mineral.crystal_system == "monoclinic") & (Mineral.mohs >= 3.0)
)

# batch hydration — N+1 is never your problem
minerals = store.get_many(oids_or_lazies_or_entities)   # one storage round-trip
found = store.get_many(Mineral, qid=["Q43010", "Q193563"])  # bulk unique-key lookup,
                                                            # aligned, None per miss

# counting and column reads — no entities constructed (decode-level)
n = store.count(Mineral)                                  # extent cardinality
n = store.count(Mineral.crystal_system == "monoclinic")   # bitmap cardinality
names = store.pluck(Mineral, "name")                      # one column, whole class
rows = store.pluck(Mineral.mohs >= 7.0, "name", "mohs")   # tuples; refs come back
                                                          # as dc.Ref for get_many()

# multi-valued (list) index — exact element membership, answered from a bitmap
glowing = store.query(Mineral.tags.contains("fluorescent"))   # no record reads

# limit / offset — window a large result set (deterministic, ascending OID)
top10 = store.query(Mineral.crystal_system == "cubic", limit=10)  # loads 10, not the extent
page2 = store.query(Mineral, limit=50, offset=50)
heads = store.pluck(Mineral, "name", limit=100)               # windows the decode-level read too

# order_by — sort the whole match set, then window (NULLs last, ascending-OID tiebreak)
hardest = store.query(Mineral, order_by=(Mineral.mohs, "desc"), limit=10)  # SortedIndex → cheap
by_name = store.pluck(Mineral, "name", order_by=Mineral.name)  # bare field = ascending
page = store.query(Mineral, order_by=(Mineral.mohs, "asc"), limit=50, offset=50)  # stable paging

# stream the whole match set in bounded memory — chunk by chunk, never all at once
for m in store.query_iter(Mineral.crystal_system == "cubic"):       # O(chunk) live, not O(extent)
    process(m)

# lazy references
ref = azurite.type_locality      # dc.Lazy[Locality]
ref.loaded                       # False — nothing fetched yet
ref.get()                        # loads now (and caches): the Locality
ref.peek()                       # the target if loaded, else None — never loads
```

Query semantics:

- Operators on class-level fields: `==`, `!=`, `<`, `<=`, `>`, `>=`, `.in_([...])`,
  `.contains("sub")`, `.startswith("pre")`; combine with `&`, `|`, `~`. **Parenthesize
  predicates** — `&` binds tighter than `==` (you get a helpful `QueryError` if you forget).
- Every read API takes an entity class **or** a Condition (symmetry, 2026-06-12):
  `query(Mineral)` hydrates the **full extent** — the expensive shape, same cost as any
  non-indexed predicate; prefer `count()`/`pluck()` when you don't need live entities.
- `query()`/`pluck()` (and `snap.query()`/`snap.all()`) take `limit=`/`offset=` to window the
  result. On a fully-indexed read the slice hits the candidate OIDs **before** hydration —
  `query(C, limit=10)` loads 10 records, not the extent. A residual predicate must
  decode-to-filter first, so there the window only trims the materialized result (it cannot
  prune the scan). Order is deterministic (ascending OID): `query(C, limit=k) == query(C)[:k]`.
- `query()`/`pluck()` (and `snap.query()`/`snap.all()`) take `order_by=(field, "asc"|"desc")`
  (a bare `field` means ascending; `field` is `EntityClass.f`, `dc.fields(C).f`, or a name str).
  It sorts the **whole** match set before the window — **NULLs sort last**, and ties break on
  **ascending OID** so `limit`/`offset` paging is deterministic (`page1 + page2 == query(...)[:n]`).
  An indexed sort field is ordered **straight from the index** — a `dc.SortedIndex` field
  (below) is the cheap path; **an un-indexed sort field must decode that field for every match
  first**, the same O(matches) scan a non-indexed predicate already pays, so it cannot beat the
  full scan. Mark a field `dc.SortedIndex` if you page by it often. (Multi-valued/list fields
  can't be a sort key.)
- **`store.query_iter(target)`** streams matching entities **chunk by chunk** for bounded memory —
  walk millions of matches without materializing the whole list (`query()`'s eager
  complement; `count()`/`pluck()` stay the decode-level options). It reads committed state at
  iteration time and re-checks the owner thread on every pull, so a foreign thread or a closed
  store stops it mid-stream.
- **`store.explain(target)`** (also on snapshots) returns the deterministic `QueryPlan`:
  which part answers from bitmaps, what evaluates as Python residual, and over how many
  candidates — `query()` hydrates at most `plan.candidates`. There are exactly **two
  planning rules and never an optimizer** (`==`/`.in_()` on indexed fields → bitmaps; the
  rest → residual); when a question needs a real query planner, hand `mirror.table(...)`
  to DuckDB — that tier owns clever.
- `==` and `.in_()` on `dc.Index` fields answer from roaring bitmaps. `.contains()` /
  `.startswith()` on an indexed field iterate the index's **distinct values** and OR the
  matching bitmaps — O(distinct values), never a record read; they are exact and
  case-sensitive (linguistic matching is `datacrystal[fts]`'s job — see
  [Full-text search](#full-text-search-datacrystalfts)). On a **multi-valued** (`list`) index
  field, `.contains(elem)` is exact *element membership* — an O(1) posting lookup, no record
  reads. All other predicates run as a Python residual over the bitmap candidates. Ordering
  comparisons never match `None`, and string matching never matches a non-string value.
- **`dc.SortedIndex`** makes a scalar field answer **range** queries — `>=`, `>`, `<=`, `<`,
  and `between` (write it as `(F.x >= lo) & (F.x <= hi)`) — from a sorted index instead of a
  full-extent scan. Mark the field `Annotated[float, dc.SortedIndex]` (or `int`/`str`,
  optionally `| None`); the application chooses which fields need ranges, just like `dc.Index`.
  A `SortedIndex` field also answers `==`/`.in_()` (it is an index), so it needn't also be
  `dc.Index`. This is a *third* deterministic planning rule, not an optimizer — `explain()`
  still shows exactly what answers from the index and what falls to a residual. On real data a
  range query drops from an O(extent) scan to a sorted slice (measured: a 6.2M-row
  "capacity ≥ 1 MW" went from ~20 s to ~85 ms).
  `datetime`/`date` are valid `dc.SortedIndex` key types (a timestamp field then answers
  range, `order_by`, **and** `==`/`.in_()` from the one index). Store timestamps
  **timezone-aware** (`datetime.now(timezone.utc)`); aware values order by their UTC instant,
  and `None` sorts last as usual. Mixing naive and aware values on one temporal index raises
  `dc.MixedTemporalIndexError` at `commit()` — before the TID is allocated, so the commit
  sequence stays gapless — instead of a confusing comparison failure; naive-only and
  aware-only fields both work.
- A condition uses fields of **one entity class** — cross-entity joins are
  `[planned — v1, on Arrow mirrors]`.
- `query()` and `get()` reflect **committed** state; uncommitted buffered changes are not
  visible to them. `count()` and `pluck()` read committed state even more strictly: they
  decode records instead of hydrating entities, so they never see in-memory mutations at
  all (and never pay entity-construction RAM — that is the point).
- Querying, counting or plucking a class the store has **no committed records of** returns
  empty and emits an `UnseenTypeWarning` — legitimate on a first run, a lifesaver when you
  forgot to `commit()` or opened the wrong store file. `get()` stays silent (`None` is the
  expected miss in get-or-create code).
- Type checkers cannot model the magic class-attribute access (they see `Mineral.mohs` as
  `float | None` and flag the comparison). Runtime is fine either way; for checker-clean code
  use the equivalent typed proxy:

  ```python
  M = dc.fields(Mineral)
  hits = store.query((M.crystal_system == "cubic") & (M.mohs >= 6.0))
  ```

  This is the first of the type-checker quirks; the full set lives in one place — see
  [Typing](#typing).

### Recipe: paging the newest/top-N by an indexed field

For "the N newest" (or hardest, heaviest, …) by an indexed field, reach for a bare
`order_by` + `limit` — **not** a `>= sentinel` predicate. Both are correct and return the same
rows; one stays flat as the dataset grows and the other does not.

```python
@dc.entity
class CatalogEvent:
    seq: Annotated[int, dc.Unique]
    at: Annotated[datetime | None, dc.SortedIndex] = None   # None = unrecorded time

F = dc.fields(CatalogEvent)

# FAST — streams the newest 100 straight off the SortedIndex head, O(limit).
newest = store.query(CatalogEvent, order_by=(F.at, "desc"), limit=100)

# SLOWER — same answer, but considers every matching candidate before windowing.
sentinel = datetime(1970, 1, 1, tzinfo=timezone.utc)
newest = store.query(F.at >= sentinel, order_by=(F.at, "desc"), limit=100)
```

Why the bare-`order_by` form wins:

- It orders **straight from the `SortedIndex` run** and windows **lazily** — it materializes only
  about `offset + limit` rows, so its cost is O(limit), flat no matter how big the class grows.
- Because **NULLs sort last** (in *both* directions), rows with `at=None` (unrecorded /
  unpublished) land in the tail and are skipped for free once the window fills — you don't need a
  predicate to exclude them.
- The `>= sentinel` form narrows the **candidate set** to every match first (it then windows the
  *same* lazy way), so its cost grows with the number of matches. It is **not** slower because it
  "scans records" or sorts everything — both forms stream from the index with **no** Python
  residual; the difference is purely the candidate-set *size*. (A `>= sentinel` predicate also
  drops `None`, since ordering comparisons never match `None` — so the two forms agree on which
  rows they return.)

`explain()` shows the tell — the `candidates: K of extent` line:

```python
store.explain(CatalogEvent)
# CatalogEvent: full extent — query() hydrates all <extent> entities ...
#   (the order_by form windows this lazily to ~offset+limit; it does NOT hydrate the extent)

store.explain(F.at >= sentinel)
# CatalogEvent: (CatalogEvent.at >= ...)
#   candidates via bitmaps: <matches> of <extent>     # grows with the match count
```

This works directly on a `datetime` `SortedIndex` field (a timestamp answers range, `order_by`,
**and** `==`/`.in_()` from the one index) — the newest-by-timestamp shape this recipe is for. It is
the rule-based planner's **third** deterministic rule (the `SortedIndex` range slice), not an
optimizer: `explain()` always shows exactly what answers from the index and what (if anything)
falls to a residual.

### Backlinks: who references this? — `incoming()`

`store.incoming(entity)` returns every committed entity that **references** `entity` — the
inverse of following a ref. Backlinks power impact analysis, orphan detection, and
digital-twin / system-of-record traversal ("which records point at this one?").

```python
quartz = store.get(Mineral, qid="Q43010")
for referrer in store.incoming(quartz):     # every entity that points at quartz —
    print(referrer)                         # eager AND Lazy refs, in scalar fields
                                            # and inside list/dict containers
```

- Answered from a **rebuildable in-memory reverse-reference index** (never persisted,
  invariant 11): the first `incoming()` scans the store once to build it (one-time
  O(extent), like the lazy forward indexes), then it is maintained incrementally at every
  commit — a second, unrelated backlink is an O(1) posting lookup.
- An unwatched store pays **nothing**: the reverse index is built only on first use, so if
  you never call `incoming()` your commits are byte-identical and free of its upkeep.
- A deleted **target** keeps its postings, so `incoming(dead)` enumerates the entities now
  **dangling** at the dead OID (OIDs are never reused) — exactly the referrers a checked
  delete would act on ([ADR-003](design/ADR-003-delete-semantics.md)). A deleted
  **referrer** drops out. Checked delete itself (refuse-if-referenced, cascades) is
  `[planned — v1]`.
- The same backlinks at a pinned watermark are `snap.incoming(...)` — see
  [Snapshots](#snapshots-and-the-commit-delta-pipeline).

## Identity and memory

- One live instance per stored object: every path to an entity yields the same Python object,
  cycles included (`a.peer.peer is a`). Identity holds for as long as the object is alive.
- The root-reachable graph is pinned (see [The root](#the-root)). Entities **not** reachable
  from the root (query results, lazily loaded subgraphs) are collectable as soon as you drop
  them — and rehydrate transparently on next access.
- Rule of thumb for big datasets: structure the hot path as eager references and put `dc.Lazy`
  on the cold edges; the eager part is your RAM budget (~600 B/object envelope).

## Big data: keeping memory bounded

A dataset larger than RAM works today **if you keep it off the pinned root**. Three patterns,
in order of leverage:

1. **Free-floating entities.** Entities do not need to be reachable from the root: register
   them with `store.store(obj)`, commit, drop your references — they are collected, and come
   back on demand via `store.get()` (unique key) or `store.query()`. Memory is bounded by what
   you currently hold plus your query results, not by the dataset.

   ```python
   for batch in read_csv_in_batches(path):           # streaming ingest
       for row in batch:
           store.store(Specimen(**row))
       store.commit()                                # batch is collectable after this
   ```

2. **`dc.Lazy` cut points.** Anything eagerly reachable from the root is pinned; a `dc.Lazy`
   edge stops both the pinning and the loading. Keep the root small (ids, settings, hot
   objects) and reach the bulk through unique keys, queries, or lazy edges.

3. **Index-friendly queries — and the decode-level reads.** `==`/`.in_()` on `dc.Index`
   fields answer from bitmaps and hydrate only the hits — and so do `.contains()`/
   `.startswith()` on them (they walk the index's distinct values, not the records);
   `count()` on any of these is pure bitmap cardinality (zero loads). For "how many?" and
   column reads use `count()`/`pluck()` — they decode records without constructing
   entities, so even their full-scan residual form costs decode time, not an entity-RAM
   spike. The expensive shape that remains is a residual predicate in `query()`
   (`>=`, `!=`, …): that **hydrates the whole extent** — on a million-object class a full
   table scan with a matching RAM peak. Design hot filters as `dc.Index` equality facets;
   real columnar speed is the `datacrystal[arrow]` mirror's job — see
   [Arrow mirrors](#arrow-mirrors-datacrystalarrow).

4. **Stream or window when you do need the entities.** The expensive shape above —
   materializing a whole match set as live objects — has two bounded answers.
   `store.query_iter(target)` yields the matches **chunk by chunk**, so peak RAM is O(chunk) not
   O(extent) (CI-gated); it reads committed state at iteration time and stops on a foreign
   thread or a closed store. For just the first page, `query()`/`pluck()` take
   `limit=`/`offset=` — a fully-indexed read loads only the slice.

   ```python
   for specimen in store.query_iter(Specimen.quality == "museum"):   # millions of matches,
       export(specimen)                                        # O(chunk) RAM throughout
   first_page = store.query(Specimen, limit=100)               # hydrates 100, not the extent
   ```

Measured (M2 dev machine, SQLite backend, 300k objects ≈ 29 MB on disk): streaming ingest
3.4 s peaking at ~750 B/object RSS with **zero** entities left live; warm bitmap query
hydrating 75k hits 0.34 s; full-scan residual query +165 MB transient; unique-key `get()`
0.1 ms hydrating exactly one object. CI now gates these properties (peak-RSS byte budget,
results collectable, `get()` hydrates one — `tests/fitness/test_memory_bounded.py`).

Loaded `Lazy` references stay loaded until you drop the holder — **or** you open the store
with `lazy_timeout=<seconds>`: the LazyReferenceManager then demotes handles idle past the
timeout back to unloaded, releasing the subgraph behind the cut point (the next `.get()`
transparently reloads, identity preserved). Demotion only ever runs on the owner — as a
piggyback on your own store calls (sync), or as an owner-loop task (`aopen`). Timeout-only in
v0.1; an RSS-quota variant is deferred (psutil stays out of core), and a hard per-store memory
cap does not exist. For analytics-style scans, the honest answer is the
[Arrow mirror tier](#arrow-mirrors-datacrystalarrow), not the object graph.

### Recipe: parallel ingest (parse in a pool → single writer)

Bulk-importing many files (the classic shape: dozens of XML/CSV files into millions of records)?
The right way under owner confinement ([ADR-001](design/ADR-001-concurrency-contract.md)) is to
**parse in a process pool, but build and write only in the owner process**. It needs no new
API — just `store.store()` / `store.commit()` and stdlib `multiprocessing`:

```python
import multiprocessing as mp
import datacrystal as dc

def parse_file(path: str) -> list[dict]:   # WORKER: returns plain data — never @entity instances
    return list(parse(path))               # dicts/tuples only

if __name__ == "__main__":
    with dc.Store.open("cabinet.store") as store:
        if store.root is None:
            store.root = {}
        with mp.Pool() as pool:
            for recs in pool.imap_unordered(parse_file, files):   # parse fans out across cores
                for d in recs:
                    store.store(Mineral(**d))                     # build + write: OWNER ONLY
                store.commit()                                    # one commit per file
```

**The one rule to hold:** workers return **plain data** (dicts/tuples); `@entity` instances are
constructed **only in the owner process**. Why this matters, honestly — across a *process* boundary
an `@entity` does **not** raise a loud error. An `@entity` is an ordinary `slots=True` dataclass
(datacrystal defines no custom pickle hook), so it pickles cleanly via the stdlib default and
arrives in the owner as an un-stamped detached copy that `store.store()` re-registers with a
**fresh OID** — building entities in workers silently double-serializes and breaks identity-by-OID
rather than failing fast. (The loud guards cover *thread* mistakes, not this one: `EntityEscapeError`
is the cross-**thread** `submit()`-result guard; `WrongThreadError` fires on foreign-thread access;
`StoreLockedError` refuses a second writer **process**.)

**Set the performance expectation correctly:** parallelism speeds the **parse**, not the **write**
— the single writer is the serial wall (`store()` + `commit()` run on the owner). So reach for this
only when parsing is a real fraction of import time. Illustrative ratios from one app's
parallel-ingest probe (8 files, 800k records, 8 cores) — **not** a datacrystal benchmark, shown only
to set the shape of the trade-off:

```
parse only:   7.1s -> 1.9s   (~3.8x)    # the parse parallelizes
full import: 22.0s -> 17.7s  (~1.2x)    # the write stays serial — the wall
```

For more throughput, tune the **write** side instead: `durability="never"` for a one-shot bulk load
(see [Open a store](#open-a-store)), `@dc.entity(frozen=True)` records (cheapest to commit —
[Frozen entities](#frozen-entities)), fewer `dc.Index`/`dc.Unique` fields, and bigger commit
batches. The scale model stays **1 writer + N readers**; write-sharding is out of core —
[SCALING.md](design/SCALING.md).

Deliberately no `store.writer()` / `bulk_load()` / `parallel_map()` helper: a batching write-sink
would reintroduce the session object the design rejects ("no session, no `save()` — mutate, then
`commit()`"), and a process-pool wrapper is not a database concern. The primitives already suffice;
this is a discoverability recipe, not a missing feature.

## Schema evolution

You can evolve entity classes between runs; old records adapt **on load**:

| Change | What happens |
|---|---|
| add a field **with a default** | old records get the default when loaded ✔ |
| remove a field | old values are ignored ✔ |
| reorder fields | values map by name ✔ |
| add a field **without a default** | `SchemaMismatchError` naming the field — add a default |
| add a `dc.Unique` field | must default to `None`, else `SchemaMismatchError` (a shared non-None default would collide) |
| rename a field | mark the new field `Annotated[T, dc.RenamedFrom("old")]` — the old values follow ✔ (see below) |
| split / merge / derive a field | mark the new field `Annotated[T, dc.Glue(fn)]` — `fn(old_record)` computes it from the old record ✔ (see below) |
| change a field's type | not checked (annotations are not validated on load) — avoid, or use `dc.Glue` to convert on load |

To rename a field without losing data, mark the new field with its old persisted name:
`mohs: Annotated[float | None, dc.RenamedFrom("hardness")]`. On load, a record that lacks
`mohs` but still has `hardness` binds the old column, so the rename follows your code —
additively, never rewriting the record (and the new name wins once data is written under it).
When a change needs the data *reshaped* — split one field into two, merge two into one, or
convert a type — mark the new field with `dc.Glue(fn)`. On load, a record that lacks the field
calls `fn(old)` with the old record as a read-only `{name: value}` mapping and uses the result:

```python
@dc.entity
class Locality:
    # old records persisted coords="48.1,11.5"; lat/lon now follow your code
    lat: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[0]))] = 0.0
    lon: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[1]))] = 0.0
```

Glue fires **only when the field is absent** from a record's persisted shape — so once data is
written in the new shape it is a no-op, and old records are never rewritten in place (additive,
like `RenamedFrom`).

v0.2 scopes both `RenamedFrom` and `Glue` to **non-indexed** fields read through live hydration
and decode (`get`/`query`/`pluck`); honoring them in the index, snapshot, and arrow decode
paths, and renaming an indexed field, are `[planned — v0.2]`.

### Rewriting old records: `migrate` and `verify`

`RenamedFrom` and `Glue` adapt old records *on read*. When you want the new shape **materialized
on disk** — so a derived field becomes a real persisted column you can then index — run the
offline `store.migrate()`:

```python
moved = store.migrate()   # re-encode every stale-shape record to the newest shape
```

`migrate()` hydrates each record persisted under an older lineage shape (through renames, glue and
defaults) and re-commits it under the current shape — additive (a new lineage row, never a blob
rewrite), owner-confined, lease-held, and crash-safe (it rides the normal commit; a partial run
just resumes). It is **idempotent** (a second run rewrites nothing) and commits in `batch`-sized
chunks (`store.migrate(batch=10_000)`, the default) so peak memory tracks the batch, not the store.

`store.verify()` is the read-only pre-flight: it decodes every record against the current code
*without* mutating anything and returns the `(typename, oid)` pairs that **don't** decode — a field
removed-then-re-added with no default or `Glue`, a type the running code no longer defines, or a
corrupt record. An empty list means the whole store reads cleanly. Run `verify()` before
`migrate()`.

### Recipe: deriving an *indexed* field (Glue + migrate)

`Glue` and `RenamedFrom` are read-time markers and live only on **non-indexed** fields — putting
one on a `dc.Index`/`dc.Unique` field raises at `@entity`. The reason is correctness, not
arbitrariness: an index is built from the *persisted* value, not the glued one, so a glued index
would silently index the wrong data. To end up with a *derived* field that is **also indexed**,
split it into two steps and let `migrate()` bridge them:

```python
# Step 1 — derive on read (NON-indexed), so old records adapt immediately on load
@dc.entity
class Locality:
    name: str
    lat: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[0]))] = 0.0

store = dc.Store.open("cabinet.store")
store.migrate()        # Step 2 — materialize `lat` into a real persisted column on disk

# Step 3 — `lat` is now a plain column; (re)declare it indexed and reopen
@dc.entity
class Locality:
    name: str
    lat: Annotated[float, dc.Index] = 0.0   # no Glue — a real, indexable column
```

After `migrate()`, every record physically carries `lat`, so adding `dc.Index` builds a **correct**
index over real data. The ordering matters: keep the field non-indexed while the value is glued
(the glue derives it on every read), and only add the index once `migrate()` has written the column
to disk. The same recipe applies to a renamed field you want indexed (`RenamedFrom` → `migrate()` →
`Index`). `migrate()` keeps existing indexes consistent automatically — it rewrites through the
normal commit path, so committed records fold into any built index and a reopen rebuilds indexes
from the newest records.

How it works (one paragraph, so the behavior is predictable): the store keeps a **type
lineage** — every field shape a class ever had gets its own row in the type dictionary, and
each record decodes through the shape it was written with, by field name. Old records are never
rewritten in place; they migrate to the newest shape the next time you modify and commit them.
A store that used schema evolution is still openable by this and any newer library version.

## Frozen entities

```python
@dc.entity(frozen=True)
class CatalogEvent:              # append-only: event logs, provenance, audit trails
    specimen_no: str
    kind: str
    note: str
```

Construct and commit them; afterwards any mutation — attribute write **or** in-place container
mutation — raises `FrozenEntityError`. Dirty tracking never arms for them, which also makes
them the cheapest records to commit in bulk.

## Concurrency and deployment

The contract ([ADR-001](design/ADR-001-concurrency-contract.md)) is **owner confinement**: a
store and its live graph belong to the thread that opened the store.

- Touching live entities or the store from another thread raises `WrongThreadError` —
  immediately and loudly, never corrupting.
- One writing process per store, enforced by the lease lock. Notably: `uvicorn --workers 4`
  means four processes — run datacrystal apps with **workers = 1** (how that scales anyway:
  [SCALING.md](design/SCALING.md)).
- If the OS pauses your process long enough for the lease to expire and be taken over, the next
  commit raises `LeaseLostError` instead of risking two writers.
- Foreign threads **ship work to the owner** instead of touching the graph:
  `store.submit(fn)` returns a `concurrent.futures.Future`. The owner runs pending
  submissions whenever it next calls into the store, or explicitly via
  `store.run_pending()`; under `aopen()` the event loop is woken instead, no owner call
  needed. Submission results must be plain data — a live entity in the result (even nested
  in a list/dict, or behind a `Lazy`) fails the future with `EntityEscapeError`.
- `store.commit()` itself is three-phase: it captures and encodes on the owner, hands the
  bytes to a dedicated IO worker thread, and finalizes on the owner. For sync stores that is
  an internal detail (commit blocks as before); for async stores it is what keeps the loop
  free.
- `store.snapshot()` gives ANY thread a frozen, read-only view of committed state — see
  [Snapshots and the commit-delta pipeline](#snapshots-and-the-commit-delta-pipeline).

### asyncio

```python
store = await dc.aopen("cabinet.store")     # binds the store to the running loop

async with store.transaction():             # an asyncio.Lock scope per unit of work
    store.root.entries.append(entry)        # … no other transaction interleaves …
# the scope committed on clean exit

await store.commit()                        # or commit explicitly outside scopes
store.close()
```

- `dc.aopen(...)` returns a `dc.AsyncStore` — the awaitable facade over `dc.Store` (same
  keywords, `await`-able `commit()`/`close()` plus the `transaction()` scope above). Every
  task on the owning loop may touch the graph (one thread by construction); foreign threads
  still get `WrongThreadError`.
- `await store.commit()` captures **before its first await**, then applies off-loop while the
  loop keeps serving. A task that mutates an entity while a commit is in flight is safe by
  contract: the write re-dirties the entity and lands in the *next* commit. Concurrent
  `commit()` calls serialize on an internal lock.
- The asyncio doctrine, documented from day one (ADR-001): **a critical section is the code
  between awaits.** Mutate-and-commit with no `await` in between, or wrap the scope in
  `transaction()`. An exception inside `transaction()` commits nothing; the in-memory
  mutations stay buffered (live objects have no rollback) — handle the exception and decide:
  fix and commit, or close to discard.
- Hydration faults (`Lazy.get()`, queries) load synchronously on the loop — the explicit
  `Lazy[T]` cut points make where that can happen visible in your model.

### FastAPI / Strawberry deployment: `datacrystal[web]`

The `datacrystal[web]` extra wires a store into a FastAPI/Strawberry app so your routes and
resolvers never have to learn the threading rules. It is **glue, not a new engine** — under it
are the same `store.snapshot()` (reads), `store.submit()` / `aopen()` (writes) and per-request
DataLoader you would wire by hand. Three primitives, one doctrine:

```python
from datacrystal.web import (
    create_app, read_snapshot, submit_write, get_store, graphql_context_getter,
)

app = create_app("cabinet.store")           # opens ONE store on startup, closes on shutdown

@app.get("/minerals/{qid}")                  # READ: a per-request snapshot, any thread
def read_one(qid: str, snap = Depends(read_snapshot)):
    hit = snap.query(Mineral.qid == qid)
    return to_pydantic(hit[0], face="public") if hit else Response(status_code=404)

@app.post("/minerals")                       # WRITE: fan the mutation into the owner
async def create(body: MineralCreate, write = Depends(submit_write)):
    def do(store):                           # runs ON the owner thread, then commits
        store.store(from_pydantic(body, Mineral))
        return store.commit()                # return plain data (a TID), never a live entity
    return {"committed_tid": await write(do)}
```

The **deployment doctrine** — and *why* each rule holds:

- **One store per worker — run `workers=1`.** A store is single-writer (the lease lock); the
  lifespan `create_app`/`store_lifespan` builds opens exactly one store for the process, on the
  startup thread (which becomes its owner thread, ADR-001). `uvicorn --workers 4` is *four
  processes*, and the second one to open the directory fails with `StoreLockedError` — so a
  datacrystal app scales **within** one process, not across writer processes (how that still
  scales: [SCALING.md](design/SCALING.md)).
- **Reads scale through snapshots, never the live graph.** `read_snapshot` hands each request a
  frozen snapshot — an any-thread/any-loop read view (ADR-002). A sync route runs in a threadpool
  worker (off the owner thread) and is *still correct*, because a snapshot is read-only committed
  state that can never violate owner confinement or dirty-tracking. Routes read `EntityView`s /
  DTOs (`to_pydantic`), never live entities. The snapshot is **pooled per commit watermark**: a
  fresh `store.snapshot()` rebuilds its query index over the whole store on first query (ADR-002 —
  snapshot indexes are never the owner's), so building one per request is O(store-size) per
  request. Instead one snapshot is shared by every read at a watermark — index built once,
  rebuilt only when a commit advances it — so a read is **O(n)/commit, not O(n)/request** (on real
  Gene Ontology, 38k terms: ~52 ms → ~0.7 ms p50). Its WAL read txn is released when a commit
  supersedes the watermark or on shutdown.
- **Writes serialize through the owner.** A foreign thread may not mutate the graph — a direct
  live write still raises `WrongThreadError`, unchanged. `submit_write` instead *ships a closure*
  to the owner via `store.submit()`; the mutation **and commit** run on the owner thread, and
  `await write(fn)` resolves only once it is durable (back-pressure by construction). Write
  routes are `async def` so they run on the owner loop and the closure runs inline; return plain
  data from the closure (an OID or a DTO) — a live entity in the result raises `EntityEscapeError`.
- **GraphQL gets a per-request snapshot *and* a per-request DataLoader.** Pass
  `GraphQLRouter(schema, context_getter=graphql_context_getter)`: each request's context carries
  one pinned snapshot and a **fresh** `SnapshotLoader` (`cache=False`) over it. This is
  mandatory, not an optimization — a process-lifetime loader caches by default and would leak
  resolved entities across requests *and* across snapshot watermarks (a stale read after a
  commit). Every field on the request reads from the one watermark, so a nested graph traversal
  is internally consistent even while the owner keeps committing — and sibling reference edges
  batch into one `Snapshot.get_many` instead of N+1-ing the store.

`get_store` exposes the one process store directly (for a route that needs it, e.g. to call
`submit_write` itself); it raises if the app was not built with the lifespan. The frameworks
(`fastapi`/`strawberry`/`pydantic`) live only inside `datacrystal.web` — a bare
`import datacrystal` never pulls them, staying inside the `{msgspec, pyroaring}` budget.

## Snapshots and the commit-delta pipeline

### `store.snapshot()` — reading from any thread

A snapshot is a frozen view of the committed state at one commit watermark, and the
sanctioned way for worker threads to read while the owner keeps writing (ADR-001 rider 2):

```python
def report(store: dc.Store) -> int:        # runs on any thread
    with store.snapshot() as snap:         # pins one durable commit boundary
        S = dc.fields(Specimen)
        return snap.count((S.quality == "fine") & (S.mass_g >= 100.0))
```

- `snap.get(oid_or_ref)`, `snap.all(EntityClass)` and `snap.root` return **immutable
  views** (`dc.EntityView`): field access mirrors the live class, entity references are
  explicit `dc.Ref` tokens you resolve via `snap.get(ref)`, lists come back as tuples,
  dicts as read-only mappings. Never live entities — nothing a worker thread does with a
  snapshot can violate confinement or dirty tracking.
- `snap.get_many(refs)` batch-resolves an iterable of OIDs / `dc.Ref` tokens / `EntityView`s
  to a `list[EntityView | None]` aligned 1:1 with the input — the snapshot twin of
  `store.get_many()` and the seam the `datacrystal[web]` GraphQL DataLoader and REST list
  endpoints build on (never N+1: one storage round-trip per chunk, cached OIDs cost nothing).
  Unlike `snap.get()` it is **miss-tolerant** — an absent or deleted OID yields `None` in its
  slot rather than raising `DanglingRefError`, exactly what a key-aligned loader needs (v0.x
  deletes are unchecked, ADR-003).
- `snap.query(cond)` and `snap.count(target)` answer the full Condition AST at the
  watermark — bitmap-indexed like the live store, results as `EntityView`s. The indexes
  behind them are **snapshot-local**, rebuilt from the pinned view on first use (one-time
  O(extent) per class, cached for the snapshot's lifetime). `snap.index_bitmaps(Cls)`
  exposes them directly as frozen bitmaps/mappings (`dc.SnapshotIndexes`) — the bootstrap
  material for index-shaped sidecars.
- `snap.incoming(view_or_ref_or_oid)` answers **backlinks at the watermark** — the frozen
  twin of `store.incoming()`, built from a snapshot-local reverse index (never shared with
  the owner's). Takes the snapshot's own currency (an `EntityView`, a `dc.Ref`, or a raw
  OID), returns `EntityView` referrers; a referrer committed *after* the snapshot is absent,
  and a deleted target still names its dangling referrers (the ADR-003 enumeration seam)
  even when `snap.get(dead)` raises `DanglingRefError`.
- `snap.tid` is the pinned watermark; `snap.types` is the type lineage at that watermark
  (what a delta consumer needs to bootstrap, see below).
- Close promptly (use the context manager): on the sqlite backend an open snapshot holds a
  WAL read transaction, which blocks checkpoint truncation.
- A snapshot taken while a commit is mid-flight may be one commit **ahead** of
  `store.last_tid` — the commit it sees is already durable; views are never torn.

### The commit-delta pipeline — what sidecars ride on

Every commit is describable as one versioned, msgpack-encodable **delta** — the public
[COMMIT-DELTA-v1](design/COMMIT-DELTA-v1.md) contract (**LOCKED v1**, 2026-06-12). Attach
a consumer and every commit hands it exactly one delta, in TID order, on the owner thread,
strictly after the commit is durable:

```python
with store.snapshot() as snap:                  # 1. bootstrap at a watermark
    consumer = MySidecar.bootstrap(snap)        #    (lineage + state + watermark)
store.attach(consumer)                          # 2. ride the stream from there
```

- `attach()` requires `consumer.watermark == store.last_tid`: deltas are **not retained**,
  a consumer that is behind (or ahead — a store restored from backup) must rebuild from a
  snapshot. This is by design: sidecars are rebuildable derived data, always.
- Update ops carry the record's **prior payload**, so index-shaped consumers un-index old
  values without ever reading the store; `store.delete()` emits **delete tombstones**
  (`payload` nil, `prior` = the last payload) through the same channel.
- A consumer that raises is **detached** with a `ConsumerDetachedWarning` — the commit
  stays durable, the store stays healthy, the sidecar rebuilds and re-attaches.
- Writing a consumer? Implement the `dc.DeltaConsumer` protocol (a `watermark` property plus
  `apply(delta)`); `store.attach(consumer)` then rides it on the stream.
  `datacrystal.testing.check_delta_consumer(factory, content=...)` certifies an implementation
  against every contract obligation (idempotency, ordering, gap/version refusal, prior-based
  un-indexing); `datacrystal.testing.CountingConsumer` is the minimal reference implementation,
  `datacrystal/contract/applier.py` the normative one.

## The delta log: durable audit history

`datacrystal.deltalog` is the pipeline's first consumer that ships with core (no extra,
no third-party dependency — stdlib + msgspec). It appends every commit's delta, byte for
byte and in TID order, to an append-only file set, so the store gains an **audit history**
and a foundation for time-travel-by-replay and follower catch-up.

It is **opt-in**: a store with nothing attached pays nothing and is byte-identical to one
that never had a log, so turn it on only when you want history (it has commit-latency and
disk costs — see below). And because deltas are never retained, a log records **only from
the moment you attach it** — attach at the store's birth (`watermark == 0`) for a complete
history, or later (via `bootstrap()`) to start the trail from that point on; history before
the attach cannot be recovered.

```python
from datacrystal.deltalog import DeltaLog

log = DeltaLog("cabinet.deltalog")     # attach to a FRESH store: records all history
store.attach(log)
... store.commit() ...

for delta in log.replay():             # every committed delta, in TID order
    ...                                # feed a follower, an applier, an audit view
```

- **Full replayability from a fresh store.** A log attached at `watermark == 0` holds the
  complete history: `log.replayed_state()` (replaying through the reference applier)
  reconstructs the exact committed state — the equality check behind time-travel-by-replay.
- **Crash-safe by construction.** Each flush fsyncs the segment bytes *before* committing
  the `manifest.json` watermark (temp-file + rename), so the durable watermark never names
  bytes that did not land. A reopen truncates any partial append and sweeps any orphan
  segment left by a killed commit — the on-disk log is always an exact, gapless commit
  prefix (a `kill -9` torture test gates this).
- **Durability knob.** `flush_every=1` (default) makes the log exactly as durable as the
  store. `flush_every=N` batches N deltas per fsync — the durable watermark then trails by
  up to N-1 commits, and a crash in that window means rebuild (the engine refuses a
  behind-the-watermark re-attach).
- **Mid-life attach** records changes from the join point on: `DeltaLog.bootstrap(path,
  snapshot)` pins the watermark to the snapshot so `attach()` accepts it (deltas before the
  join were never retained — §5). Its replay is the change-feed from the join onward — the
  honest audit semantics. A full-state checkpoint that would make a mid-life log self-
  contained for replay is `[planned — demand-driven]`.
- **Retention is the operator's policy.** The log is append-only and grows with history
  (segments roll at `max_segment_bytes`); pruning old segments is a deliberate operator
  choice, never the engine's. Like the store, a log directory has one owner process.

## Full-text search: datacrystal[fts]

`pip install 'datacrystal[fts]'` (adds `snowballstemmer`). The extra is a commit-delta
consumer: an SQLite FTS5 index in its own sidecar file, kept current by the pipeline,
rebuildable from a snapshot at any time.

```python
from datacrystal.fts import FullTextIndex

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    notes: Annotated[str | None, dc.FullText(language="de")] = None

idx = FullTextIndex("cabinet.fts")     # config read from the dc.FullText markers
store.attach(idx)
... store.commit() ...

for hit in idx.search("Kristall"):     # stemming: finds "Kristalle", ranked by BM25
    print(hit.score, hit.typename, hit.snippet)   # snippet marks matches [like] this
minerals = store.get_many([hit.oid for hit in idx.search("Tsumeb", cls=Mineral)])
```

- **Stemming is per-field**: `dc.FullText(language="de")` gets index-time Snowball
  stemming (27 languages by ISO code or Snowball name); bare `dc.FullText` is fold-only
  exact matching (case + diacritics + Unicode-compat forms fold: `m²` matches `m2`,
  `Glänzend` matches `glanzend`). Exact matches outrank stem-only matches.
- Quoted phrases stay phrases; loose terms combine per `match=`: **`"any"` (the default)**
  ranks the OR-union of the terms (natural-language recall — a question doc needn't contain
  *every* word), `"all"` requires every term (precise faceting). User input is quoted into the
  FTS5 expression — it can never inject MATCH operators. `cls=` narrows to one entity type;
  `hit.snippets` maps each matched field to its highlighted excerpt, and `hit.snippet` is the
  first non-empty one.
- Attaching to a lived-in store: `FullTextIndex.bootstrap(path, snapshot)` (deltas are
  not retained — the snapshot recipe above). Reopening with a different field/language
  configuration raises `FtsConfigError`: rebuild, a half-matching index is stale.
- Honest limits: unsegmented CJK runs are single tokens under unicode61 (`水晶です` is
  findable only as that whole run) `[planned — segmenting tokenizer, demand-driven]`;
  abugida-script languages (hi/ne/ta) are refused loudly rather than silently broken.
  Like the store, an index is used from the thread that opened it.

## Arrow mirrors: datacrystal[arrow]

`pip install 'datacrystal[arrow]'` (adds `pyarrow`). The mirror is the pipeline's second
consumer and the columnar answer to projection/range analytics (a one-column read over
millions of records stops costing minutes): per-type Arrow tables, persisted as parquet
in a mirror directory, kept current from commit deltas.

```python
from datacrystal.arrow import ArrowMirror

mirror = ArrowMirror("cabinet.mirror")
store.attach(mirror)
... store.commit() ...

table = mirror.table(Specimen)          # pyarrow.Table at the mirror's watermark
import duckdb, polars as pl
duckdb.from_arrow(table)                # zero-copy
pl.from_arrow(table)                    # zero-copy
table.to_pandas()
```

- Rows carry `__oid__` (int64, the primary key — also `ArrowMirror.OID_COLUMN`) plus every
  persisted field, types inferred and promoted through a total lattice (`bool < int < float`;
  lists element-wise; anything mixed becomes msgpack-binary —
  `datacrystal.arrow.decode_fallback()` restores the value), so additive schema evolution can
  never wedge the mirror. Entity references are int64 OID columns — join them, feed them to
  `store.get_many()`, or use the OID as the handoff key for the
  [analytics recipe](#analytics-at-scale-filter-here-aggregate-in-duckdb) below.
- Persistence is an LSM of parquet segments with `manifest.json` as the atomic,
  fsync-ordered commit point: reopening resumes at the durable watermark, a crash
  mid-flush is swept on open. `mirror.compact()` collapses each type to one plain
  parquet file — after it, the `data/` directory is directly readable by DuckDB/Spark
  (the parquet-datalake story).
- `only=[Specimen, ...]` mirrors a subset; `flush_every=N` batches flushes (durable
  watermark trails by up to N−1 commits; a crash in that window costs a rebuild).
  Mid-life attach: `ArrowMirror.bootstrap(path, snapshot, batch=N)` **streams** the extent in
  `batch`-sized chunks (default 50 000), so peak memory is O(batch), not O(extent) — a store
  larger than RAM can be mirrored; lower `batch` for a tighter footprint. (`batch` is the
  bootstrap chunk; `flush_every` above is the separate post-bootstrap delta-batch knob.) The
  watermark is stamped only by the final flush, so a crash mid-bootstrap forces a clean
  re-bootstrap rather than trusting a partial extent. A mirror directory has one owner
  process, like the store file.
- DuckDB/polars recipe polish (joins across mirrors, parquet-on-S3) stays on the
  roadmap `[planned — v1, items 7/16]`.

### Analytics at scale: filter here, aggregate in DuckDB

Aggregates over a filtered set — `sum`/`avg`/`min`/`max`, `GROUP BY` — have no fast path in
the live object layer **on purpose**: the engine is rule-based and never grows an optimizer
(see [`explain()`'s two rules](#reading-get-query-lazy-references)). `pluck` reads a column
without building entities,
but you still pay O(hits) Python to fold it — summing 1.4 M values took ~5.6 s on the MaStR
eval. The mirror is the columnar tier: hand its parquet to **DuckDB** and the same fold is a
vectorized scan. Two shapes, both at the mirror's `watermark`:

**(1) Aggregate entirely in DuckDB** when the filter is itself a plain columnar predicate —
the simplest path. The mirror table goes in zero-copy; DuckDB does the filter, the group, and
the fold in one query:

```python
import duckdb
from datacrystal.arrow import ArrowMirror

mirror = ArrowMirror("cabinet.mirror")
store.attach(mirror)
... store.commit() ...

finds_tbl = mirror.table(Find)          # pyarrow.Table, zero-copy; named in the SQL
duckdb.query(
    "SELECT grade, count(*) AS n, sum(mass_g) AS total, avg(mass_g) AS mean "
    "FROM finds_tbl WHERE grade IS NOT NULL GROUP BY grade ORDER BY grade"
).fetchall()
# [('A', 2, 532.5, 266.25), ('B', 3, 366.0, 183.0), ('C', 1, 58.0, 58.0)]
```

The Python equivalent — `for v in store.pluck(...): total += v` — produces the same numbers
but materializes and folds every hit in the interpreter; DuckDB stays in vectorized C over the
Arrow buffers.

**(2) Filter in datacrystal, aggregate in DuckDB** when the filter wants the bitmap index (an
indexed `==`/`.in_()`, a reverse-ref `incoming()`, a graph walk). The datacrystal-side query
yields **OIDs**; DuckDB aggregates over only those rows by joining on `ArrowMirror.OID_COLUMN`.
Use a `store.snapshot()` for the filter — its `watermark` equals the mirror's, and an
`EntityView`/`Ref` carries `.oid` without hydrating the entity:

```python
F = dc.fields(Find)
with store.snapshot() as snap:           # snap.tid == mirror.watermark
    hit_oids = [v.oid for v in snap.query(F.grade == "B")]   # bitmap → OIDs

finds_tbl = mirror.table(Find)
duckdb.execute(
    f"SELECT sum(mass_g) FROM finds_tbl "
    f"WHERE {ArrowMirror.OID_COLUMN} IN (SELECT * FROM UNNEST(?))",
    [hit_oids],
).fetchone()                             # (366.0,)
```

The bitmap restricts the scan to the hits — `IN (SELECT * FROM UNNEST(?))` lets DuckDB build a
hash set from the OID list rather than parsing a giant literal. (For a very large OID set,
register it as its own table — `duckdb.register("hits", pa.table({"oid": hit_oids}))` — and
`JOIN` it instead.)

**Off-thread, file-based.** Both shapes call `mirror.table(...)` on the store's owner thread
(it folds the LSM segments), then hand the immutable Arrow table to DuckDB anywhere. To skip
the in-RAM fold entirely, `mirror.compact()` first — each type collapses to one fold-free
parquet file — then point DuckDB at the files via `mirror.parquet_dir(Find)`:

```python
mirror.compact()                         # one plain parquet file per type
glob = str(mirror.parquet_dir(Find) / "*.parquet")
duckdb.execute(
    f"SELECT grade, sum(mass_g) FROM read_parquet('{glob}') GROUP BY grade"
).fetchall()
```

After `compact()` that directory is the live set exactly (tombstones dropped); **without** it a
`parquet_dir()` may hold several LSM segments that still need newest-wins folding, so read
`table()` (or compact first) when you need precise results. `duckdb` is not a datacrystal
dependency — `pip install duckdb` alongside `datacrystal[arrow]`; `polars`/`pandas` read the
same `mirror.table(...)` if you prefer them.

## Transactional guarantees (A/C/I/D)

`store.commit()` is **one transaction**. This section is the per-letter account of what that
buys you — what each of atomicity, consistency, isolation, and durability guarantees, which
in-repo test proves it, and (just as important) what datacrystal **does not** claim. It is the
authoritative companion to [Durability and crash safety](#durability-and-crash-safety) (the loss
windows) and [Deleting](#deleting) (the referential-integrity caveat); where they overlap, they
agree.

### Atomicity — all of a commit, or none of it

A commit's records, out-of-line blobs, deletes, and metadata are written inside a single SQLite
`BEGIN IMMEDIATE … COMMIT`; any error rolls the whole batch back (`except: ROLLBACK; raise`) and
nothing lands. After a crash you see an **exact prefix** of your acked commits — never a torn one,
never half a commit.

- **Proven by** the CI-gated `kill -9` torture test (`tests/integration/test_crash.py`): a writer
  SIGKILL'd mid-commit reopens to exactly its last acked commit.
- **And** the SQL-layer rollback test (`tests/integration/test_sql_rollback.py`): a fault injected
  *after* the records-and-blob inserts but *before* `COMMIT` leaves **zero** rows on disk and the
  watermark unmoved — SQLite itself undoes the half-written batch, so atomicity is proven at the
  storage layer, not merely asserted by construction.

### Consistency — invariants checked before the commit is taken

Uniqueness (`dc.Unique` → `UniqueViolationError`), schema validity (additive type lineage →
`SchemaMismatchError`), frozen-entity immutability (`FrozenEntityError`), and the temporal-index
comparability rule (`MixedTemporalIndexError`) are all enforced **before the TID is allocated**, in
P1. A rejected commit therefore consumes no TID and leaves the sequence **gapless and retryable**
(invariant 5 — replay determinism is a public contract). The buffers stay intact, so you can fix
and re-commit.

What datacrystal does **not** enforce here:

- **Referential integrity is not enforced.** `store.delete()` is *unchecked* in v0.x
  ([ADR-003](design/ADR-003-delete-semantics.md)): a delete can leave other records pointing at the
  gone OID, and *following* such a stale reference raises `DanglingRefError` only at follow time —
  never silently. The dev-time bridge is `Store.open(strict_deletes=True)` (raises at the offending
  `commit()`, naming the referrers, still before the TID so the sequence stays gapless) or
  `Store.open(debug=True)` (warns with `DanglingDeleteWarning` and commits anyway). See
  [Deleting](#deleting). Checked delete (refuse-if-referenced, cascades) lands with the v1
  reverse-reference index `[planned — v1]`.
- **Live objects have no rollback.** A rejected `commit()` reverts nothing in memory — your
  in-RAM mutations stay buffered (that is what makes the commit retryable). Decide explicitly: fix
  and re-commit, or `close()` to discard the uncommitted changes.

### Isolation — single-writer serialization, WAL snapshot reads

Writes never interleave because there is exactly **one writer**: the store and its live graph are
owner-confined (a foreign thread raises `WrongThreadError`, [ADR-001](design/ADR-001-concurrency-contract.md))
and a second *process* opening the directory is refused by the lease lock (`StoreLockedError`). So
all writes serialize through the owner — no write-write conflicts to resolve.

Readers get **snapshot isolation** through SQLite WAL: each `store.snapshot()` (and each streamed
`open_blob()`) reads from its own connection pinned to one commit watermark
([ADR-002](design/ADR-002-storage-read-views.md)), so a reader on another thread sees a stable,
never-torn view while the owner keeps committing.

What datacrystal does **not** claim: there is **no configurable SQL isolation level** (no
`READ COMMITTED`/`SERIALIZABLE` knob) and **no multi-writer MVCC**. Isolation comes from the
single-writer contract plus WAL read views, not from concurrency control over competing writers.

### Durability — the configurable triad

Durability is the `durability=` triad chosen at `Store.open`, each with an explicit loss window
(the full account is in [Durability and crash safety](#durability-and-crash-safety) and
[Open a store](#open-a-store)):

- **`"commit"`** — `synchronous=FULL` (plus `F_FULLFSYNC` on macOS): every acked commit is fsync-
  durable, surviving even OS crash / power loss (cost: ~4 ms/commit on macOS, honestly).
- **`"interval"`** (default) — `synchronous=NORMAL`, WAL group-commit: a **process** crash
  (`kill -9`) loses nothing; an OS crash or power loss may trim the last few commits, but the file
  is never corrupted.
- **`"never"`** — `synchronous=OFF`: no fsync, an OS crash can corrupt the file. Benchmarks and
  throwaway scratch stores only.

Honesty note: process-crash durability **is** in-process testable and CI-gated (the `kill -9`
test runs under `"commit"`). True power-loss durability rests on SQLite's `synchronous=FULL`/
`F_FULLFSYNC` settings — it cannot be exercised from within a process, so it is **settings-backed,
not in-process tested**.

### What we do *not* claim

datacrystal deliberately does **not** wear a blanket **"ACID compliant"** badge. Concretely:

- no blanket ACID claim — read the per-letter guarantees above instead;
- no configurable **SQL isolation level** and no multi-writer MVCC (isolation = single-writer +
  WAL snapshots);
- no **referential integrity** in v0.x (`store.delete()` is unchecked; `DanglingRefError` is the
  loud follow-time signal, not a commit-time guard).

Each guarantee above is exactly as strong as its cited test or setting — no more, no less.

## Durability and crash safety

- A commit is one SQLite transaction; `durability="commit"` makes it fsync-durable per commit,
  the default `"interval"` group-commits at WAL checkpoints (see [Open a store](#open-a-store)
  for the triad's exact loss windows).
- Kill -9 mid-commit, power loss, OS crash: on reopen you get exactly a committed prefix —
  never a torn commit. Under `"commit"` that prefix is *every acked commit*; under
  `"interval"` an OS crash may trim the tail. This is CI-gated (the SIGKILL crash test runs
  under `"commit"`) and was true from the first walking skeleton.
- Backup: close the store (or pause writing) and copy the directory.
  `sqlite3.backup`/Litestream PITR recipes are `[planned — docs, v0.x]`.
- Opening a store written by a **newer** library version raises `NewerStoreError` naming both
  versions — never a misread.

## Typing

datacrystal is typed-Python-first, and the **runtime is always exact**. But three spots use Python
in ways a static checker (pyright/basedpyright/mypy) cannot follow, so they flag a false positive.
This section is the single, authoritative list — meet them once here, apply the blessed workaround,
and your checker is clean with **zero** behavior change. (A pyright/mypy plugin that would erase
these is `[planned]`; see *Deferred* below.)

### 1. Class-attribute conditions read as the field's value type

`Mineral.mohs >= 6.0` is the documented primary query form, but a checker sees `Mineral.mohs` as
`float | None` and reads the whole thing as `float >= float -> bool`, not a `Condition`. The
**workaround** is the typed field proxy `dc.fields(C)` — it returns a `FieldProxy` whose attributes
are `FieldExpr`s, so the comparison types as a `Condition`:

```python
M = dc.fields(Mineral)
hits = store.query((M.crystal_system == "cubic") & (M.mohs >= 6.0))   # checker-clean
```

Both forms are identical at runtime; `dc.fields(C)` is purely for the checker. (Also covered inline
in [Reading](#reading-get-query-lazy-references).)

### 2. A `dc.Blob` field reads back as `dc.BlobHandle`, not `bytes`

A field declared `Annotated[bytes, dc.Blob]` hydrates to a `dc.BlobHandle` (lazy — `.size`/`.hash`
are free, `.bytes()` fetches once). `BlobHandle` is **not** a `bytes` subclass, so a checker that
trusts the `bytes` declaration flags `.bytes()`/`.size` on the field. There is no pragma that fixes
this cleanly — treat the handle as the real shape (the declared `bytes` is the *write*-side type),
and reach for `.bytes()` / streamed `store.open_blob()` as documented in
[Storing binary blobs](#storing-binary-blobs-pdfs-scans-invoices).

### 3. Assigning a `dc.BlobSource` to a `bytes`-typed `dc.Blob` field

The streamed-write form assigns a `dc.BlobSource` (or `dc.blob_from_path(...)`) to a field typed
`bytes`, which a checker flags `[assignment]`. The **workaround** is a `# type: ignore[assignment]`
on that line (or a per-file `# pyright: reportArgumentType=false` in code that writes many):

```python
inv.pdf = dc.blob_from_path("/tmp/2026-0042.pdf")   # type: ignore[assignment]
store.commit()
```

This is the same write/read asymmetry as #2, from the write side. (Also noted inline in
[Writing a big blob](#writing-a-big-blob-without-holding-it-whole-in-ram).)

### Not a false positive: `list`/`dict` read back as persistent containers

For completeness — a field declared `list[str]` (or `dict[...]`) reads back as a
`dc.PersistentList` / `dc.PersistentDict`. This is **not** a checker quirk and needs **no
workaround**: `PersistentList` subclasses `list` and `PersistentDict` subclasses `dict`, so the
read-back value stays assignable to the declared type and the checker is happy. The only semantic to
remember is the runtime one, not a typing one: **assignment copies** (mutate *through* the field —
see [Lists and dicts](#lists-and-dicts-inside-entities)).

### Deferred

A pyright/mypy plugin (or `.pyi` overloads) that would type `EntityClass.field <op> value` as a
`Condition` and reflect the real read-back types (`BlobHandle`, `PersistentList[T]`) — erasing
quirks 1–3 without any pragma — is **out of scope here and deferred** to its own backlog issue. The
runtime exactness above is unaffected by whether it ever ships.

## Glossary

The core jargon, in one place — terms that appear above before they are defined:

- **OID** — object identifier: the stable 64-bit identity of a persisted entity. One live
  instance per OID (`a.friend is b` survives a restart).
- **CID** — class identifier: the identity of a *class shape*. A field-shape change mints a new
  CID, so old records keep decoding through their own persisted shape (additive schema evolution).
- **TID** — transaction identifier: the sequence-derived id of a commit. Never wall-clock;
  the sequence stays gapless even after a rejected commit, so replay is deterministic.
- **watermark** — the latest committed TID (`store.last_tid`). Snapshots, the index cache, and
  the delta pipeline are all pinned to / validated against a watermark.
- **owner-confinement** — the concurrency contract (ADR-001): a store and its live entities are
  bound to the thread that opened them; a foreign thread raises `WrongThreadError` before any
  mutation lands. Snapshots are the cross-thread read path.
- **P1 / P2 / P3** — the three commit phases: **P1** captures the change set (and builds the delta
  when consumers are watching), **P2** does the backend I/O (durability), **P3** flips to the new
  state and delivers deltas. `commit()` keeps this shape even when synchronous.
- **extent** — every committed instance of a class. An indexed read costs `f(hits)`, never
  `f(extent)`; a residual `query()` over a non-indexed predicate hydrates the whole extent.
- **residual** — the part of a query the bitmap indexes can't answer, evaluated as a Python
  filter over the candidate set. `explain()` shows what answers from the index vs. the residual.
- **swizzle** — at encode time, an in-RAM reference to another entity is replaced by its OID (an
  msgpack extension value); on decode the OID is resolved back to the one live instance. No
  pickle, no code execution.

## Errors

Everything derives from `dc.DataCrystalError`:

| Error | Meaning |
|---|---|
| `StoreClosedError` | operation on a closed store |
| `StoreLockedError` | another live process holds the lease |
| `LeaseLostError` | this process lost the lease (paused too long); refusing to write |
| `WrongThreadError` | live entity/store touched from a foreign thread |
| `EntityEscapeError` | a `submit()` result tried to carry a live entity across the owner boundary |
| `FrozenEntityError` | mutation of an `@entity(frozen=True)` record |
| `NotAnEntityError` | a non-entity where an entity is required |
| `UniqueViolationError` | duplicate value for a `dc.Unique` field in a commit |
| `SchemaMismatchError` | a class change beyond additive evolution (see [Schema evolution](#schema-evolution)) |
| `UnregisteredTypeError` | store has records of a class not imported in this process |
| `NewerStoreError` | store written by a newer format version |
| `CorruptRecordError` | a record failed its checksum — the file is damaged |
| `QueryError` | malformed condition (two classes mixed, missing parentheses, …) |
| `MixedTemporalIndexError` | a `datetime`/`date` `SortedIndex` field mixed naive and aware values — store timestamps timezone-aware |
| `DeletedEntityError` | write/`store()` on a `store.delete()`d instance — it is detached; create a new entity |
| `DanglingRefError` | a reference to a deleted (or never-existing) record was followed (see [Deleting](#deleting)) |

Pipeline consumers can additionally raise the contract errors (`datacrystal.contract`):
`DeltaGapError` (history missing — resync/rebuild; also raised by `attach()` on a
watermark mismatch) and `DeltaFormatError` (malformed/newer-versioned delta). The extras
add `datacrystal.fts.FtsConfigError` and `datacrystal.arrow.MirrorConfigError` (both
`DataCrystalError`s): the sidecar file/directory contradicts the requested configuration
or is newer than the installed extra — rebuild rather than guess.

Four warnings live outside the exception family (all `UserWarning`s):
`UntrackedMutationWarning`, emitted by the `debug=True` safety net when a mutation slipped
past the dirty tracking — the entity is committed anyway; fix the write path it names;
`DanglingDeleteWarning`, emitted under `debug=True` when a `commit()` deletes an entity another
record still references — the commit proceeds (use `strict_deletes=True` to raise instead);
`ConsumerDetachedWarning`, emitted when an attached delta consumer raised during
delivery and was detached (the commit is durable; rebuild the sidecar and re-attach);
and `UnseenTypeWarning`, emitted when `query()`/`count()`/`pluck()` run against a class
the store has no committed records of (the result is empty — first run, or a forgotten
`commit()`).

## Planned features and when they land

Sequencing follows the ratified [roadmap](design/ROADMAP.md); the live backlog (in/order)
is on [GitHub Issues](https://github.com/themerius/datacrystal/issues). **v0.1.0 (the API-freeze
baseline) and the purely additive surface through v0.6.0 are all tagged (the v0.1.0 freeze is
never broken); PyPI publication is still deferred (names reserved).**

| Feature | Where it lands |
|---|---|
| **object-store (S3) primary backend** — "the only infra is a blob store" | `[planned — item 16]`; feasibility spiked (manifest-LSM + conditional-PUT CAS), gated on the retained log + a scope ruling |
| vector search — `datacrystal[vector]`, usearch, ≥2 vector fields per entity | extension package, after v1 |
| property-graph recipes, cross-mirror DuckDB recipes | v1 |
| indexed-field renames, sets, custom scalar types, CJK-segmenting FTS tokenizer | demand-driven (offline `migrate`/`verify` and `dc.RenamedFrom`/`dc.Glue` already ship — see [Schema evolution](#schema-evolution)) |

Without the `[arrow]` extra installed, getting data into pandas is still a two-liner via
the decode-level projection (copies, not zero-copy — but no entities are built either):

```python
import pandas as pd
df = pd.DataFrame(store.pluck(Mineral, "qid", "name", "crystal_system"),
                  columns=["qid", "name", "system"])
```
