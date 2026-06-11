# Round-2 finding: rdf-native

_Round-2 research, 2026-06-10. Confidence: high. Cross-examined verdicts in [cross-examination.md](cross-examination.md)._

## Summary

RDF should not be first-class because the expensive part of an RDF store is not the triples — it is the term dictionary maintenance, the permutation indexes, and above all a SPARQL engine, which is a multi-month solo-killer; meanwhile the object graph already provides 2 of the 3 ingredients for free (stable int64 identity = interned subject terms; references = forward edges). The one genuinely missing graph primitive is the REVERSE-reference index, and that belongs in core regardless of RDF since it powers cascade checks, orphan/GC, backlinks, and impact analysis at a cost of roughly 8-16 B/edge (+5-15% on the 600 B/object envelope) when stored as a sorted Arrow pair table rather than per-OID roaring bitmaps. Triple projection fits the already-decided "rebuildable derived sidecar fed by commit-delta watermark" pattern (same as FTS5/usearch), making RDF a clean extension, not a core concern — and pyoxigraph's documented in-memory mode defuses the original objection (no persistent RocksDB needed when triples are rebuildable derived data). The property-graph gap in Python 2026 is real (Kuzu archived 2025-10-10 after the Apple acquisition, CozoDB dormant since Dec 2023/Dec 2024, networkx is in-memory-only), but the right answer for this project is reverse index + traversal API + a DuckPGQ recipe over the existing Arrow mirrors, not a query-language engine.

## Key findings

### Reverse-reference index: design and what it buys (CORE)

Design: the serializer already walks every dirty object's fields to swizzle refs into OIDs, so R_new(oid) is free at commit; R_old comes from decoding the previous msgpack record (one SQLite blob read + µs msgspec decode) or an optional in-memory oid->refset cache (~8 B x fan-out per object, ~+5%). The diff (added/removed edges) feeds an incoming_refs(tgt_oid, src_oid, field_id) structure maintained LSM-style: sorted base run + per-commit delta + tombstone bitmap, compacted at the existing watermark. Buys: 'what links here' backlinks (agent-memory killer feature), cascade-delete safety, orphan detection, impact analysis/derived-data invalidation, safe Lazy-ghost eviction (only unload objects with known inbound state), bidirectional traversal (k-hop neighborhoods), and parent.children without denormalized child lists. Note EclipseStore itself has NO reverse index (its storage GC does forward reachability sweeps), so this is an addition beyond the inspiration, justified by the persona.

### Reverse-index representation: sorted Arrow pairs beat per-OID roaring bitmaps; cost +5-15%

A roaring bitmap per target OID is the wrong layout at low fan-in: croaring container + pyroaring wrapper costs ~100-200 B fixed per bitmap, so 5M targets ≈ 0.5-1 GB of pure overhead. Roaring shines keyed per (type, field) — exactly EclipseStore GigaMap's bitmap-per-(indexer, key) pattern — as a derived accelerator layer over hot edges. Canonical store should be a sorted (tgt, src, field) Arrow/numpy table: 16 B/edge with raw int64 OIDs, ~8-10 B/edge with dense int32 internal ids; at 5M objects x avg 4 refs = 20M edges that is ~160-320 MB, i.e. +5-11% on the 3 GB envelope, binary-searchable via searchsorted and zero-copy queryable via DuckDB. Write overhead estimate: O(fan-out) int set-diff per dirty object plus delta append, ~10-30% commit-time, zero read-path cost. pyroaring provides BitMap64 for the int64-OID accelerator layer (https://github.com/Ezibenroc/PyRoaringBitMap).

### EclipseStore local-source evidence: GigaMap is bitmaps, has no graph/RDF layer

/Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/ contains 128 files dominated by bitmap index machinery (BitmapIndex, BitmapLevel2/3, CompositeBitmapIndex, per-type BinaryIndexer*), plus Lucene and jvector sidecar modules — confirming the planned 'bitmap conditions + rebuildable sidecar indexes' architecture is a faithful GigaMap analog. EclipseStore has no reverse-reference index, no triple projection, and no graph query language anywhere in the tree: the design inspiration treats the object graph itself as sufficient, which supports demoting RDF below core.

### Native triples design: term dictionary + Annotated projection as a third sidecar

Subjects are free: OIDs are already interned int64s (skolemize as urn:pyr:oid:{n}). Add a terms(id INTEGER PRIMARY KEY, lex, kind) dictionary in the existing SQLite file — the analog of Oxigraph's id2str table — interning predicates IRIs and literals; predicates are schema-derived from Annotated field markers (e.g. name: Annotated[str, rdf('foaf:name')]) so predicate cardinality is dozens, not millions. Triples are projected in the same idempotent commit-delta/watermark pipeline as FTS5/usearch: triples = rebuildable derived data, zero new persistence technology. This is the decisive architectural point: RDF rides existing machinery end to end.

### Index permutations: 3 orderings suffice; bitmaps for star joins, Arrow/DuckDB for chains

Production systems converge on 3 permutations for triples: Jena TDB2 uses SPO/POS/OSP (6 only for quads: https://jena.apache.org/documentation/tdb/store-parameters.html), Blazegraph stores B+Tree permutations SPO/POS/OSP (https://blazegraph.com/database/apidocs/com/bigdata/rdf/spo/SPORelation.html), Oxigraph stores spo/pos/osp for the default graph plus 6 quad orderings and id2str (https://github.com/oxigraph/oxigraph/wiki/Architecture); 6-way Hexastore (Weiss et al., VLDB 2008) is unnecessary at this predicate cardinality. With few predicates, vertical partitioning (Abadi et al., VLDB 2007, per-predicate (s,o) tables) fits Arrow naturally: one triples table with sorted-by-s and sorted-by-o views ≈ SPO+POS+OSP coverage. Roaring-bitmap-per-(predicate, object-term) is exactly the GigaMap equality index, and star-shaped BGPs (shared subject variable) compile directly to existing bitmap AND/OR algebra — precedent: BitMat answered conjunctive triple-pattern joins with bitwise AND/OR up to 10x faster than RDF-3X (https://ceur-ws.org/Vol-517/ssws09-paper3.pdf). Chain patterns (?x p ?y . ?y q ?z) need s-o joins that bitmap algebra cannot do — hand those to DuckDB hash joins over the Arrow triple table, already zero-copy in the stack.

### SPARQL: rdflib Store facade is ~500 LOC; own BGP engine is the trap; pyoxigraph in-memory kills the RocksDB objection

Path A (recommended extension): implement rdflib's plugin Store API — essentially add/remove/triples((s,p,o), context)/__len__ plus term conversion; rdflib's pure-Python SPARQL 1.1 engine then does nested-loop joins by calling triples() per pattern: fully correct, slow, fine for interop/serialization/pyshacl. Size calibration: oxrdflib is 543 LOC of Python total, store.py 232 LOC (measured via GitHub API on oxigraph/oxrdflib), and it delegates query evaluation to pyoxigraph; a facade exposing pyrsistance triples is realistically 400-700 LOC, 1-2 weeks. Path B (avoid): compiling SPARQL BGPs to bitmap joins — a star-BGP+FILTER subset is 1-2 months, full SPARQL 1.1 (OPTIONAL, property paths, aggregates) is 6+ months solo; never. Path C (optional accelerator): pyoxigraph Store() without a path is documented as 'kept in memory and never written on disk' (https://pyoxigraph.readthedocs.io/en/stable/store.html) — feed it from the same watermark pipeline and you get fast native SPARQL 1.1 with NO fourth persistence technology, since the triple store is ephemeral rebuildable derived data; persistence stays in pyrsistance. Rebuild-on-open is seconds-to-tens-of-seconds at 1-10M triples; at the 5M-object/50M-triple ceiling its memory cost argues for the Arrow path instead.

### Property-graph gap in Python 2026 is real — and DuckPGQ is already in the stack

Kuzu's repo was archived 2025-10-10 after Kùzu Inc. (≈10 employees) was acquired — reported as Apple via EU DMA filings — leaving the MIT code orphaned with only the immature Kineviz 'bighorn' fork (https://www.theregister.com/2025/10/14/kuzudb_abandoned/, https://www.macobserver.com/news/apple-buys-graph-database-startup-kuzu-eu-filing-shows-more/). CozoDB: last release v0.7.6 on 2023-12-11, last commit 2024-12-04 (GitHub API) — dormant ~18 months. networkx is an in-memory algorithms library with no persistence or transactions. Remaining embedded options are SQLite recursive CTEs and DuckPGQ, an actively maintained DuckDB community extension implementing SQL:2023 SQL/PGQ with on-the-fly CSR construction (https://duckdb.org/community_extensions/extensions/duckpgq, https://duckpgq.org/). Since pyrsistance already mirrors entities to Arrow and queries via DuckDB, exposing a property-graph view (vertex tables = entity mirrors, edge table = the reverse-ref index) gives typed MATCH/path queries for roughly a documentation page plus a small helper — a genuine differentiator over both EclipseStore and the dead embedded graph DBs, without writing a Cypher engine.

### Cost asymmetry that decides the question

First-class RDF means committing core to: term dictionary GC, named-graph semantics, SPARQL conformance, RDF literal typing (lang tags, xsd types), and N x permutation write amplification on every commit — each a permanent tax on the single-writer commit path and on a solo maintainer. Implicit graph via reverse index costs ~2-3 weeks once, accelerates features the core needs anyway (GC/cascade/eviction), and leaves triples as a pay-for-what-you-use extension whose entire pipeline (dictionary in SQLite, deltas via watermark, indexes in Arrow/bitmaps, joins in DuckDB) reuses decided machinery. The stress-test demotion of pyoxigraph was right in conclusion but wrong in reasoning: the issue is not RocksDB (in-memory mode exists), it is that full SPARQL/RDF semantics are not worth core complexity when 80% of graph value is reverse edges + traversal + SQL/PGQ.

## Recommendation

CORE in v0.x: reverse-reference index as a sorted Arrow (tgt, src, field_id) table maintained LSM-style in the commit pipeline (~2-3 weeks) plus a traversal API (incoming()/outgoing()/neighbors()/paths, ~1 week) — justified independently of RDF by cascade checks, orphan detection, backlinks, and ghost-eviction safety. v1: per-(type, field) roaring-bitmap accelerators over hot edges integrated with the Condition AST, plus a documented DuckPGQ property-graph recipe over the existing Arrow mirrors (~days, mostly docs). Extension package (pyrsistance-rdf, post-v1): Annotated rdf-predicate markers + SQLite term dictionary + triples Arrow sidecar fed by the watermark pipeline (~3-4 weeks), an rdflib Store facade (~500 LOC, 1-2 weeks) for serialization/interop/slow-but-correct SPARQL, and optionally pyoxigraph in in-memory mode as a rebuildable SPARQL 1.1 accelerator (~1 week). Never (solo realism): a homegrown full SPARQL or Cypher engine, or persistent RocksDB in core — graph ergonomics come from reverse edges and SQL/PGQ, not from a query-language engine.

## Sources

- https://www.theregister.com/2025/10/14/kuzudb_abandoned/
- https://www.macobserver.com/news/apple-buys-graph-database-startup-kuzu-eu-filing-shows-more/
- https://github.com/cozodb/cozo/releases
- https://github.com/oxigraph/oxigraph/wiki/Architecture
- https://github.com/oxigraph/oxrdflib
- https://pyoxigraph.readthedocs.io/en/stable/store.html
- https://jena.apache.org/documentation/tdb/store-parameters.html
- https://blazegraph.com/database/apidocs/com/bigdata/rdf/spo/SPORelation.html
- https://rdflib.readthedocs.io/en/stable/
- https://duckdb.org/community_extensions/extensions/duckpgq
- https://duckpgq.org/
- https://www.cidrdb.org/cidr2023/papers/p66-wolde.pdf
- https://github.com/Ezibenroc/PyRoaringBitMap
- https://ceur-ws.org/Vol-517/ssws09-paper3.pdf
- https://www.vldb.org/conf/2007/papers/research/p411-abadi.pdf
- https://dl.acm.org/doi/10.14778/1453856.1453965
- https://networkx.org/documentation/stable/reference/index.html
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/
