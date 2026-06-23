# How-to: query and page results

Goal-oriented reading recipes — windowing large result sets, paging the newest/top-N, and finding
backlinks. For the complete operator and planner contract see
[the Reading API reference](../reference.md#reading-api); for *why* the planner is rule-based see
[the query-semantics explanation](../explanation.md#query-semantics-the-planner-the-residual-and-the-candidate-set).

## Page a large result set with limit / offset

`query()` and `pluck()` (and the snapshot twins `snap.query()`/`snap.all()`) take `limit=` and
`offset=` to window the result deterministically (ascending OID):

```python
top10 = store.query(Mineral.crystal_system == "cubic", limit=10)  # loads 10, not the extent
page2 = store.query(Mineral, limit=50, offset=50)
heads = store.pluck(Mineral, "name", limit=100)                   # windows the decode-level read too
```

On a fully-indexed read the slice hits the candidate OIDs **before** hydration —
`query(C, limit=10)` loads 10 records, not the extent. A residual predicate must decode-to-filter
first, so there the window only trims the materialized result (it cannot prune the scan). Order is
deterministic: `query(C, limit=k) == query(C)[:k]`, and `page1 + page2 == query(...)[:n]`.

## Sort, then window, with order_by

```python
hardest = store.query(Mineral, order_by=(Mineral.mohs, "desc"), limit=10)  # SortedIndex → cheap
by_name = store.pluck(Mineral, "name", order_by=Mineral.name)  # bare field = ascending
page = store.query(Mineral, order_by=(Mineral.mohs, "asc"), limit=50, offset=50)  # stable paging
```

`order_by=(field, "asc"|"desc")` (a bare `field` means ascending) sorts the **whole** match set
before the window — **NULLs sort last**, ties break on **ascending OID** so paging is deterministic.
An indexed sort field is ordered straight from the index; mark a field `dc.SortedIndex` if you page
by it often, because an un-indexed sort field must decode that field for every match first.

## Recipe: paging the newest/top-N by an indexed field

For "the N newest" (or hardest, heaviest, …) by an indexed field, reach for a bare
`order_by` + `limit` — **not** a `>= sentinel` predicate. Both are correct and return the same
rows; one stays flat as the dataset grows and the other does not.

```python
from datetime import datetime, timezone
from typing import Annotated
import datacrystal as dc

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

## Stream a huge match set in bounded memory

When you do need the live entities but the match set is enormous, `query_iter` yields them chunk by
chunk so peak RAM is O(chunk), not O(extent):

```python
for m in store.query_iter(Mineral.crystal_system == "cubic"):   # millions of matches
    process(m)                                                  # O(chunk) RAM throughout
```

See [the ingest-and-memory how-to](ingest-and-memory.md) for the full bounded-memory toolkit.

## Recipe: backlinks — who references this?

`store.incoming(entity)` returns every committed entity that **references** `entity` — the inverse
of following a ref. Backlinks power impact analysis, orphan detection, and digital-twin /
system-of-record traversal ("which records point at this one?").

```python
quartz = store.get(Mineral, qid="Q43010")
for referrer in store.incoming(quartz):     # every entity that points at quartz —
    print(referrer)                         # eager AND Lazy refs, in scalar fields
                                            # and inside list/dict containers
```

- Answered from a **rebuildable reverse-reference index** (derived data, invariant 11; cached to the
  watermark-stamped sidecar when `cache_index=True`, never authoritative — [ADR-005](../design/ADR-005-index-cache.md)):
  the first `incoming()` scans the store once to build it (one-time O(extent), like the lazy forward
  indexes), then it is maintained incrementally at every commit — a second, unrelated backlink is an
  O(1) posting lookup, and a warm reopen loads it from the sidecar instead of rescanning.
- An unwatched store pays **nothing**: the reverse index is built only on first use, so if you never
  call `incoming()` your commits are byte-identical and free of its upkeep.
- A deleted **target** keeps its postings, so `incoming(dead)` enumerates the entities now
  **dangling** at the dead OID (OIDs are never reused) — exactly the referrers a checked delete
  would act on ([ADR-003](../design/ADR-003-delete-semantics.md)). A deleted **referrer** drops
  out. Checked delete itself (refuse-if-referenced, cascades) is `[planned — v1]`.
- The same backlinks at a pinned watermark are `snap.incoming(...)` — see
  [the snapshots how-to](snapshots-and-delta-log.md).
