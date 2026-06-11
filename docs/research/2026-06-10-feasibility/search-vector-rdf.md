# Finding: search-vector-rdf

_Feasibility study, 2026-06-10. One of 10 parallel investigations (5 reading the EclipseStore source in `resources/store`, 5 researching the Python ecosystem)._

## Summary

Evaluated 14 embeddable in-process engines across full-text, vector, and RDF categories for pyrsistance's baked-in search. Recommended defaults: SQLite FTS5 (full-text; the only candidate with truly transactional per-row incremental updates and zero native dependencies, with tantivy-py as the high-performance opt-in), usearch (vector; active through May 2026, Apache-2.0, single-file save/load/mmap persistence, incremental add/remove, resolved 3.13t support), and pyoxigraph (RDF/SPARQL; clear winner — 0.5.8 Apr 2026, MIT/Apache dual, RocksDB on-disk store with snapshot isolation, cp313t/cp314t free-threaded wheels, plus oxrdflib bridging into rdflib). Hard disqualifications: DuckDB FTS (no incremental updates, full PRAGMA rebuild required), DuckDB VSS (experimental persistence, WAL-recovery corruption risk, full index rewrite per checkpoint), Kuzu (archived/abandoned Oct 2025), CozoDB (dead since Dec 2023), whoosh-reloaded (last release 2024, pure-Python, slow), hnswlib (dormant since Dec 2023, sdist-only). Recommended commit-pipeline pattern: indexes are rebuildable derived data updated via a commit-log-driven outbox with per-index last-applied-commit watermarks — synchronous single-transaction apply for the SQLite sidecar, async replayable apply for usearch/pyoxigraph.

## Key findings

### FULL-TEXT default: SQLite FTS5 — only engine with transactional, per-row incremental index updates

FTS5 processes INSERT/UPDATE/DELETE incrementally inside SQLite transactions (delete-keys merged later via automerge/crisismerge/optimize/merge commands), supports contentless (content='', contentless_delete=1) and external-content tables so we never duplicate object payloads, BM25 ranking, unicode61 + trigram tokenizers (substring/LIKE acceleration). Ships in CPython stdlib via sqlite3 — zero added dependency — though FTS5 availability depends on how libsqlite3 was compiled (python.org Windows/macOS installers have it; some Linux distro builds may not), so plan a fallback to apsw or pysqlite3-binary which bundle a full-featured SQLite; apsw additionally allows registering custom FTS5 tokenizers from Python. Index lives in a sidecar .db file colocated in the store directory. Source: sqlite.org/fts5.html, docs.python.org/3/library/sqlite3.html, charlesleifer.com blog on JSON1/FTS5.

### FULL-TEXT runner-up: tantivy-py — fastest and actively maintained, but commit model favors batching over per-transaction updates

MIT license, latest 0.26.0 released 2026-04-29 (quickwit-oss), wheels for 3.10-3.13 plus free-threaded 3.13t/3.14t support added in 0.25.1. Persists to its own index directory (tantivy.Index(schema, path=...)) — perfect colocation. Incremental: writer.add_document / writer.delete_documents(field_name, value) (delete-by-term; known gotcha on tokenized fields, issue #297) / writer.commit() + wait_merging_threads(); index.reload() for NRT visibility. Each commit creates a new segment, so committing on every small store transaction causes segment churn — needs debounced group-commit (async post-commit), meaning search lags writes slightly. Single IndexWriter at a time. Sources: github.com/quickwit-oss/tantivy-py/releases, tantivy-py.readthedocs.io, issue #297.

### FULL-TEXT disqualified: DuckDB FTS and whoosh-reloaded

DuckDB FTS index does NOT update on table changes — official docs require PRAGMA drop_fts_index + create_fts_index (or overwrite:=1) full rebuild; maintainers say it's 'a bunch of SQL tables' designed for static OLAP data (duckdb.org/docs/current/core_extensions/full_text_search, github.com/duckdb/duckdb/issues/3543, discussion #15291). whoosh-reloaded (Sygil-Dev fork, BSD) last released 2.7.5 in early 2024, pure Python (slow for an engine baked into a persistence layer), low activity — not a serious 2026 default.

### VECTOR default: usearch — active, Apache-2.0, file-based persistence, true incremental add/remove, 3.13t resolved

unum-cloud/USearch v2.25.3 (2026-05-24), 206 releases, continuous development; Apache-2.0. Persists a single index file (save/load) and can serve via memory-mapped view() without loading into RAM — ideal for colocating one .usearch file per indexed type in the store directory. Supports per-vector add/search/remove (incremental), user-defined metrics, f16/i8 quantization. Python 3.13 cp313 wheels shipped (v2.25.2+); free-threaded 3.13t support tracked in issue #560, closed via PR #614 (opened Jan 2025). Risk: single-vendor (Unum) bus factor — mitigated by treating the index as rebuildable derived data. Sources: github.com/unum-cloud/usearch, pypi.org/project/usearch, issues #530/#560.

### VECTOR alternatives: sqlite-vec (transactional but brute-force, ANN still alpha), lancedb (heavyweight second engine), hnswlib/faiss (lagging)

sqlite-vec (asg017, MIT/Apache): vec0 virtual tables give transactional INSERT/UPDATE/DELETE inside the same SQLite sidecar as FTS5 — the most elegant consistency story — plus metadata/partition/aux columns since 0.1.6; but search is exact brute-force; ANN (IVF/DiskANN) only landed in 0.1.10-alpha series (2026), pre-1.0, single maintainer. Fine default for <~500K vectors; we recommend it as the documented 'unified sidecar' alternative. LanceDB (Apache-2.0, very active): embedded OSS mode persists Lance-format directories with MVCC zero-copy versioning; new rows are brute-force scanned until table.optimize() incrementally updates the index — good incremental story but it embeds an entire second columnar database engine and own storage layout inside our store. hnswlib: dormant — last release 0.8.0 Dec 2023, sdist-only (no cp313 wheels), though it does support add_items/mark_deleted and is picklable. faiss-cpu 1.13.2 (community faiss-wheels, Dec 2025) has cp310+ abi3 and cp313 wheels but no free-threaded wheels, awkward deletes (IDMap/remove_ids), conda-first official distribution. DuckDB VSS disqualified: HNSW persistence behind hnsw_enable_experimental_persistence flag, WAL recovery 'not properly implemented' (crash = index corruption/data loss), and every checkpoint rewrites the entire serialized index — official docs label it experimental.

### RDF default: pyoxigraph — actively developed, ACID RocksDB store, free-threaded wheels, full SPARQL 1.1 (+1.2 features)

pyoxigraph 0.5.8 (2026-04-28), dual MIT/Apache-2.0, wheels cp38-cp314 including cp313t/cp314t free-threaded. Two stores: in-memory Store() and disk-based Store(path=...) on RocksDB — a directory we can colocate inside the pyrsistance store dir; Store.read_only() for readers. Strong guarantees documented: no partial writes exposed, repeatable-read snapshot for the full duration of reads; extend(quads) is atomic (all-or-nothing), update(sparql) transactional, plus bulk_extend/bulk_load, backup(), optimize(), flush(). SPARQL 1.1 Query/Update complete; 0.5.3 (Dec 2025) added SPARQL 1.2 VERSION support. Sources: pypi.org/project/pyoxigraph, pyoxigraph.readthedocs.io/en/stable/store.html, oxigraph CHANGELOG (0.5.7 dated 2026-04-19).

### rdflib is the ecosystem API layer, and pyrsistance can be an rdflib Store backend — oxrdflib proves the pattern

rdflib is very active: 7.6.0 released 2026-02-13 (7.3/7.4/7.5 all in late 2025), BSD-3, with v8 in alpha. Its pluggable Store API is exactly the extension point for exposing OUR object graph as triples: oxrdflib (BSD-3, v0.5.0 Sep 2025, github.com/oxigraph/oxrdflib) implements an rdflib store named 'Oxigraph' via rdflib.Graph(store='Oxigraph') + graph.open(path), delegating SPARQL evaluation to pyoxigraph — note it documents 'does not yet support transactions'. Recommended architecture: pyoxigraph as the default triple index engine, rdflib+oxrdflib as the optional compatibility facade, and a pyrsistance-native rdflib Store later (rdflib stores aren't transactional anyway, so read-only projection of the object graph is the low-risk v1).

### RDF disqualified: Kuzu abandoned Oct 2025, CozoDB dead since Dec 2023

Kuzu (MIT, embedded property graph with vector+FTS — would have been attractive): Kùzu Inc. archived the GitHub repo around 2025-10-10 ('Kuzu is working on something new'), confirmed by The Register (2025-10-14) and HN; file format was still in flux (single-file format only arrived in final 0.11.0, July 2025); community forks (Kineviz 'bighorn') are immature. CozoDB (MPL-2.0, Datalog relational-graph-vector): last release v0.7.6 on 2023-12-11, issues from 2024-2025 unanswered; a github discussion (#299, Oct 2025) states 'Cozo has not been an active project for a long time'. Neither is safe to build a persistence library on.

### Commit pipeline: indexes as rebuildable derived data, commit-log outbox + per-index watermark; sync for SQLite sidecar, async replay for usearch/pyoxigraph

Since none of the engines can join pyrsistance's own storage transaction, do NOT attempt distributed transactions. Pattern: (1) during a store transaction, accumulate index deltas (upsert/delete keyed by stable object-id) alongside dirty objects; (2) persist deltas in the store's commit record (analog of EclipseStore's transaction log) — this is the write-ahead source for index replay; (3) post-commit, apply deltas to each index with an idempotent last_applied_commit_id watermark: SQLite sidecar (FTS5 + optionally sqlite-vec) applies synchronously in ONE sidecar transaction that also updates a watermark table — crash-safe and read-your-writes; usearch applies add/remove in-memory immediately, snapshots to file periodically (saves are whole-file, so save on checkpoint/close + atomic rename, watermark stored beside the file), replays the commit-log tail on open; pyoxigraph applies via atomic extend()/remove() with the watermark written as a quad in a meta named-graph in the same update. (4) Recovery/rebuild: on open, replay commit log from each watermark; full rebuild from the object graph is always possible. tantivy-py backend uses the same outbox but with debounced group commits (segment churn). DuckDB-style 'rebuild on read' is the anti-pattern this design avoids.

### Python 3.13+/free-threading compatibility matrix

Free-threaded (cp313t/cp314t) wheels TODAY: pyoxigraph (yes, on PyPI), tantivy-py (yes, since 0.25.1), usearch (3.13t support merged via PR #614). stdlib sqlite3: works on free-threaded builds; threadsafety is set from SQLite's compile-time threading mode (serialized mode = fully thread-safe; per-connection confinement is the safe pattern regardless). rdflib: pure Python, works everywhere. NOT free-threading-ready: faiss-cpu (abi3 cp310+ wheels, no t-wheels), hnswlib (no wheels at all since 2023), duckdb (GIL-build only as of research date), lancedb (no t-wheels yet). PyO3-based Rust bindings are the most future-proof for 3.13t — another argument for the tantivy/pyoxigraph/usearch family. Sources: py-free-threading.github.io/tracking, docs.python.org free-threading howto, respective PyPI pages.

## Implications for the Python port

Build the index subsystem as a pluggable 'derived data' layer over pyrsistance's commit pipeline, not as transactional participants. Concretely: (1) Default stack = stdlib sqlite3+FTS5 sidecar ('indexes.db' colocated in the store directory, contentless/external-content tables keyed by object-id, trigram tokenizer optional), usearch index files per vector field, pyoxigraph RocksDB directory for triples — all inside the store dir so backup/copy of the directory captures everything; every index carries a last_applied_commit_id watermark and can be dropped+rebuilt from the object graph (the EclipseStore analog: graph is the single source of truth, indexes are like GigaMap indices — derived and lazily rebuildable). (2) Replicate from EclipseStore the commit-record/transaction-log discipline: append index deltas into the commit record so index application is replayable and idempotent; do NOT replicate its Java-style eager index objects living inside the graph. (3) Simplify: skip tantivy/faiss/lancedb in v1 — offer them as optional extras behind a SearchBackend/VectorBackend protocol (entry-point registered, mirroring rdflib's plugin pattern); skip Kuzu/Cozo entirely (dead). (4) Do differently from Java: lean on SQLite's real transactionality for the FTS sidecar (Java EclipseStore has no such luxury — GigaMap indexes are custom), expose vectors as numpy arrays end-to-end (usearch/sqlite-vec both speak numpy), and ship an rdflib Store facade over the triple index (oxrdflib is the reference implementation, ~600 LOC) so pyrsistance objects are SPARQL-queryable through the standard Python RDF API. (5) Wheels/CI: target cp310-cp314 + cp313t/cp314t; the chosen defaults are the only category winners that already publish or support free-threaded builds, so free-threading support can be a launch feature rather than a retrofit. (6) Watch items: sqlite-vec 0.1.10 ANN (DiskANN/IVF) maturing — if it stabilizes, a single transactional SQLite sidecar covering FTS+vector could replace usearch as default; FTS5 availability on exotic Linux Pythons — add an apsw/pysqlite3-binary fallback probe at store-open time.

## Sources

- https://github.com/quickwit-oss/tantivy-py/releases
- https://github.com/quickwit-oss/tantivy-py
- https://tantivy-py.readthedocs.io/en/latest/tutorials/
- https://github.com/quickwit-oss/tantivy-py/issues/297
- https://sqlite.org/fts5.html
- https://docs.python.org/3/library/sqlite3.html
- https://charlesleifer.com/blog/using-the-sqlite-json1-and-fts5-extensions-with-python/
- https://github.com/Sygil-Dev/whoosh-reloaded
- https://pypi.org/project/Whoosh-Reloaded/
- https://duckdb.org/docs/current/core_extensions/full_text_search
- https://github.com/duckdb/duckdb/issues/3543
- https://github.com/duckdb/duckdb/discussions/15291
- https://duckdb.org/docs/current/core_extensions/vss
- https://github.com/unum-cloud/usearch
- https://pypi.org/project/usearch/
- https://github.com/unum-cloud/usearch/issues/560
- https://github.com/unum-cloud/usearch/issues/530
- https://pypi.org/project/hnswlib/
- https://github.com/nmslib/hnswlib
- https://pypi.org/project/faiss-cpu/
- https://github.com/faiss-wheels/faiss-wheels
- https://github.com/asg017/sqlite-vec/releases
- https://alexgarcia.xyz/blog/2024/sqlite-vec-metadata-release/index.html
- https://github.com/lancedb/lancedb
- https://lancedb.com/docs/indexing/reindexing/
- https://pypi.org/project/pyoxigraph/
- https://pyoxigraph.readthedocs.io/en/stable/store.html
- https://github.com/oxigraph/oxigraph/blob/main/CHANGELOG.md
- https://github.com/oxigraph/oxrdflib
- https://pypi.org/project/rdflib/
- https://github.com/RDFLib/rdflib/releases
- https://www.theregister.com/2025/10/14/kuzudb_abandoned/
- https://news.ycombinator.com/item?id=45560036
- https://github.com/cozodb/cozo/releases
- https://github.com/cozodb/cozo/discussions/299
- https://py-free-threading.github.io/tracking/
- https://docs.python.org/3/howto/free-threading-python.html
