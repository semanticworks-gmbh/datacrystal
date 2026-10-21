# How-to: ingest big data and keep memory bounded

After this page you can import a dataset larger than RAM and keep peak memory under your control.

```python
import multiprocessing as mp
from typing import Annotated

import datacrystal as dc

@dc.entity
class Specimen:
    specimen_no: Annotated[str, dc.Unique]
    quality: Annotated[str, dc.Index]
    mass_g: float

def parse_batch(batch_no: int) -> list[dict]:
    qualities = ["museum", "fine", "cabinet", "thumbnail"]
    return [{"specimen_no": f"DC-{batch_no}-{i}", "quality": qualities[i % 4], "mass_g": float(i)}
            for i in range(250)]

if __name__ == "__main__":
    with dc.Store.open("cabinet.store", lazy_timeout=60.0) as store:
        if store.root is None:
            store.root = {"seeded": True}
            with mp.Pool(2) as pool:
                for rows in pool.imap_unordered(parse_batch, range(4)):
                    for row in rows:
                        store.store(Specimen(**row))
                    store.commit()
        museum = store.count(Specimen.quality == "museum")
        masses = sum(store.pluck(Specimen, "mass_g", limit=100))
        exported = sum(1 for _ in store.query_iter(Specimen.quality == "museum"))
        print(museum, masses, exported)
```

- Workers return plain data and only the owner process builds `@entity` instances and writes.
  An entity crossing a process boundary pickles as an unstamped copy that `store.store()` registers under a fresh OID, so building entities in workers silently breaks identity instead of raising.
- Parallelism speeds up parsing, not writing, because the single writer is the serial part.
  To speed up the write side, use `durability="never"` for a one-shot load ([Open a store](../reference.md#open-a-store)), `@dc.entity(frozen=True)` records, fewer `Index` and `Unique` fields and larger commit batches.
- Entities that are not reachable from `store.root` are garbage-collected once you drop them, and come back through `get()` or `query()`.
  `store.store(obj)` registers such a free-floating entity, so memory is bounded by what you hold plus your query results.
- Everything eagerly reachable from the root is pinned; a `dc.Lazy` edge stops both the pinning and the loading.
- `==`, `.in_()`, `.contains()` and `.startswith()` on `dc.Index` fields answer from bitmaps and hydrate only the hits; `count()` on them hydrates nothing.
- `count()` and `pluck()` decode records without building entities, so even a full scan costs decode time and no entity memory.
- A residual predicate in `query()` (`>=` on an unsorted field, `!=`) hydrates the whole extent.
  Design hot filters as `dc.Index` equality facets, and use the [Arrow mirror](analytics.md) for columnar speed.
- `query_iter(target)` yields chunk by chunk, so peak memory is O(chunk); `limit=` and `offset=` on `query()` and `pluck()` load only the slice for a fully indexed read.
- `lazy_timeout=<seconds>` demotes loaded `Lazy` handles idle for that long back to unloaded; the next `.get()` reloads them with the same identity.
  There is no hard per-store memory cap and no RSS-based demotion.
- `tests/fitness/test_memory_bounded.py` gates the peak-RSS budget, collectability of results and that `get()` hydrates exactly one object.

Reference: [Concurrency primitives](../reference.md#concurrency-primitives), [Reading API](../reference.md#reading-api).
Why identity and memory are one question: [explanation](../explanation.md#identity-and-memory).
The concurrency contract behind owner-only writes: [ADR-001](../design/ADR-001-concurrency-contract.md).
