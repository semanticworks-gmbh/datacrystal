# Finding: surface-usage

_Feasibility study, 2026-06-10. One of 10 parallel investigations (5 reading the EclipseStore source in `resources/store`, 5 researching the Python ecosystem)._

## Summary

EclipseStore's product surface decomposes into: (1) a small core API — EmbeddedStorage.start(root, path) returning an EmbeddedStorageManager with storeRoot()/store(obj)/shutdown(), crash-safe append-only binary storage with channels, housekeeping, backup, import/export; (2) an Abstract File System (afs/) with 13+ pluggable storage targets (local NIO default, 5 SQL dialects, 7 blob/cloud backends); (3) GigaMap — an indexed, lazily-loaded collection with bitmap indexes, a typed fluent query API, plus optional Lucene full-text (@FullText) and JVector HNSW vector search (Vectorizer, cosine similarity, PQ compression) and spatial indexing; (4) framework integrations (Spring Boot 3 autoconfig, CDI4/Jakarta, REST storage inspector + web console, JMX monitoring); (5) a JSR-107 JCache implementation with Hibernate 2nd-level-cache support. The hello-world pattern is 4 lines: create root POJO, EmbeddedStorage.start, mutate, storeRoot. There is no RDF/triples support anywhere in EclipseStore — that part of pyrsistance is net-new. The 80/20 core is: start/root/store/restart-restore on local files + Lazy references + GigaMap-style indexed queries.

## Key findings

### Canonical hello-world usage pattern

From /Users/sh/pyrsistance/resources/store/examples/helloworld/src/main/java/.../HelloWorld.java: `final DataRoot root = new DataRoot(); final EmbeddedStorageManager storageManager = EmbeddedStorage.start(root, Paths.get("data")); System.out.println(root); /* shows state from last run */ root.setContent("Hello World! @ " + new Date()); storageManager.storeRoot(); // shutdown optional — crash-safe by design`. Incremental pattern (examples/storing): mutate a sub-object then `storageManager.store(root.myObjects)` or `store(dataItem)` — each store() is one atomic commit. Python sketch: `store = EmbeddedStorage.start(root=DataRoot(), path="data"); store.root.content = f"Hello {datetime.now()}"; store.store_root()`.

### Complete storage target list (afs/)

afs/ submodules: nio (local filesystem, the default, also any java.nio.FileSystem e.g. Jimfs in-memory); blobstore (shared base for all blob adapters); aws/s3; aws/dynamodb; azure/storage (Azure Blob); googlecloud/firestore; oraclecloud/objectstorage; kafka; redis; sql with 5 dialect creators (SqlFileSystemCreatorPostgres, MariaDb, Oracle, Sqlite, Hana — see /Users/sh/pyrsistance/resources/store/afs/sql/src/main/java/org/eclipse/store/afs/sql/types/). All implement one AFS interface; EmbeddedStorage.start(path) is sugar for NioFileSystem.New().ensureDirectoryPath(...). Docs pages confirm: aws-s3, aws-dynamodb, azure-storage, google-cloud-firestore, kafka, oracle-cloud-object-storage, redis, plus sql/{mariadb,oracle,postgresql,sqlite}.

### Core engine API surface (EmbeddedStorageManager)

From storage/embedded/.../EmbeddedStorageManager.java: start(), shutdown(), root()/setRoot()/storeRoot(), store/storeAll (via StorageManager), createStorer/createEagerStorer/createLazyStorer, createConnection(), issueFullGarbageCollection/issueFileCheck/issueCacheCheck (housekeeping on demand), issueFullBackup, exportChannels/exportTypes/importFiles/importData (binary + CSV import/export), createStorageStatistics, typeDictionary(), viewRoots(). Config properties (docs/modules/storage/pages/configuration/properties.adoc): storage-directory, storage-filesystem, channel-count, backup-directory, entity-cache-threshold/timeout, housekeeping-* (adaptive time budgets), data-file-*/transaction-file-* naming, lock-file-name, type-dictionary-file-name.

### Storing semantics: lazy vs eager storers, Lazy<T> loading

docs/modules/storage/pages/storing-data/lazy-eager-full.adoc: the object passed to store() is ALWAYS rewritten; referenced children already known to the PersistenceObjectRegistry (have an objectId) are skipped under the default lazy strategy — eager storers rewrite everything reachable. Bytes buffer in memory until storer.commit(). Lazy loading uses the Lazy<T> wrapper (org.eclipse.serializer.reference.Lazy): `Lazy<List<Turnover>> turnovers; Lazy.get(this.turnovers); Lazy.Reference(list)` (examples/lazy-loading/BusinessYear.java), with touched-timestamp-based clearing by housekeeping.

### GigaMap: indexed queryable collection (the query story)

gigamap/gigamap: GigaMap<E> is a persisted, partially-lazily-loaded collection with 3-level bitmap indexes (GigaLevel1/2/3, BitmapLevel2/3) and typed Indexers (IndexerString/Integer/Long/LocalDate/UUID/Number, BinaryIndexer, composite indexers, unique + custom constraints). Usage (examples/gigamap/BasicExample.java + PersonIndices.java): define static Indexer objects naming a field extractor, then `gigaMap.query(firstName.is("Thomas"))` returning GigaQuery<Person> with .toList(limit)/iteration; updates via `gigaMap.store()`. Indexes can also be generated from annotations: IndexerGenerator.AnnotationBased(Article.class).generateIndices(gigaMap). GigaMap is itself the storage root in the canonical example (storageManager.setRoot(gigaMap)).

### Full-text and vector search are GigaMap plugins

gigamap/lucene: LuceneIndex, @FullText(analyzed=true/false, store, name) annotation, DocumentPopulator, LuceneContext, AnalyzerCreator, LuceneSearchResult — registered via LuceneAnnotationHandler into the same GigaMap index registry. gigamap/jvector: VectorIndex (HNSW via JVector), Vectorizer<T> abstract class (override `float[] vectorize(entity)` + isEmbedded()), @Vector annotation, VectorSimilarityFunction (cosine etc.), PQCompressionManager (product quantization), DiskIndexManager, VectorSearchResult. Demo product-recommendations uses LangChain4j + local Ollama all-minilm to produce embeddings. There is also a bitmap-based spatial index (docs gigamap/indexing/spatial, country-explorer demo: radius, kNN, bounding-box, Haversine).

### Integrations: Spring Boot 3, CDI4, REST console, JMX monitoring

integrations/spring-boot3: autoconfiguration with `org.eclipse.store.*` property prefix, typesafe EclipseStoreProperties covering EVERY AFS target (S3, DynamoDB, Azure, Firestore, OracleCloud, Kafka, Redis, MariaDB, Oracle, Postgres, SQLite classes present), EmbeddedStorageManagerFactory/FoundationFactory beans, @Storage root annotation, @Read/@Write locking aspect with Mutex, multiple-storage support. integrations/spring-boot3-console: web UI autoconfig wrapping the REST inspector. integrations/cdi4: Jakarta CDI extension (config, cache, extension). storage/rest: rest-adapter + rest-service (javalin/springboot/sparkjava impls) + rest-client (jersey) + client GUI app, for human-readable inspection of stored binary data (explicitly NOT a query language). Monitoring is JMX MXBeans in storage/storage/.../monitoring (StorageManagerMonitorMXBean, EntityCacheSummaryMonitorMBean, StorageChannelHousekeepingMonitor). Micronaut integration exists in docs only.

### Cache module is JSR-107 JCache plus Hibernate L2

cache/cache implements javax.cache 1.1.1 (CachingProvider, CacheManager, Cache, EvictionPolicy/EvictionManager, CacheStore persisting through EclipseStore, CacheConfigurationMXBean, CacheStatisticsMXBean). cache/hibernate provides a Hibernate second-level cache region factory (CacheRegionFactory). Both are Java-ecosystem-specific glue with no meaningful Python analog.

### External configuration and tooling periphery

storage/embedded-configuration: EmbeddedStorageConfiguration.load("/META-INF/eclipsestore/storage.ini") supporting ini/properties/xml — example storage.ini is just `storage-directory = data` / `channel-count = 4`, then .createEmbeddedStorageFoundation().createEmbeddedStorageManager(root).start(). storage/embedded-tools: storage-converter and storage-migrator (MicroStream-to-EclipseStore). Other examples covering the long tail: reloader (reset in-memory objects from storage), custom-type-handler + custom-legacy-type-handler (schema evolution / legacy type mapping), layered-entities (generated immutable entity layers with versioning/logging), extension-wrapper, deleting (deletion = unreference + GC, no explicit delete API), blobs, filesystems (Jimfs in-memory).

### No RDF/triples anywhere; serialization core is external

Nothing in store/ (afs, gigamap, storage, cache, integrations, docs) mentions RDF, triples, or SPARQL — that pyrsistance pillar has no EclipseStore precedent. The binary serializer (type dictionary, type handlers, object registry, Lazy, legacy type mapping engine) lives in the separate eclipse-serializer repo; this clone only contains its consumers plus docs/modules/serializer. docs/modules/communication describes serializer-based TCP object messaging (also external). Type dictionary + persisted type metadata is what enables class evolution across restarts.

## Implications for the Python port

MVP (the 20% delivering 80%): (1) `start(root, path) -> StorageManager` with `store_root()` / `store(obj)` / transparent restore-on-start — this single loop IS the product; make it work with plain classes, dataclasses, and pydantic models. (2) Local-filesystem target only at first, but behind an AFS-like 3-method abstraction (read/write/list blobs) so SQLite and S3 (the only two targets that matter early in Python: stdlib sqlite3, boto3) drop in later; the other ~10 connectors are demonstrably long-tail enterprise checklist items. (3) A Lazy[T] reference wrapper for partial graph loading — without it large graphs force full load and the value prop collapses. (4) A GigaMap analog: a persisted indexed collection with declared indexers and a fluent query (`gm.query(Person.first_name == "Thomas").to_list(10)`) — in Python, derive indexers from dataclass/pydantic field annotations instead of anonymous Indexer subclasses, and use roaring bitmaps (pyroaring) for the index. Replicate EclipseStore's key design move: full-text (tantivy-py/Whoosh) and vector (hnswlib/usearch) are sibling index types registered on the same collection via field annotations, not separate subsystems — pyrsistance's RDF/triple index can follow the same plugin pattern (net-new, no Java precedent). (5) Replicate storing semantics exactly: passed object always rewritten, known children skipped (lazy default), explicit eager storer optional; each store() is an atomic commit to an append-only file + transaction log (crash-safe, shutdown optional). Persist a type dictionary from day one — it is what makes schema evolution (their legacy-type-mapping) possible later; without it stored data rots. Simplify away: channels/channel-count (Java thread-parallelism; Python GIL makes 1 channel + async housekeeping the right start), JCache/Hibernate (no analog), CDI/Spring autoconfig (replace with a FastAPI lifespan dependency + pydantic-settings using the same flat key names like storage-directory), JMX (expose stats as a dict / prometheus), REST inspector (a later FastAPI router that walks the live object graph is strictly easier than their binary-level adapter), storage-converter/migrator tools, layered-entities, extension-wrapper. Defer but keep in design: continuous backup, import/export (use pyarrow for the export format instead of their CSV), housekeeping GC of unreferenced entities (deletion model is unreference+GC — document this early, it surprises users).

## Sources

- /Users/sh/pyrsistance/resources/store/examples/helloworld/src/main/java/org/eclipse/store/examples/helloworld/HelloWorld.java
- /Users/sh/pyrsistance/resources/store/examples/storing/src/main/java/one/microstream/examples/storing/Main.java
- /Users/sh/pyrsistance/resources/store/examples/lazy-loading/src/main/java/org/eclipse/store/examples/lazyLoading/BusinessYear.java
- /Users/sh/pyrsistance/resources/store/examples/helloworld-ini/src/main/resources/META-INF/eclipsestore/storage.ini
- /Users/sh/pyrsistance/resources/store/examples/gigamap/src/main/java/org/eclipse/store/examples/gigamap/BasicExample.java
- /Users/sh/pyrsistance/resources/store/examples/gigamap/src/main/java/org/eclipse/store/examples/gigamap/PersonIndices.java
- /Users/sh/pyrsistance/resources/store/examples/gigamap/src/main/java/org/eclipse/store/examples/gigamap/vector/ProductVectorizer.java
- /Users/sh/pyrsistance/resources/store/afs/ (nio, blobstore, aws/{s3,dynamodb}, azure/storage, googlecloud/firestore, oraclecloud/objectstorage, kafka, redis, sql)
- /Users/sh/pyrsistance/resources/store/afs/sql/src/main/java/org/eclipse/store/afs/sql/types/
- /Users/sh/pyrsistance/resources/store/storage/embedded/src/main/java/org/eclipse/store/storage/embedded/types/EmbeddedStorageManager.java
- /Users/sh/pyrsistance/resources/store/storage/embedded-configuration/src/main/java/org/eclipse/store/storage/embedded/configuration/types/
- /Users/sh/pyrsistance/resources/store/storage/rest/
- /Users/sh/pyrsistance/resources/store/storage/storage/src/main/java/org/eclipse/store/storage/monitoring/
- /Users/sh/pyrsistance/resources/store/gigamap/{gigamap,lucene,jvector}/src/main/
- /Users/sh/pyrsistance/resources/store/cache/{cache,hibernate}/
- /Users/sh/pyrsistance/resources/store/integrations/{spring-boot3,spring-boot3-console,cdi4}/
- /Users/sh/pyrsistance/resources/store/demos/README.md
- /Users/sh/pyrsistance/resources/store/docs/modules/storage/pages/storage-targets/index.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/storage/pages/storing-data/lazy-eager-full.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/storage/pages/configuration/properties.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/gigamap/pages/indexing/lucene/defining.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/misc/pages/integrations/spring-boot.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/misc/pages/monitoring/index.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/storage/pages/rest-interface/index.adoc
