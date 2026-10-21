# How-to: evolve your schema

After this page you can change entity classes between runs and keep reading the old records.

```python
from typing import Annotated

import datacrystal as dc

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    hardness: float | None = None
    coords: str = "0,0"

with dc.Store.open("cabinet.store") as store:
    if store.root is None:
        store.root = {"seeded": True}
        store.store(Mineral(qid="Q193563", name="azurite", hardness=3.5, coords="-19.2,17.7"))
        store.commit()

@dc.entity
class Mineral:  # noqa: F811 - the same class, one release later
    qid: Annotated[str, dc.Unique]
    name: str
    mohs: Annotated[float | None, dc.RenamedFrom("hardness")] = None
    lat: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[0]))] = 0.0
    lon: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[1]))] = 0.0
    color: str = "unknown"

with dc.Store.open("cabinet.store") as store:
    azurite = store.get(Mineral, qid="Q193563")
    print(azurite.mohs, azurite.lat, azurite.lon, azurite.color)
    print(store.verify())
    print(store.migrate(), store.migrate())
```

The script prints `1 0` on its first run and `0 0` on its second, because `migrate()` rewrites each old record once.

| Change | What happens |
|---|---|
| add a field with a default | old records get the default |
| remove a field | old values are ignored |
| reorder fields | values map by name |
| add a field without a default | `SchemaMismatchError` naming the field |
| add a `dc.Unique` field | must default to `None`, otherwise `SchemaMismatchError` |
| rename a field | `Annotated[T, dc.RenamedFrom("old")]` binds the old values |
| split, merge or derive a field | `Annotated[T, dc.Glue(fn)]` computes it from the old record as a read-only `{name: value}` mapping |
| change a field's type | not checked on load; convert with `dc.Glue` |

- `RenamedFrom` and `Glue` fire only when the field is absent from a record's persisted shape, and never rewrite the record.
- `store.migrate()` re-encodes every record stored under an older shape to the newest one, so a derived field becomes a real column.
  It is idempotent (the second call returns 0), crash-safe and commits in `batch=10_000` chunks.
- `store.verify()` is the read-only pre-flight: it returns the `(typename, oid)` pairs that do not decode against the current code, and an empty list means the store reads cleanly.
  Run it before `migrate()`.
- `Glue` and `RenamedFrom` are read-time markers and raise at `@entity` on a `dc.Index` or `dc.Unique` field, because an index is built from the persisted value and would index the wrong data.
  To index a derived field, keep it non-indexed with `Glue`, run `migrate()`, then redeclare it as `Annotated[float, dc.Index] = 0.0` without `Glue`.
  The same order applies to a renamed field you want indexed.
- The markers cover non-indexed fields on the `get`, `query` and `pluck` paths; the index, snapshot and Arrow decode paths and renaming an indexed field are `[planned — demand-driven]`.
- The store keeps a type lineage, one row per field shape a class ever had, and decodes each record through the shape it was written with.
  A store that used schema evolution still opens in this and any newer library version.

Reference: [Define entities](../reference.md#define-entities), [Errors](../reference.md#errors).
