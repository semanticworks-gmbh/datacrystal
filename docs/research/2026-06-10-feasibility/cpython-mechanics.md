# Finding: cpython-mechanics

_Feasibility study, 2026-06-10. One of 10 parallel investigations (5 reading the EclipseStore source in `resources/store`, 5 researching the Python ecosystem)._

## Summary

Ran original benchmarks on CPython 3.14.5 and 3.14.0t (Apple Silicon) plus web research. Headline results: (a) a pure-Python "class-swap ghost" (ZODB semantics via assigning __class__ on first touch) works on 3.14 and has ZERO steady-state attribute overhead, beating wrapt proxies (4.0x forever) and even ZODB's C extension (1.6x reads, 2.6x writes); (b) a small slots dataclass costs ~135 bytes RSS (~170 with __dict__) vs ~32-40 bytes for the equivalent Java object — 3.5-4x, so 10M objects ≈ 1.4GB; (c) bulk filtering 10M Python objects is 14-23x slower than pyarrow/numpy masks and 100x slower for aggregation — indexes must be columnar; (d) msgspec msgpack encodes 100k typed records in 6.9ms vs pickle-p5 80.7ms (12-14x), with validation on decode; (e) free-threaded 3.14 is officially supported (PEP 779) but a pointer-chasing object scan pays ~2.5x single-thread penalty on 3.14t (far above the advertised 5-10% average) and 8 threads only tie the GIL build's single thread. Note: EclipseStore itself uses explicit Lazy<T> wrapper fields, not transparent proxies, and the prompt's claim that ZODB "changes class of unloaded objects" is wrong — the persistent C extension keeps the class and uses a state flag + tp_getattro hook.

## Key findings

### Class-swap ghosts: ZODB semantics at zero steady-state cost, pure Python

Verified on 3.14.5: a generated GhostRec(Rec) subclass with __slots__=() overrides __getattribute__, loads state on first touch, then does object.__setattr__(self, '__class__', Rec) — layout-compatible class assignment is legal and after the swap reads are 22.5 ns/op and writes 12.5 ns/op, identical to direct access. Same trick gives one-shot dirty tracking: Clean subclass with __setattr__ that registers the object dirty and swaps to the real class, so only the FIRST write per object per transaction pays the hook. Benchmark: /tmp/pyrs_bench/bench_ghost.py. Constraint: requires non-frozen classes and identical slot layout (empty-slots subclass of the user class works).

### Proxy alternatives measured: wrapt 4.0x forever, ZODB C ext 1.6x, descriptors 4.0x

Attribute read on slots dataclass: direct 22.6 ns; wrapt.ObjectProxy (C extension active) 90 ns (4.0x); pure-Python __getattr__ proxy 85 ns (3.8x); persistent.Persistent (C tp_getattro, cPersistence confirmed) 36.8 ns (1.6x); per-field lazy descriptor 89 ns (4.0x). Writes: direct 12.3 ns; pure-Python __setattr__ dirty hook 153 ns (12.4x); persistent C write 32 ns (2.6x). Proxies also leak identity ('is' comparisons, type() checks, C-API consumers like numpy/orjson bypass __class__ spoofing). /tmp/pyrs_bench/bench_proxy.py.

### ZODB 'persistent' mechanics correction

The persistent C extension does NOT change the class of ghosts. Persistent instances keep their class; ghost is a state (_p_changed is None, _p_jar/_p_oid set, state dict released). Per_getattro (tp_getattro in cPersistence.c) unghostifies on any attribute access including method lookup; tp_setattro marks _p_changed and registers with the data manager; 'is' comparisons do not unghostify. Source: persistent.readthedocs.io interfaces + zodb.org guide. The class-swap technique above is therefore NOVEL relative to ZODB and avoids ZODB's need for a C extension and a mandatory base class with per-access overhead.

### EclipseStore itself chose explicit Lazy<T>, not transparent proxies

/Users/sh/pyrsistance/resources/store/docs/modules/storage/pages/loading-data/lazy-loading/index.adoc: fields are declared Lazy<ArrayList<Turnover>> turnovers = Lazy.Reference(...) and accessed via .get(); clearing is manual Lazy.clear() or timeout-based via LazyReferenceManager (touched-timestamp variant documented). Even in Java, with bytecode weaving available, they made laziness explicit and put it at subgraph boundaries (collections), not on every reference. This legitimizes an explicit Lazy[T] descriptor as the primary pyrsistance API.

### Memory: ~135-170 B per small Python object vs ~32-40 B Java; pydantic 588 B

RSS-measured, 1M instances of a 3-field record (unique int + unique float + shared str), CPython 3.14.5: dataclass with __dict__ 170 B; slots dataclass 135 B; msgspec.Struct 134 B; persistent.Persistent 233 B; pydantic BaseModel 588 B; tuple 150 B. Boxed payload alone (int 28 B + float 24 B) is ~60 B of that — there is no primitive flattening. Equivalent Java object (16 B header + 4 int + 8 double + 4 compressed ref, padded) is ~32-40 B, so Python is 3.5-4x; 10M-object graph ≈ 1.4-1.7 GB. /tmp/pyrs_bench/bench_memory.py.

### GC and registry: gc.freeze() kills 464 ms pauses; WeakValueDictionary costs 212 B/entry

Full gc.collect() with 10M live slots-dataclass instances pauses 464 ms; after gc.freeze() (moves all current objects to a permanent generation) a collect is 0.0 ms — freeze after each load/commit batch is the key tactic for a large stable graph. gc.callbacks exists for instrumentation. WeakValueDictionary as OID->object registry: 632 ns insert, 93 ns lookup, 212 B/entry, auto-eviction on refcount death confirmed — refcounting gives deterministic eviction notification via weakref callbacks, something Java needs ReferenceQueues for. Slots classes need weakref_slot=True (dataclass arg, 3.11+). No compaction exists; long-lived churn fragments pymalloc arenas, so per-object allocation should be batched.

### Bulk scans over Python objects are 14-109x slower than columnar — indexes must be pyarrow/numpy

10M slots-dataclass records, 3.14.5: Python loop filter+count 184 ms (18 ns/obj), attribute-sum 222 ms; numpy boolean mask 8.1 ms (23x faster), numpy sum 2.0 ms (109x); pyarrow compute filter 13.4 ms (14x), sum 2.3 ms (97x). Building the 10M objects took 5.1 s. Conclusion: the GigaMap analog cannot iterate the object graph; it must maintain columnar sidecar indexes (numpy/arrow arrays of OIDs + indexed fields) and hydrate objects only for final result sets. /tmp/pyrs_bench/bench_scan.py.

### Serialization: msgspec msgpack 12-14x faster than pickle p5, with schema validation

100k records (int, str, float, list[str]), 3.14.5: msgspec msgpack encode 6.9 ms / decode+validate 16.7 ms (4.9 MB); pickle protocol 5: 80.7 / 48.4 ms (5.2 MB); msgspec JSON 11.2 / 21.9 ms; cattrs+orjson 40.8 / 100 ms; pydantic v2 TypeAdapter 42.1 / 108 ms. Per-object (EclipseStore stores flat per-instance records): pickle 1.28 us/obj vs msgspec msgpack 93 ns/obj. msgspec cannot encode shared references/cycles, but that is irrelevant when references are swizzled to OIDs in flat per-object records, exactly as EclipseStore's binary format does. pickle p5 out-of-band buffers (PEP 574) remain useful for zero-copy of large numpy/arrow payload fields. /tmp/pyrs_bench/bench_serial.py.

### Free-threading mid-2026: officially supported, ecosystem transitional, pointer-chasing penalty is real

PEP 779 accepted June 2025: free-threaded build is officially supported (phase II of PEP 703) in 3.14 (Oct 2025), not the default; default flip projected 2028-2030; Python 3.15 lands Oct 2026 with further ft and JIT work. Advertised single-thread overhead is 5-10% on pyperformance, but my measured object-graph scan on 3.14.0t: 1 thread 179 ms vs 70 ms on the GIL build (~2.5x penalty — biased/deferred refcounting slow path when worker threads touch objects allocated by the main thread), scaling to only 2.5x at 8 threads (71.8 ms), i.e. 8 ft threads merely tie 1 GIL-build thread on this workload. Ecosystem: numpy/scipy/pytorch/pydantic-core ship cp313t/cp314t wheels; grpcio and others still block full stacks. /tmp/pyrs_bench/bench_ft.py.

### Dirty-tracking option matrix with measured costs

(1) Permanent __setattr__ hook on base class: 12.4x every write — reject as default. (2) C-extension hooks (ZODB model): 2.6x writes, requires shipping a C ext — defer. (3) Tri-state class swap Ghost->Clean->Dirty: one 153 ns hit on first write per object per txn, zero after — winner. (4) Immutable+diff / snapshot-hash at commit: zero write overhead, O(loaded objects) commit using msgspec encode at 93 ns/obj as the cheap fingerprint, costs a retained snapshot or hash per loaded object — good complement for objects whose class cannot be subclassed (frozen dataclasses, pydantic models, third-party types). (5) pydantic validators: 588 B/obj and slowest serialization — keep pydantic at the API boundary (FastAPI), not as the storage object model.

## Implications for the Python port

Recommended combo for pyrsistance: (1) Object model: slots dataclasses (weakref_slot=True) or msgspec.Struct as the canonical stored form, ~135 B/obj; treat pydantic strictly as an edge/API layer with converters. (2) Lazy loading, two tiers exactly like EclipseStore: explicit Lazy[T] descriptor fields at subgraph boundaries (collections, big blobs) as the documented API — it is what EclipseStore itself does and is predictable — plus transparent class-swap ghosts for intra-graph object references (generated GhostX subclass, __getattribute__ loads state then assigns __class__ back), giving ZODB transparency with zero post-load overhead and no C extension. Reject wrapt/ObjectProxy: permanent 4x access tax and identity leaks. (3) Dirty tracking: tri-state class swap (Ghost->Clean->Dirty, one ~150 ns hook hit per object per transaction); fall back to msgspec-encode fingerprint diff at commit for unswappable/frozen/third-party types; never a permanent __setattr__ hook. (4) Registry/memory: OID->object via WeakValueDictionary (deterministic eviction through refcounting — simpler than EclipseStore's swizzling registry + Java GC interplay); call gc.freeze() after load/commit batches to eliminate ~0.5 s cyclic-GC pauses at 10M objects; budget 3-4x Java RAM and document it; consider arrow-backed columnar storage for very large value-only collections instead of object-per-row. (5) Queries/GigaMap analog: maintain numpy/pyarrow columnar indexes (OID column + key columns) beside the graph; run filters/aggregations columnar (14-109x faster), hydrate ghosts only for final hits — this also gives FTS/vector/RDF indexes a natural columnar substrate. (6) Wire format: EclipseStore-style flat per-object binary records with references swizzled to OIDs, encoded with msgspec msgpack (93 ns/obj, validated decode), pickle-p5 only as escape hatch for arbitrary types, PEP 574 buffers for zero-copy numpy/arrow payloads. (7) Concurrency: design single-writer/multi-reader with an explicit commit lock; be ft-ready (no global mutable state, pure-Python core or cp314t wheels) but do not architect around free-threaded parallel scans yet — measured 2.5x single-thread penalty and poor scaling on pointer-chasing workloads mean columnar indexes (which release the GIL inside numpy/arrow anyway) are the better parallelism story through at least 2027.

## Sources

- /tmp/pyrs_bench/bench_memory.py (original benchmark, CPython 3.14.5 macOS arm64)
- /tmp/pyrs_bench/bench_proxy.py
- /tmp/pyrs_bench/bench_scan.py
- /tmp/pyrs_bench/bench_serial.py
- /tmp/pyrs_bench/bench_ghost.py
- /tmp/pyrs_bench/bench_ft.py (3.14.0 vs 3.14.0+freethreaded)
- /Users/sh/pyrsistance/resources/store/docs/modules/storage/pages/loading-data/lazy-loading/index.adoc
- /Users/sh/pyrsistance/resources/store/docs/modules/storage/pages/loading-data/lazy-loading/clearing-lazy-references.adoc
- https://peps.python.org/pep-0779/
- https://docs.python.org/3/whatsnew/3.14.html
- https://py-free-threading.github.io/tracking/
- https://persistent.readthedocs.io/en/latest/api/interfaces.html
- https://zodb.org/en/latest/guide/writing-persistent-objects.html
- https://jcristharif.com/msgspec/benchmarks.html
- https://gist.github.com/jcrist/d62f450594164d284fbea957fd48b743
- https://blog.ionelmc.ro/2015/01/12/proxying-objects-in-python/
- https://wrapt.readthedocs.io/
- https://realpython.com/python-news-june-2026/ (Python 3.15 feature freeze)
- https://peps.python.org/pep-0803/ (abi3t stable ABI for free-threaded builds)
