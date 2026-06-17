# How-to: ingest big data and keep memory bounded

Goal: import a dataset larger than RAM, and keep peak memory under control while you do. The *why*
behind identity and the pinned root is in
[the memory explanation](../explanation.md#identity-and-memory); the API surface is in
[Concurrency primitives](../reference.md#concurrency-primitives) and
[Reading API](../reference.md#reading-api).

## Keeping memory bounded

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
   [the analytics how-to](analytics.md).

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

## Demoting idle lazy references

Loaded `Lazy` references stay loaded until you drop the holder — **or** you open the store
with `lazy_timeout=<seconds>`: the LazyReferenceManager then demotes handles idle past the
timeout back to unloaded, releasing the subgraph behind the cut point (the next `.get()`
transparently reloads, identity preserved). Demotion only ever runs on the owner — as a
piggyback on your own store calls (sync), or as an owner-loop task (`aopen`). Timeout-only in
v0.1; an RSS-quota variant is deferred (psutil stays out of core), and a hard per-store memory
cap does not exist. For analytics-style scans, the honest answer is the
[Arrow mirror tier](analytics.md), not the object graph.

## Recipe: parallel ingest (parse in a pool → single writer)

Bulk-importing many files (the classic shape: dozens of XML/CSV files into millions of records)?
The right way under owner confinement ([ADR-001](../design/ADR-001-concurrency-contract.md)) is to
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
(see [Open a store](../reference.md#open-a-store)), `@dc.entity(frozen=True)` records (cheapest to
commit — [Frozen entities](../reference.md#frozen-entities)), fewer `dc.Index`/`dc.Unique` fields,
and bigger commit batches. The scale model stays **1 writer + N readers**; write-sharding is out of
core — [SCALING.md](../design/SCALING.md).

Deliberately no `store.writer()` / `bulk_load()` / `parallel_map()` helper: a batching write-sink
would reintroduce the session object the design rejects ("no session, no `save()` — mutate, then
`commit()`"), and a process-pool wrapper is not a database concern. The primitives already suffice;
this is a discoverability recipe, not a missing feature.
