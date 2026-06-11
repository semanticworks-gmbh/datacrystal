# Finding: columnar-analytics

_Feasibility study, 2026-06-10. One of 10 parallel investigations (5 reading the EclipseStore source in `resources/store`, 5 researching the Python ecosystem)._

## Summary

The proposed pattern (object graph = source of truth, incremental Arrow columnar mirrors per homogeneous collection, DuckDB/polars query, OID column to swizzle hits back to live objects) is validated by both the lakehouse literature and multiple prior systems — including EclipseStore's own GigaMap, which exists precisely because iterating a heap object graph is too slow and instead maintains off-heap bitmap indexes with lazy object loading. The critical negative result: DuckDB cannot scan arbitrary Python objects fast — pandas object-dtype columns go through a sampled analyze phase and per-value conversion under the GIL — so a typed Arrow projection is mandatory, not optional. Arrow immutability is handled the way Delta Lake and Lance handle it: append-only delta batches (zero-copy concat as new chunks), RoaringBitmap-style deletion vectors as tombstones (merge-on-read), and periodic compaction that rewrites and re-bases; write amplification is O(changed rows) per commit plus amortized compaction. DuckDB scans Arrow zero-copy with projection/filter pushdown (11x–2900x over pandas in DuckDB's own benchmarks) and since 1.1 consumes any __arrow_c_stream__ PyCapsule object, so polars and DuckDB can share the same mirror; use DuckDB as the SQL frontend and polars for expression-style pipelines. Lance is a strong candidate for the durable tier (versioned manifests, per-fragment deletion files, merge_insert upsert, IVF_PQ vector index), but its _rowid is a physical row address and its "stable row ids" are experimental and not stable across updates — so identity must live in an explicit OID column, never in the format's row id.

## Key findings

### pyarrow immutability: chunked append + tombstone bitmap + compaction is the canonical workaround

pa.Table/RecordBatch and schemas are immutable; pa.concat_tables is zero-copy (the result's ChunkedArrays just reference existing buffers), so incremental ingestion = accumulate new RecordBatches as extra chunks. Table.filter/take materialize copies, so deletes should NOT rewrite — keep a boolean/roaring tombstone bitmap aligned to row positions and apply it as a WHERE predicate at query time. ChunkedArray.combine_chunks()/concat + rebuild is the compaction primitive; trigger it on chunk-count or tombstone-ratio thresholds because scan overhead grows with tiny chunks. Sources: arrow.apache.org/docs/python/data.html, pyarrow.Table/ChunkedArray API docs.

### Delta Lake deletion vectors quantify the write-amplification win of merge-on-read

Delta's deletion vectors are RoaringBitmap files (~43 bytes for a single tombstone); deleting one row under copy-on-write rewrote all 10 touched files, under DVs it writes one tiny DV file. Explicit tradeoff from the Delta blog: 'Whatever time the DELETE operation saves... the reader and compaction commands pay for later' — readers merge bitmaps at scan time and REORG/OPTIMIZE eventually rewrites files (recommended target 100MB–1GB). Cautionary note for Python: delta-rs (the deltalake pip package) still does not support deletion vectors, which is a reason NOT to pick Delta as pyrsistance's columnar layer. Sources: delta.io/blog/2023-07-05-deletion-vectors/, delta-io.github.io/delta-rs best-practices and small-file-compaction docs.

### Lance: versioned manifests + per-fragment deletion files; row ids are NOT a safe identity

Every Lance mutation (add/update/delete/merge_insert/add_columns) writes new files and a new immutable manifest version; deletes write per-fragment deletion files (_deletions/{fragment_id}-{read_version}.arrow) that are loaded and applied during scans. _rowid is physically fragment_id(32b)<<32|offset — a row ADDRESS that changes on compaction; the experimental 'move-stable row ids' (lance issue #2307) survive compaction but still change on UPDATE. Versions accumulate metadata overhead (100 versions = 100x manifest metadata, slower queries) and compaction temporarily doubles disk until old versions are cleaned. Consequence: pyrsistance's OID must be an explicit uint64 column; never reuse the storage format's row id as object identity. Sources: lancedb.com/documentation/concepts/data.html, github.com/lancedb/lance issues #2307/#1378/discussion #3694.

### DuckDB over Arrow: zero-copy with real pushdown; the numbers justify the mirror

DuckDB streams Arrow data zero-copy in both directions and 'can push down filters and projections directly into Arrow scans'; it queries Arrow Tables, Datasets, RecordBatchReaders and Scanners via replacement scans (local variable name = table name). DuckDB's own benchmarks vs pandas: 11x (projection pushdown), 57x (filter pushdown), ~2900x and 0.3GB-vs-248GB on the streamed NYC-taxi case. Since DuckDB 1.1 it consumes any object exposing __arrow_c_stream__ (Arrow PyCapsule interface), so the same in-memory mirror serves DuckDB SQL and polars without pyarrow being a hard boundary. Sources: duckdb.org/2021/12/03/duck-arrow, duckdb.org/docs/current/guides/python/sql_on_arrow, github.com/duckdb/duckdb discussion #10716.

### DuckDB over raw Python objects is the trap the design must avoid

DuckDB cannot scan lists of arbitrary Python objects; it scans pandas/polars/numpy/Arrow only. pandas object-dtype columns trigger an 'analyze phase' that samples 1000 rows (pandas_analyze_sample) to guess a type, then converts value-by-value — non-strings get stringified — while holding the GIL (duckdb/duckdb issue #2450, Python data_ingestion docs). This is the empirical proof that 'just query the objects' does not work in Python and that the mirror must consist of natively typed Arrow columns extracted once per commit, not per query. Sources: duckdb.org/docs/current/clients/python/data_ingestion, github.com/duckdb/duckdb/issues/2450.

### polars vs DuckDB: not either/or; DuckDB as SQL frontend, polars as expression engine, same Arrow mirror

Consensus across 2025/2026 comparisons (kestra.io embedded-databases, codecentric benchmark, codecut.ai): DuckDB wins for SQL-first, join/aggregation-heavy, larger-than-memory embedded analytics; polars wins for chained typed transformations with lazy optimization and a Python-native expression API; both are embedded, interoperate zero-copy via Arrow, and 'the integration cost is mostly mental, not infrastructural'. For pyrsistance: register each projection in a DuckDB connection (gives users SQL + the GraphQL/FastAPI pushdown target) and additionally expose pl.from_arrow/LazyFrame views for programmatic pipelines — one mirror, two query surfaces.

### Prior art #1 — EclipseStore GigaMap is the same problem solved with bitmap indexes instead of columns

Local docs (/Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/index.adoc) describe GigaMap as 'off-heap bitmap indexing to facilitate lightning-fast searches and lazy loading of objects' — point-lookup/predicate indexes (BitmapIndex.java, BitmapIndices.java in gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/) that avoid scanning the heap graph and avoid GC pressure by going off-heap. GigaMap accelerates selective queries but is NOT an analytical scan engine (no aggregations/group-by over columns); the Arrow-mirror pattern is strictly more capable for analytics and is the natural Python translation, since Python's equivalent of 'off-heap' IS Arrow/numpy buffers.

### Prior art #2 — Spark Dataset Encoders, Realm, Apache Ignite, ZODB catalogs: the object<->columnar mirror exists in many forms

Spark Encoders convert typed JVM objects to/from Tungsten's binary columnar InternalRow so 'many operations can be done in-place, without needing to materialize an object at all' — the closest precedent for query-on-encoded-form, decode-objects-on-demand (databricks.com Datasets intro). Realm's core engine stores object properties column-wise inside B+tree Cluster leaves — a production object DB with a columnar physical layout (dbdb.io/db/realm, academy.realm.io). Apache Ignite keeps binary objects in cache plus B+tree SQL indexes over annotated fields, query-then-get-by-key (ignite.apache.org/features/sql.html). In Python, ZODB + repoze.catalog/ZCatalog/souper is the direct ancestor: derived indexes over a native object graph, with the known failure mode that the app must remember to reindex — i.e., consistency by convention, which pyrsistance should replace with automatic maintenance at the commit boundary.

### Write amplification and consistency model: per-commit deltas, snapshot-swap, mirror is rebuildable

Cost model converged on by Delta and Lance and directly portable: insert = buffer rows, flush one RecordBatch per commit (O(changed rows), ~zero copy of existing data); delete = set bit in roaring bitmap (O(1), bytes); update = tombstone + re-append (merge-on-read); compaction = O(live rows) amortized, threshold-driven (chunk count, tombstone ratio — Lance explicitly recommends regular compaction when single-row inserts are unavoidable). Consistency: maintain the mirror synchronously inside the same commit that persists dirty objects (the EclipseStore Storer analog); because Arrow is immutable, publishing is an atomic swap of an immutable (batch_list, tombstone_bitmap, oid->position index) snapshot — readers get snapshot isolation for free, single-writer keeps it trivial. Since the mirror is purely derived data, full rebuild-from-graph is always a valid recovery path, which keeps correctness obligations low. For derived aggregates beyond per-class projections, DBSP/Feldera (VLDB'23) is the principled incremental-view-maintenance reference, but is overkill for v1.

### Should Lance BE the columnar layer? Yes for the durable/vector tier, no for the hot in-memory tier

Pros: one dependency gives versioned columnar persistence, time travel, merge_insert upsert keyed on OID, IVF_PQ vector indexes (FAISS-class ANN with filtering), fast random access by row, and zero-copy readability from DuckDB/polars/pyarrow — covering pyrsistance's vector-search requirement without building ANN. Cons: every write is a filesystem transaction (latency vs pure in-memory appends), manifest/version accumulation requires cleanup_old_versions, row ids unstable across updates, format still evolving. Recommended architecture: hot tier = in-process Arrow RecordBatches + roaring tombstones registered in DuckDB (microsecond appends, no disk), cold/durable tier = one Lance dataset per projection flushed asynchronously, OID as explicit uint64 key column joining both tiers back to live objects via a weakref OID->object registry.

## Implications for the Python port

Replicate: (1) GigaMap's role, but as Arrow: a per-class `ColumnarProjection(spec)` registered against the store — spec lists field extractors (compiled attrgetters / dataclass-field readers, pydantic-aware) plus a mandatory uint64 `__oid__` column; maintained automatically inside the commit pipeline, never by user reindex calls (learn from ZODB's failure mode). (2) Merge-on-read mechanics from Delta/Lance: per-projection state = immutable list of RecordBatches + roaring tombstone bitmap + oid->(batch,offset) dict; insert appends to a small Python-side row buffer flushed to a RecordBatch at commit; delete/update flips bits; queries run over `pa.Table.from_batches(...)` registered in DuckDB with `WHERE NOT deleted` injected (or a precomputed boolean column), hits return OIDs that swizzle to live objects through the identity map the persistence engine already needs. (3) Compaction as a background/threshold task (chunk count > N or tombstones > 20%): combine chunks, drop dead rows, rebuild the oid index — O(live rows), amortized. Simplify: skip deletion-vector file formats, multi-writer commit protocols, and DBSP-style IVM — single-writer + atomic snapshot swap of immutable state gives snapshot isolation trivially, and "mirror is derived, rebuildable from the graph" is the correctness escape hatch. Do differently from EclipseStore: don't build bespoke off-heap bitmap indexes — Arrow buffers + DuckDB's pushdown ARE the off-heap layer in Python, and they additionally buy aggregations/joins/window functions that GigaMap can't do. Hard constraints discovered: never expose object-dtype columns to DuckDB (GIL-bound stringification); never use Lance/Delta row ids as identity (use the OID column; Lance stable-row-ids are experimental and change on update); pick DuckDB as the primary query surface (SQL + future GraphQL/FastAPI pushdown) with polars LazyFrame as a secondary zero-copy view via the Arrow PyCapsule (__arrow_c_stream__) interface, which DuckDB supports since 1.1. For vectors, don't hand-roll ANN: use Lance datasets (IVF_PQ + filter pushdown) as the durable tier per projection, flushed asynchronously via merge_insert on OID — that one dependency also provides versioning/time-travel for restart-fast scans.

## Sources

- https://duckdb.org/2021/12/03/duck-arrow
- https://duckdb.org/docs/current/guides/python/sql_on_arrow
- https://duckdb.org/docs/current/clients/python/data_ingestion
- https://github.com/duckdb/duckdb/issues/2450
- https://github.com/duckdb/duckdb/discussions/10716
- https://delta.io/blog/2023-07-05-deletion-vectors/
- https://delta-io.github.io/delta-rs/usage/optimize/small-file-compaction-with-optimize/
- https://delta-io.github.io/delta-rs/delta-lake-best-practices/
- https://lancedb.com/documentation/concepts/data.html
- https://github.com/lancedb/lance/issues/2307
- https://github.com/lancedb/lance/issues/1378
- https://github.com/lancedb/lance/discussions/3694
- https://github.com/lance-format/lance
- https://arrow.apache.org/docs/python/data.html
- https://arrow.apache.org/docs/python/generated/pyarrow.Table.html
- https://arrow.apache.org/docs/format/CDataInterface/PyCapsuleInterface.html
- https://kestra.io/blogs/embedded-databases
- https://www.codecentric.de/en/knowledge-hub/blog/duckdb-vs-dataframe-libraries
- https://codecut.ai/pandas-vs-polars-vs-duckdb-comparison/
- https://www.databricks.com/blog/2016/01/04/introducing-apache-spark-datasets.html
- https://jaceklaskowski.gitbooks.io/mastering-spark-sql/spark-sql-Encoder.html
- https://dbdb.io/db/realm
- https://academy.realm.io/posts/jp-simard-realm-core-database-engine/
- https://ignite.apache.org/features/sql.html
- https://github.com/repoze/repoze.catalog/blob/master/docs/overview.rst
- https://github.com/bluedynamics/souper
- https://github.com/feldera/feldera
- https://docs.feldera.com/assets/files/vldb23-1bfe30b29f95168c8e1f427fccfc6da2.pdf
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/index.adoc
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/BitmapIndex.java
