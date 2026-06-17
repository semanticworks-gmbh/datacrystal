# How-to: analytics with Arrow mirrors (datacrystal[arrow])

Goal: run columnar projection/range analytics and aggregates over millions of records without
paying for live-object hydration. The `datacrystal[arrow]` mirror is a commit-delta consumer (see
[the commit-delta pipeline](../reference.md#the-commit-delta-pipeline)) and the columnar tier
datacrystal hands off to DuckDB. The reason the live engine has *no* fast aggregate path — the
rule-based planner never grows an optimizer — is in
[the query-semantics explanation](../explanation.md#query-semantics-the-planner-the-residual-and-the-candidate-set);
`MirrorConfigError` is in [the Errors reference](../reference.md#errors).

```python
pip install 'datacrystal[arrow]'     # adds pyarrow
```

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
  `store.get_many()`, or use the OID as the handoff key for the analytics recipe below.
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

## Analytics at scale: filter here, aggregate in DuckDB

Aggregates over a filtered set — `sum`/`avg`/`min`/`max`, `GROUP BY` — have no fast path in
the live object layer **on purpose**: the engine is rule-based and never grows an optimizer
(see [explain()'s two rules](../reference.md#reading-api)). `pluck` reads a column
without building entities, but you still pay O(hits) Python to fold it — summing 1.4 M values took
~5.6 s on the MaStR eval. The mirror is the columnar tier: hand its parquet to **DuckDB** and the
same fold is a vectorized scan. Two shapes, both at the mirror's `watermark`:

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
