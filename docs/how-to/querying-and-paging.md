# How-to: query and page results

After this page you can window a result set, sort it, take the top N by an indexed field, stream a large match set and find the entities that reference one.

```python
import datacrystal as dc
from typing import Annotated

@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str, dc.Index]
    mohs: Annotated[float | None, dc.SortedIndex] = None
    type_locality: dc.Lazy[Locality] | None = None

store = dc.Store.open("cabinet.store")
if store.root is None:
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.root = {"seeded": True}
    store.store(tsumeb)
    for number, (name, system, mohs) in enumerate(
        [("quartz", "trigonal", 7.0), ("azurite", "monoclinic", 3.5), ("malachite", "monoclinic", 3.8),
         ("diamond", "cubic", 10.0), ("pyrite", "cubic", 6.3), ("fluorite", "cubic", 4.0)]
    ):
        store.store(Mineral(qid=f"Q{number}", name=name, crystal_system=system, mohs=mohs,
                            type_locality=dc.Lazy.of(tsumeb)))
    store.commit()

first_two = store.query(Mineral, limit=2)
next_two = store.query(Mineral, limit=2, offset=2)
hardest = store.query(Mineral, order_by=(Mineral.mohs, "desc"), limit=3)
names = store.pluck(Mineral, "name", order_by=Mineral.name)
print([m.name for m in first_two], [m.name for m in next_two])
print([m.name for m in hardest])
print(names)

for mineral in store.query_iter(Mineral.crystal_system == "cubic"):
    print("cubic:", mineral.name)

tsumeb = store.get(Locality, qid="Q571997")
print(sorted(m.name for m in store.incoming(tsumeb)))
store.close()
```

- `limit=` and `offset=` window the result in ascending OID order, so `query(C, limit=k) == query(C)[:k]`.
- On a fully indexed query the window is applied to the candidate OIDs before hydration, so `limit=10` loads 10 records.
  A residual predicate must decode every candidate first, so there the window only trims the result.
- `order_by=(field, "asc" | "desc")` sorts the whole match set before the window; a bare field means ascending.
  NULLs sort last in both directions and ties break on ascending OID, so paging is stable.
- Mark a field `dc.SortedIndex` if you sort or page by it often; an unindexed sort field is decoded for every match.
- For "the N newest", use `order_by=(field, "desc"), limit=N` on a `SortedIndex` field rather than a `>= sentinel` predicate.
  Both return the same rows, but the sentinel form considers every match as a candidate, so its cost grows with the match count while the `order_by` form costs O(limit).
  `store.explain(...)` shows `candidates: K of extent` for the difference.
- `query_iter` yields matches chunk by chunk, so peak memory is O(chunk); see [ingest and memory](ingest-and-memory.md).
- `store.incoming(entity)` returns every committed entity that references `entity`, through eager and lazy references, in scalar fields and in list or dict containers.
  The first call builds the reverse-reference index with one scan; later calls are O(1) lookups, and a store that never calls it pays nothing ([ADR-005](../design/ADR-005-index-cache.md)).
- After a delete, `incoming(dead)` lists the entities now dangling at the dead OID ([ADR-003](../design/ADR-003-delete-semantics.md)).
  Checked delete (refuse if referenced, cascade) is `[planned — v1]`.
- The same backlinks at a pinned watermark are `snap.incoming(...)`; see [snapshots](snapshots-and-delta-log.md).

Reference: [Reading API](../reference.md#reading-api).
Why the planner is rule-based: [query semantics](../explanation.md#query-semantics-the-planner-the-residual-and-the-candidate-set).
