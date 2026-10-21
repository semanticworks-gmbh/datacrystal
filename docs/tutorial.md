# Tutorial: your first mineral cabinet

After this page you can define a model, store it, reopen it and query it.

Save this as `cabinet.py` and run `python cabinet.py` twice.

```python
from typing import Annotated
import datacrystal as dc

@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None
    type_locality: dc.Lazy[Locality] | None = None

store = dc.Store.open("cabinet.store")

if store.root is None:
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.root = {"minerals": [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"),
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic",
                type_locality=dc.Lazy.of(tsumeb)),
    ]}
    store.commit()
    print("first run: added 2 minerals")
else:
    print("reopened: the data is already here")

hits = store.query(Mineral.crystal_system == "monoclinic")
print("monoclinic:", sorted(m.name for m in hits))

azurite = store.get(Mineral, qid="Q193563")
print("first described at:", azurite.type_locality.get().name)

store.close()
```

The first run prints:

```
first run: added 2 minerals
monoclinic: ['azurite']
first described at: Tsumeb Mine
```

The second run prints `reopened: the data is already here` instead of the first line, and the other two lines stay the same.

## What the example shows

- Install with `uv add "datacrystal @ git+https://github.com/semanticworks-gmbh/datacrystal@v0.10.0"`; it needs Python 3.14 and no database server, because a store is a directory.
- `@dc.entity` makes a typed class persistable; you still construct it like a dataclass.
- `Annotated[str, dc.Unique]` makes `qid` a unique key that `store.get(Mineral, qid=...)` looks up.
- `Annotated[..., dc.Index]` makes `crystal_system` queryable from a bitmap index instead of a scan.
- `store.root` is the entry point into the graph and is `None` until you assign it, so `if store.root is None` means "first run".
- `store.commit()` writes everything you changed in one atomic transaction; there is no session and no `save()`.
- `dc.Lazy[Locality]` loads its target on `.get()`, so a large graph stays out of memory until you walk to it.

## Next

- [How-to guides](GUIDE.md#how-to-guides) for specific jobs.
- [Reference](reference.md) for every method and guarantee.
- [Explanation](explanation.md) for identity, memory and the query planner.
