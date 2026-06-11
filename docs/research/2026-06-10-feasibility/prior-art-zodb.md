# Finding: prior-art-zodb

_Feasibility study, 2026-06-10. One of 10 parallel investigations (5 reading the EclipseStore source in `resources/store`, 5 researching the Python ecosystem)._

## Summary

ZODB affirmatively proves the EclipseStore concept works in Python: it has run transparent object-graph persistence in production for 25+ years (Plone CMS deployments at FBI, NASA, Harvard; RelStorage installations with 44M+ object rows) and the entire stack is still actively maintained — ZODB 6.3 shipped 2026-04-14 with Python 3.14 support, with 2026 releases also for ZEO, RelStorage, persistent, BTrees, and transaction. Its architecture (persistent.Persistent base class with C-extension attribute interception, ghost objects, tri-state _p_changed dirty tracking, per-connection PickleCache, append-only FileStorage with packing, MVCC snapshot isolation, pluggable storages) is a directly reusable blueprint. Adoption stalled for identifiable, fixable reasons: Python-only opaque pickle format that breaks on class moves/renames and made the py2→3 migration traumatic; leaky abstraction requiring manual _p_changed=True for plain dicts/lists; zero built-in indexing/search (bolt-on ZCatalog/zope.catalog causes ConflictErrors under write load); no asyncio API (GitHub issue #311 still open); and a "Martian technology" perception problem. Today's actually-used embedded persistence tools (diskcache 39.3M downloads/month, tinydb 3.1M, sqlitedict 1.8M vs ZODB 205K) are all key-value/document stores — none offer transparent object-graph persistence, so the niche a modern reboot targets is currently unserved.

## Key findings

### Verdict: ZODB proves the concept works in Python — yes

ZODB has been in production since ~2000 as the storage engine of Zope and Plone; Plone sites for FBI, CIA, NASA, Disney, Harvard, Stanford run on it (sixfeetup.com/blog/can-plone-scale), plone.org itself serves ~1M page views/month on it, and a documented RelStorage/PostgreSQL deployment held 44M+ rows in object_state (community.plone.org thread 21406). ZODB docs claim thousands of commits/second with ACID snapshot isolation. JPMorgan independently built a similar append-only pickle-based object store circa 2010 (HN 29113528). The concept — in-memory Python object graph as the database, transparent persistence, no external server — demonstrably works and scales for read-heavy workloads.

### Core architecture: persistent.Persistent + C extension + ghosts

The persistent package implements Persistent in C (src/persistent/cPersistence.c) with pure-Python fallbacks (persistence.py); the C type hooks attribute access (tp_getattro/tp_setattro) so touching a 'ghost' (empty shell, state not loaded) triggers automatic state load from the storage, and any __setattr__ sets the dirty flag. _p_changed is tri-state: None=ghost, False=loaded/clean, True=dirty. _p_jar holds the Connection (data manager), _p_oid the 8-byte object id, _p_serial the revision TID. Per-connection object cache is cPickleCache.c plus ring.c (doubly-linked LRU ring); cache invalidation propagates on commit. _v_* attributes are volatile (never persisted); __getattr__/__getattribute__ overrides are explicitly discouraged as nearly impossible to get right against the machinery.

### Storage layer: append-only FileStorage, packing, MVCC, pluggable backends

FileStorage is a log-structured single file: transaction records with data records appended, plus an in-memory (and checkpointed on-disk) OID→file-offset index proportional to DB size; old revisions enable undo/time-travel until 'packing' copies current records to a new file (analogous to Postgres vacuum). MVCC gives each connection snapshot isolation; conflicts are detected per-object at commit (ConflictError) with optional conflict-resolution hooks (BTrees implement them). Storage API is pluggable: FileStorage (single process only), ZEO (client-server, rewritten on asyncio), RelStorage (pickles inside PostgreSQL/MySQL/Oracle/SQLite, leveraging native RDBMS MVCC; repeatable-read for loads), plus layering for compression/encryption. Layered design: persistent (object lifecycle) / transaction (generic 2PC transaction manager) / storage (MVCC + durability).

### Indexing story is the biggest hole — and the clearest EclipseStore-gigamap parallel

ZODB has no built-in indexing, query language, or full-text search; its own docs say don't use it for search-primary apps. The BTrees package (OOBTree/IOBTree etc., C-accelerated, conflict-resolving) is the raw building block; real indexing requires bolt-ons: ZCatalog/zope.catalog (alive, zope.catalog 6.0 Sep 2025), repoze.catalog (dead, last release 2019), hypatia (0.5, Nov 2024). Catalog indexes are the documented hot spot for ConflictErrors under write-intensive load, forcing workarounds like QueueCatalog deferred indexing. Jim Fulton's Newt DB (dual-representation: pickle + JSONB in Postgres, searchable via SQL/jsonb indexes) was the official answer to this gap and is dead since 2017 (newt.db 0.9.0, 2017-06-29) — evidence the gap was recognized but never fixed in-core.

### Pickle format is the root cause of most pain

Storage sees only opaque pickles: non-Python tools cannot read the DB at all; indexing at storage level is impossible without unpickling; persistent cross-references embed the dotted class path, so moving or renaming a class breaks unpickling (requires keeping import aliases forever, or running zodbupdate, which literally rewrites pickle opcodes across the whole DB). The Python 2→3 migration was a documented multi-step trauma (bytes/str pickle protocol issues, ZODB issue #285, dedicated Plone migration guides). HN commenters describe 'Martian Technology Syndrome' and having to serialize the datastore for migrations. EclipseStore solved exactly this with a versioned binary type dictionary + legacy type mapping — pyrsistance must not use pickle as the canonical format.

### Leaky dirty tracking is the #2 developer-experience pain

Mutation of plain Python mutable attributes (list.append, dict[k]=v) is invisible to the persistence machinery; developers must set obj._p_changed = True manually, or use PersistentList/PersistentMapping/BTrees instead of native types, or only assign immutables. Docs further warn that custom __eq__/__hash__ force ghost loading (performance trap) and that __getstate__/__setstate__ overrides become brittle. This 'you must know the machinery' tax is cited in the Wikipedia limitations list and HN threads as a primary reason developers preferred ORMs over ZODB.

### No asyncio story; concurrency is thread+connection-bound

ZODB's API is fully synchronous; GitHub issue zopefoundation/ZODB#311 ('Asyncio support') remains an open wishlist item. Persistent objects are bound to one connection and must not be shared across threads; the sanctioned async patterns are thread-pool executors sized to the connection pool, gevent monkey-patching, or the third-party dm.zodb.asynchronous helpers. Only ZEO's network layer was rewritten on asyncio. There is no public free-threading (PEP 703) work; the C extensions (cPersistence, cPickleCache, BTrees) predate the nogil ABI. A 2026 reboot has a green field here.

### ZODB is actively maintained in 2025/2026 — but tiny mindshare

Release dates from PyPI JSON API: ZODB 6.3 (2026-04-14, adds Python 3.14, drops 3.9), 6.2 (2026-01-23), 6.1 (2025-10-01); ZEO 6.2 (2026-04-13); RelStorage 4.3.0 (2026-05-20); persistent 6.7 (2026-05-26); BTrees 6.4 (2026-04-29); transaction 5.1 (2026-03-17). GitHub zopefoundation/ZODB last push 2026-04-14, 755 stars, 75 open issues. So: maintained (largely by the Plone ecosystem), but not growing — pypistats last_month downloads: ZODB 205K vs diskcache 39.3M, tinydb 3.1M, sqlitedict 1.8M; the standalone transaction package (921K/month) outpaces ZODB itself because Pyramid/SQLAlchemy apps reuse it.

### Durus: proof a radically simpler core suffices

Durus (CNRI/MEMS Exchange team, now maintained by Neil Schemenauer at github.com/nascheme/durus) is a deliberate reimplementation of the ZODB subset they actually used after 3 years on ZODB: Persistent base class, append-only FileStorage with on-disk index, ClientStorage server (ZEO analog), aggressive per-connection cache — but no MVCC, no undo, no multi-threaded access, no conflict resolution. PyCon 2005 paper documents the rationale (complexity of ZODB existed for features they never used). Status: version 4.3 released 2024-10-01, repo last pushed 2025-10-20, 34 stars — minimally alive. Lesson: a single-writer, process-embedded design (which is also EclipseStore's default posture) cuts most of ZODB's complexity.

### What people actually use today leaves the object-graph niche empty

The popular embedded options are all key-value or document stores with pickle/JSON values: diskcache (SQLite+files, 39.3M dl/month, cross-process, Django-compatible), sqlitedict (dict-on-SQLite, 1.8M/month, thread-safe), tinydb (pure-Python JSON document DB, 3.1M/month, explicitly not for concurrency or speed), stdlib shelve (dbm+pickle, widely advised against in favor of sqlitedict). None offer transparent reachability-based object-graph persistence, transactions over an object graph, or identity preservation. ZODB remains the only maintained Python system in that category — and its ~205K/month shows the category is dormant, not disproven.

## Implications for the Python port

REPLICATE from ZODB (it is validated prior art): (1) the layered split — object lifecycle / transaction manager / pluggable storage with MVCC — and strongly consider depending on the existing, actively-maintained `transaction` package (5.1, Mar 2026) for two-phase commit instead of writing one; (2) ghost objects + per-connection LRU cache for graphs larger than RAM (EclipseStore's lazy Loading is the same idea); (3) append-only log storage with background packing/GC (matches EclipseStore's storage channels + housekeeping); (4) per-object conflict detection with snapshot isolation; (5) BTrees-style conflict-aware ordered collections as index primitives. DO DIFFERENTLY: (1) canonical serialization must NOT be pickle — use a versioned, self-describing binary format with a persisted type dictionary and legacy-type mapping (EclipseStore's solution), generated from dataclass/pydantic/attrs definitions so class renames/moves are a metadata remap, not a DB rewrite, and so non-Python tools (and Arrow export) can read it; (2) dirty tracking should be automatic — instrument via dataclass field descriptors/__setattr__ and ship persistent list/dict/set as the default field types (or copy-on-write snapshots), eliminating the _p_changed footgun that ZODB's docs spend pages explaining; (3) build indexing IN: declarative secondary indexes (GigaMap analog), FTS (e.g. tantivy/SQLite FTS5-style) and vector indexes updated transactionally in the same commit — ZODB's bolt-on catalog with ConflictError hot spots is the cautionary tale, and dead Newt DB shows half-measures don't stick; (4) asyncio-native API from day one (async commit/load, async storage SPI) plus a free-threading-safe cache design — ZODB's open issue #311 and thread-pool workarounds are its weakest competitive flank; (5) type hints as schema and typed query expressions — ZODB has no typing story at all. SIMPLIFY: follow Durus and EclipseStore, not ZEO — single-process embedded engine first (one writer, many readers via MVCC snapshots), skip client-server, undo/time-travel, and cross-thread object sharing initially; Durus proves the subset is enough, and diskcache's 39M downloads/month proves Python developers adopt embedded, serverless persistence when the ergonomics are right. Positioning: the transparent object-graph niche has zero modern, typed, async, search-capable occupant — ZODB validates feasibility (25 years in production, still releasing in 2026) while its pickle/DX/search failures define exactly the checklist a 2026 reboot must clear.

## Sources

- https://zodb.org/en/latest/guide/writing-persistent-objects.html
- https://zodb.org/en/latest/introduction.html
- https://zodb.org/en/stable/changelog.html
- https://zodb.org/en/latest/articles/ZODB-overview.html
- https://zodb.org/en/latest/reference/storages.html
- https://github.com/zopefoundation/ZODB
- https://github.com/zopefoundation/persistent (src/persistent: cPersistence.c, cPickleCache.c, ring.c)
- https://github.com/zopefoundation/ZODB/issues/311
- https://github.com/zopefoundation/zodbupdate
- https://github.com/zopefoundation/ZODB/issues/285
- https://pypi.org/pypi/{ZODB,ZEO,RelStorage,persistent,BTrees,transaction,zope.catalog,repoze.catalog,hypatia,newt.db,Durus}/json (release dates via PyPI JSON API)
- https://pypistats.org/api/packages/{zodb,tinydb,diskcache,sqlitedict,persistent,btrees,transaction}/recent (download counts)
- https://news.ycombinator.com/item?id=29113528
- https://news.ycombinator.com/item?id=6791293
- https://en.wikipedia.org/wiki/Zope_Object_Database
- https://sixfeetup.com/blog/can-plone-scale
- https://community.plone.org/t/crippling-performance-issues-relstorage-postgresql-db-with-44m-zodb-objects/21406
- https://seecoresoftware.com/blog/2019/10/intro-zodb.html
- https://relstorage.readthedocs.io/en/latest/index.html
- https://github.com/nascheme/durus
- http://ftp.ntua.gr/mirror/python/pycon/2005/papers/17/ (Durus PyCon 2005 paper)
- https://github.com/newtdb/db
- https://6.docs.plone.org/backend/upgrading/version-specific-migration/upgrade-zodb-to-python3.html
- http://blog.rfox.eu/en/Programming/Python/Dont_use_Shelve_use_sqlitedict.html
- https://grantjenks.com/docs/diskcache/
- https://pypi.org/project/dm.zodb.asynchronous/
