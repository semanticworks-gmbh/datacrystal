# Finding: query-gigamap

_Feasibility study, 2026-06-10. One of 10 parallel investigations (5 reading the EclipseStore source in `resources/store`, 5 researching the Python ecosystem)._

## Summary

EclipseStore answers "complex queries on big data" in two tiers. Tier 1 (documented baseline, docs/modules/storage/pages/queries.adoc): there is no query language at all — queries are plain Java Streams over the in-heap object graph, with documented patterns for hand-rolled indexes (HashMap fields, TreeMap/NavigableMap ranges, materialized aggregates) and parallelStream only for CPU-heavy predicates on already-loaded data; the "microsecond / up to 1000x faster than JPA+RDBMS" claims live on eclipsestore.io marketing, not the manual. Tier 2 (GigaMap module): a billion-scale indexed collection that stores entities in a 3-level lazy-loaded segment tree (default 256 entities per leaf segment) and maintains off-heap, compressed hierarchical bitmap indexes (512-byte Level1 blocks = 4096 entity bits) per distinct key; queries are a fluent Condition AST compiled to BitmapResult trees combined 64 bits at a time with short-circuiting AND/OR/NOT, so only the entity segments containing hits are ever deserialized. Full-text is a Lucene sidecar index (gigamap/lucene) and ANN vector search a JVector/HNSW sidecar (gigamap/jvector), both auto-synced with the GigaMap and linking documents/vectors back via the stored entity id. Benchmarks I ran locally show CPython 3.14 filters 10M slot-objects at ~20-26 ns/object (~0.2-0.5 s/query, no GIL-free parallelism), while pyroaring ANDs bitmaps over a 10M-id space in ~306 µs and numpy columnar masks run at ~0.76 ns/row — so a Python port must make bitmap indexes + optional columnar mirrors the primary query path, not object iteration.

## Key findings

### Baseline query story: Java Streams ARE the query language, indexes are hand-rolled fields

docs/modules/storage/pages/queries.adoc (663 lines) states queries 'run on your data in the local JVM heap... no SQL dialect, no DSL'. It maps SQL/Cypher idioms to stream()/filter/flatMap/groupingBy, then documents escape hatches for scale: 'Streams scan; maps look up' (HashMap fields as secondary indexes stored in the graph), NavigableMap/TreeMap subMap for range queries, materialized aggregates updated synchronously in write methods, and domain-method encapsulation. parallelStream is recommended only 'when the predicate does real CPU work over data that is already loaded' with an explicit WARNING never to parallelize across Lazy.get() boundaries (triggers concurrent storage loads). No snapshot isolation: readers must use locks or defensive copies. The page ends by punting to GigaMap once 'a single collection grows beyond what is sensible to keep on the heap'.

### Performance claims: microseconds is marketing, not the manual

The in-repo docs only claim 'Ultra-fast — binary serialization significantly faster than traditional database approaches' (docs/modules/intro/pages/welcome.adoc). The quantified claims are on eclipsestore.io: 'Up to 1000x Faster than JPA + Traditional RDBMS', 'Microsecond response time, up to 1000x faster queries', 'Microsecond query time', 'save up to 99% on cloud data storage costs'. These compare in-process pointer dereferencing/HashMap gets against network+ORM round trips, not stream scans against SQL engines.

### GigaMap data layout: 3-level lazy segment tree, sequential entity ids, 2^50 capacity

GigaMap.java (3071 lines, gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/): entities live in GigaLevel3 (Lazy<GigaLevel2>[]) -> GigaLevel2 (Lazy<GigaLevel1>[]) -> GigaLevel1 (plain E[] array). Defaults in GigaMap.Dimensions: 2^8=256 entities per L1 segment, 2^10=1024 L1 segments per L2, total capacity capped at 2^50 (~10^15) entities because that is what the bitmap index can address. get(entityId) is bit-shift/mask index math plus Lazy.get() on exactly one L2 and one L1 segment — querying never materializes the collection. Change tracking (AbstractStateChangeFlagged) lets gigaMap.store() persist only dirty segments; gigaMap.store() also takes the internal lock (docs warn storageManager.store(gigaMap) does not).

### Bitmap index internals: per-key off-heap compressed bitmaps, 4096-bit Level1 blocks

A hashing bitmap index (AbstractBitmapIndexHashing.java) is an on-heap EqHashTable<K, BitmapEntry> mapping each distinct key to a BitmapEntry owning an off-heap BitmapLevel3->BitmapLevel2 hierarchy. BitmapLevel2.java (1538 lines) manages raw memory via XMemory.allocate (no GC), with byte-pattern compression (BytePatternCompression.java): trivial all-zeroes/all-ones Level1 entries collapse to a 1-byte header; non-trivial entries carry a 12-bit bitPopulation and 10-bit length. Constants in BitmapLevel3.java: LEVEL_1_SEGMENT_LENGTH=512 bytes=4096 entity bits, LEVEL_2_SEGMENT_LENGTH=256 L1 segments=1,048,576 entities per L2. The docs add that off-heap memory 'can be swapped to disk by the OS' so indexes can exceed RAM.

### Three indexer families + composite/spatial/multi-value; identity vs unique are separate concepts

docs/modules/gigamap/pages/indexing/bitmap/{index,types}.adoc: (1) regular hashing indexers (IndexerString/Integer/LocalDate/... ~16 types) for low cardinality, supporting equality, predicates, and ranges; (2) BinaryIndexer* (String/Long/UUID/...) for high cardinality — bit-sliced: max 64 bitmap entries, one per bit of the long-converted key, equality-only (is/in); (3) ByteIndexer* — base-256 byte decomposition, one sub-index of <=256 entries per byte position, giving high-cardinality range queries (between/lessThan/greaterThan); same composite machinery as IndexerLocalDate (year/month/day sub-indexes). Plus SpatialIndexer (lat/lon bounding box/proximity), IndexerMultiValue (Iterable keys, all(...) condition), IndexerComparing (any Comparator). Identity index = which fields locate an entity for remove/update (internal query instead of scan); uniqueness is enforced separately via withBitmapUniqueIndex/addUniqueConstraint, validated on every write with rollback (ConstraintViolationException). Custom constraints are persisted with the map, so they must be named classes, not lambdas.

### Query execution: condition AST -> word-level bitmap algebra -> lazy entity resolution

Condition.java builds And/Or/Not/Equals/In/All/Searched nodes; GigaQuery exposes fluent and()/or() plus ConditionBuilder (is/not/in/like/between/contains/startsWith/isYear/...). Condition.evaluate() yields BitmapResult trees; BitmapResult.ChainAnd/ChainOr/Not combine 64-bit words on the fly with short-circuit ((result &= ...) == 0L breaks; (result |= ...) == -1L breaks) — no intermediate result sets. andOptimize() reorders AND chains. AbstractBitmapIterating walks L3->L2->L1->long-word skipping empty segments and emits ascending entity ids; BitmapIterator resolves each id through the lazy segment tree so only segments containing hits load. Crucially, predicate conditions like contains()/is(Predicate) iterate the index's DISTINCT KEYS (O(cardinality), AbstractBitmapIndexHashing.search()) and OR the matching bitmaps — entities are never scanned. count() runs purely on the index.

### Concurrency and parallel queries: read-write locking + segment-partitioned threading

GigaMap uses internal read-write locking; every GigaIterator is AutoCloseable and MUST be closed to release read locks (try-with-resources; toList/findFirst/count close internally). createIterator() registers active readers and marks the map read-only during reads. Multithreaded execution: gigaMap.query(IterationThreadProvider) with ThreadCountProvider.Fixed(n)/Adaptive(max); the bitmap result is partitioned by segments and each thread (Creating or Pooling provider) processes its slice; docs say multi-consumer query.execute(consumer...) beats the ThreadedIterator because it avoids cross-thread iterator coordination. Stateful sub-query matchers force single-threaded fallback.

### Full-text and vector search are sidecar index groups on GigaMap, not bitmap features

gigamap/lucene/LuceneIndex.java: registered via articles.index().register(LuceneIndex.Category(context)); a DocumentPopulator maps entity fields to Lucene fields; every document carries LongField "_id_" (Store.YES) holding the GigaMap entity id; internalAdd/internalRemove/internalUpdateIndices keep it in sync on every map mutation; query(String|Query) parses full Lucene syntax (boolean, wildcard, phrase), returns entities resolved lazily from the map plus scores (LuceneSearchResult, SearchResultAcceptor(entityId, entity, score)). gigamap/jvector: HNSW ANN (k-NN, cosine/dot/euclidean), PQ compression, memory-mapped on-disk graphs, background persistence/optimization, SIMD via Panama on Java 20+, hybrid filtering with bitmap indexes; annotations @FullText and @Vector auto-define them. The top-level cache/ module is unrelated to search — it is JSR-107 JCache + Hibernate second-level cache backed by storage.

### Measured numbers: CPython object scans vs roaring bitmaps vs columnar

On this machine (Apple Silicon, CPython 3.14.5): list-comprehension filter (2 predicates) over 2M __slots__ objects = 20 ns/obj; dict-based objects = 26 ns/obj -> 10M objects ~ 0.2-0.5 s per ad-hoc scan, single-threaded only (GIL); CPython <=3.11 is typically 5-10x worse (~100-200 ns/obj -> 1-2 s). Java single-threaded streams over pointer-chased POJOs run ~5-20 ns/elem (~50-200 ms/10M), parallelStream divides by cores (~10-30 ms on 8-16 cores) — so realistic Python penalty is ~2-10x best-case on 3.14, 10-100x for non-trivial predicates, and ~100x+ vs parallel Java. Memory: 10M dict-based Python objects ~3-6 GB (slots ~1-1.5 GB) vs ~0.5-1 GB Java. Counter-measures measured: pyroaring BitMap AND across a 10M-id space = 306 µs + 116 µs to materialize 160k matching ids (literal microsecond queries); numpy int8 columnar mask = 0.76 ns/row (~8 ms/10M), i.e. faster than Java streams.

## Implications for the Python port

Replicate the architecture, not the implementation. (1) The GigaMap blueprint maps almost 1:1 to Python and is the part to copy: sequential int64 entity ids; a segmented lazy entity store (pages of ~256-4096 entities, deserialized on demand, dirty-flag change tracking for partial persistence); indexer = key-extractor callable per field; a Condition AST with And/Or/Not built via operator overloading ((idx.city == "Berlin") & (idx.age >= 18) reads better in Python than in Java); query results = bitmap of entity ids -> iterate ids -> load only touched pages; the predicate-over-distinct-keys trick for contains/startswith/custom predicates (O(cardinality), never O(entities)); identity indexes for update/remove; unique constraints validated on write. (2) Do NOT reimplement the off-heap machinery: EclipseStore's hand-rolled XMemory bitmaps, byte-pattern compression, and the 3-level bitmap hierarchy exist to dodge Java GC; in Python, pyroaring (CRoaring) already provides compressed, GC-invisible bitmaps with AND/OR/NOT at 0.3 ms across 10M ids — that is the entire query kernel for free. Bit-sliced binary indexes and byte-decomposed range indexes can be simplified to dict[key]->BitMap plus a sorted numpy array (searchsorted) for ranges. (3) Indexes are mandatory, earlier than in Java: CPython ad-hoc scans cost 0.2-0.5 s per 10M objects (3.14 best case; seconds on older CPython) with no parallelStream escape under the GIL, and 10M resident objects cost 1.5-6 GB — so the Python engine should treat unindexed full scans as a fallback and warn, roughly above 10^5-10^6 entities. (4) Add what Java didn't need: an optional columnar mirror (numpy/pyarrow arrays of indexed scalar fields, maintained like the bitmaps on add/update/remove) gives 0.76 ns/row vectorized scans — beating Java streams — and doubles as the zero-copy bridge to pandas/polars/duckdb/Arrow Flight, which is a genuine differentiator for pyrsistance. (5) Sidecar pattern for search: copy the IndexGroup design — full-text via tantivy-py (the Rust Lucene), vectors via usearch/hnswlib, RDF via oxigraph — each auto-synced on mutation and linking back through a stored entity-id field, results resolved lazily from the entity store with scores. (6) Keep the GigaMap concurrency contract (internal RW lock, context-manager iterators that release read locks, store() takes the lock); skip the thread-pooled parallel iteration initially — it buys little under the GIL — but design the bitmap-partitioning seam so free-threaded CPython (3.13+/3.14 nogil builds) or multiprocessing can exploit it later.

## Sources

- /Users/sh/pyrsistance/resources/store/docs/modules/storage/pages/queries.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/intro/pages/welcome.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/index.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/getting-started.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/indexing/bitmap/index.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/indexing/bitmap/types.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/queries/index.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/queries/executing.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/constraints.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/persistence.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/indexing/lucene/index.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/indexing/jvector/index.adoc
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/GigaMap.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/GigaLevel1.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/GigaLevel2.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/GigaLevel3.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/BitmapLevel2.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/BitmapLevel3.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/BitmapResult.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/BitmapEntry.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/AbstractBitmapIndexHashing.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/AbstractBitmapIterating.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/Condition.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/GigaQuery.java
- /Users/sh/pyrsistance/resources/store/gigamap/gigamap/src/main/java/org/eclipse/store/gigamap/types/BytePatternCompression.java
- /Users/sh/pyrsistance/resources/store/gigamap/lucene/src/main/java/org/eclipse/store/gigamap/lucene/LuceneIndex.java
- https://eclipsestore.io
