# How-to: run analytics on Arrow mirrors

After this page you can aggregate a store's records in DuckDB without hydrating entities.

```bash
pip install 'datacrystal[arrow]' duckdb
```

```python
from typing import Annotated

import duckdb
import datacrystal as dc
from datacrystal.arrow import ArrowMirror

@dc.entity
class Specimen:
    specimen_no: Annotated[str, dc.Unique]
    quality: Annotated[str, dc.Index]
    mass_g: float

store = dc.Store.open("cabinet.store")
mirror = ArrowMirror("cabinet.mirror")
store.attach(mirror)
if store.root is None:
    store.root = {"seeded": True}
    for number, (quality, mass) in enumerate(
        [("museum", 310.0), ("museum", 222.5), ("fine", 120.0), ("fine", 150.0), ("fine", 96.0), ("cabinet", 58.0)]
    ):
        store.store(Specimen(specimen_no=f"DC-{number}", quality=quality, mass_g=mass))
    store.commit()

specimens = mirror.table(Specimen)
print(duckdb.query(
    "SELECT quality, count(*) AS n, sum(mass_g) AS total FROM specimens GROUP BY quality ORDER BY quality"
).fetchall())

F = dc.fields(Specimen)
with store.snapshot() as snap:
    fine_oids = [view.oid for view in snap.query(F.quality == "fine")]
print(duckdb.execute(
    f"SELECT sum(mass_g) FROM specimens WHERE {ArrowMirror.OID_COLUMN} IN (SELECT * FROM UNNEST(?))",
    [fine_oids],
).fetchone())

mirror.compact()
parquet_files = str(mirror.parquet_dir(Specimen) / "*.parquet")
print(duckdb.execute(f"SELECT max(mass_g) FROM read_parquet('{parquet_files}')").fetchone())
store.close()
```

- The live engine has no aggregate path on purpose: `pluck` avoids building entities but still folds in Python, and the rule-based planner never grows an optimizer ([why](../explanation.md#query-semantics-the-planner-the-residual-and-the-candidate-set)).
  The mirror hands the same data to DuckDB as a vectorized scan, zero-copy; `polars.from_arrow(table)` and `table.to_pandas()` read it too.
- Use DuckDB alone when the filter is a plain columnar predicate (first query).
  Filter in datacrystal and aggregate in DuckDB when the filter wants a bitmap index, `incoming()` or a graph walk (second query): take the OIDs from a `store.snapshot()` and join on `ArrowMirror.OID_COLUMN`.
  The snapshot's `tid` equals the mirror's `watermark`.
  For a very large OID set, register it as its own table and `JOIN` it.
- Rows carry `__oid__` (int64 primary key) plus every persisted field.
  Types are inferred and promoted through a total lattice (`bool < int < float`, lists element-wise), and anything mixed becomes msgpack binary that `datacrystal.arrow.decode_fallback()` restores, so additive schema evolution cannot wedge the mirror.
  Entity references are int64 OID columns; join them or pass them to `store.get_many()`.
- The mirror is an LSM of parquet segments with `manifest.json` as the atomic, fsync-ordered commit point.
  A reopen resumes at the durable watermark and a crash mid-flush is swept on open.
- `mirror.table()` runs on the store's owner thread and folds the segments; the returned Arrow table can go to DuckDB anywhere.
  `mirror.compact()` collapses each type to one plain parquet file with tombstones dropped, so `parquet_dir()` is then readable by DuckDB or Spark directly.
  Without `compact()` a directory may hold several segments that still need newest-wins folding.
- `only=[Specimen, ...]` mirrors a subset.
  `flush_every=N` batches flushes, so the durable watermark trails by up to N−1 commits and a crash in that window costs a rebuild.
- To attach to a store that already has data, use `ArrowMirror.bootstrap(path, snapshot, batch=N)`.
  It streams the extent in chunks (default 50 000), so peak memory is O(batch) and a store larger than RAM can be mirrored, and a crash mid-bootstrap forces a clean re-bootstrap.
- A mirror directory has one owner process, like the store file.
- A mirror of protected data is protected data, as plaintext on disk ([permissions](permissions.md#what-the-fence-does-and-does-not-cover)).
- Joins across mirrors and parquet on S3 are `[planned — v1, items 7/16]`.

Reference: [The commit-delta pipeline](../reference.md#the-commit-delta-pipeline), [Errors](../reference.md#errors).
