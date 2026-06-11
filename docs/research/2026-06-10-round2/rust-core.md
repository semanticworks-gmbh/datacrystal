# Round-2 finding: rust-core

_Round-2 research, 2026-06-10. Confidence: high. Cross-examined verdicts in [cross-examination.md](cross-examination.md)._

## Summary

A Rust core is mostly redundant for this design: the data-plane hot paths are already compiled (msgspec C, pyroaring/CRoaring, DuckDB/pyarrow C++, polars Rust, usearch C++, SQLite+FTS5 C in stdlib), and measured on CPython 3.14.0 the remaining pure-Python work is small in absolute terms (msgspec decodes msgpack directly into slots-dataclasses at 256 ns/obj = 3.9M obj/s, so a 1M-object boot is ~2-3.5s of which only ~50-70% is Python — Amdahl caps any Rust rewrite at ~1.5-2.5x). The parts that ARE pure Python (dirty-tracking __setattr__ at ~126 ns overhead/write, registry, swizzling, ghosts) are PyObject-graph operations where PyO3 boundary cost (~25 ns best-case no-arg call, 100-500 ns with conversions) erases most of the win and where precedents show the highest maintenance cost. The one place Rust genuinely pays — a custom append-only log engine (parallel mmap segment scan, CRC verify, off-thread compaction with GIL released) — is already scheduled as a later optimization behind the 3-method storage protocol, which is exactly the right Rust-ready seam. Existing Rust engines don't help: redb is solid but its official PyPI binding died in 2022, fjall has no Python bindings, sled is perpetually beta, and RocksDB (rocksdict) is an operationally heavyweight mismatch vs the already-decided SQLite-blob path. Recommendation: pure-Python-first with byte-oriented, batch-oriented seams; optional Rust accelerator wheel only post-v1 if profiling shows boot/compaction dominating.

## Key findings

### Hot-path inventory: ~80-95% of the runtime budget is already compiled code

Serialization (msgspec, C), bitmap set-ops (pyroaring wrapping CRoaring via Cython), columnar queries (DuckDB/pyarrow C++, polars Rust), vectors (usearch C++ SIMD), full-text (SQLite FTS5, C), and the v1 storage layer (stdlib sqlite3, C) are all native. Measured locally (CPython 3.14.0, Apple Silicon, msgspec 0.21.1): msgpack decode directly into a slots-dataclass = 256 ns/obj (3.9M obj/s), faster than decoding to dict (307 ns) — i.e., object hydration is ALREADY C, since msgspec natively constructs dataclasses. crc32 over a 600 B record = 110 ns (C). Remaining pure Python: reference swizzling, WeakValueDictionary registry insert (~510 ns/obj, weakref.__setitem__ is Python in 3.14), dirty tracking, commit-loop orchestration (~1 µs/dirty object), Condition-AST-to-roaring translation (µs per query, negligible).

### Quantified Rust ceiling on the boot path: ~1.5-2.5x, on an already-fast path

Projected 1M-object boot: ~0.5-1 s SQLite blob read (C) + ~0.3 s msgspec decode (C) + ~0.5 s registry + ~0.5-1.5 s swizzle/orchestration (Python) ≈ 2-3.5 s total, pure-Python share ~50-70%. By Amdahl, a Rust rewrite of the Python share caps total speedup at ~1.5-2.5x — and capturing it requires constructing/wiring PyObjects from Rust (the pydantic-core technique), the most fragile and unsafe-heavy kind of PyO3 code. pydantic-core got 17x only because it replaced ~10-50 µs/obj of pure-Python validation; pyrsistance's per-object Python cost is already ~0.5-1.5 µs, leaving little headroom.

### PyO3 boundary cost kills Rust for the PyObject-touching layers

PyO3 no-arg calls via vectorcall measure ~25 ns best case; with argument conversion, 100-500 ns is typical, and pythonspeed documents further hidden extension overheads (https://pythonspeed.com/articles/python-extension-performance/, https://github.com/PyO3/pyo3/issues/1607). Measured dirty-tracking overhead: tracked __setattr__ 156 ns vs plain 30 ns = ~126 ns/write (5.2x) — moving that hook across FFI would save almost nothing since the boundary itself costs the same order. Dirty tracking, OID registry, swizzling, Lazy/ghost class-swap, and Condition AST evaluation should NEVER move to Rust; better pure-Python mitigations exist (generated per-class __setattr__, batching). Side-finding: slots dataclasses need weakref_slot=True or WeakValueDictionary registration raises TypeError — a canonical-form requirement.

### Where Rust genuinely pays: the later-phase custom append-only log engine

Parallel mmap segment scan + per-record CRC verify + compaction with the GIL released (rayon across data files) is real data-plane work over bytes, no PyObjects — ideal Rust territory, returning batches of (oid, offset, bytes) for msgspec to decode Python-side. Secondary win: truly off-thread compaction and index-feed. But note checksumming is cheap anyway (110 ns/600 B record = 0.11 s per 1M records in C from Python), and bitmap-index boot building is better solved by the design's own 'indexes are rebuildable derived data' rule (persist roaring bitmaps + Arrow mirrors, reload instead of rebuild) than by Rust.

### PyO3/maturin 2026 state: mature, but free-threaded + abi3 don't combine until Python 3.15

PyO3 has supported free-threaded builds since 0.23; 0.28 defaults to assuming modules are thread-safe; 3.14t is supported (3.13t dropped). Critical: there is no limited API for free-threaded builds — abi3 settings are ignored on cp314t, forcing version-specific wheels. PEP 803 ('abi3t') was Accepted 2026-03-30 targeting Python 3.15, so the abi3-everywhere story arrives only with 3.15. For a >=3.14 project today, a Rust extension means a wheel matrix of ~3 OS × 2 arch × {cp314, cp314t, ...}; maturin + maturin-action/cibuildwheel automate it well (Rust cross-compiles cleanly, no manylinux glibc pain), but it is a permanent CI/release tax for a solo maintainer.

### Precedents: Rust cores correlate with company-scale teams and binding lag

polars: essentially everything in Rust, py-polars is a thin PyO3 wrapper; manages compile times via multiple feature-flagged runtime binaries and pushes contributors to a plugin system (pyo3-polars) — company-backed, not a solo pattern. pydantic-core: Rust validators, Python keeps the API and schema-building; 17x faster but raised the contribution barrier and created two-repo version-pinning release coupling; Pydantic later wrote its own JSON parser, showing the depth of investment required. tantivy-py: thin bindings over tantivy maintained by Quickwit; feature surface lags the Rust core enough that community forks (ntantivy-py) exist to fill gaps. lance/lancedb: full 'core + bindings' architecture (query engine, storage, indexing in Rust; PyO3 + zero-copy Arrow FFI) — built by a funded team. Common pattern: Rust holds the buffer/columnar data plane; Python keeps API, schema, and orchestration.

### Existing Rust engines are not a shortcut past SQLite-blob

redb: stable file format, v3 format + 3.0 release in progress, single-writer MVCC B-tree — conceptually the best fit, BUT the author's own PyPI binding ('redb' 0.5.0) was last released August 2022 against a pre-1.0 format and is effectively dead; adopting redb means writing and maintaining your own PyO3 binding, i.e., the exact burden under discussion. fjall: 3.0 (Jan 2026) / 3.1 (Mar 2026), capable RocksDB-like LSM, Rust-only, no Python bindings, and feature development is winding down in 2026. sled: still beta after years, storage subsystem being rewritten again (komora/marble), non-forward-compatible formats — avoid. RocksDB via rocksdict: actively maintained with broad wheels, but a heavyweight C++ LSM (compaction stalls, large binary, big tuning surface) mismatched to a single-process, single-writer, ~600 B-record workload. stdlib SQLite remains zero-dependency, battle-tested, and easily sufficient for the 1-5M-object envelope.

### Dependency-health note: msgspec wobble resolved, but it is the load-bearing C dependency

msgspec had a 2025 maintenance scare (Python 3.13/3.14 release lag; marimo shepherded a maintenance fork, plus msgspec-m/msgspec-x community forks), resolved upstream: msgspec 0.21.1 (April 2026) ships Python 3.14 wheels including free-threaded support, and the project moved to the msgspec/msgspec org. Since hydration speed hinges on msgspec's C dataclass decoding, pin and monitor it; the existence of viable forks is the fallback. This is also the strongest single argument that pyrsistance's 'compiled core' already exists — it's msgspec.

### Design seams that make the codebase Rust-ready without writing Rust now

(1) Keep the 3-method storage protocol strictly byte-oriented (oid -> bytes), with BATCH variants (write_batch/load_batch over lists) so a future Rust engine amortizes FFI per batch, not per record — per-call overhead of 25-500 ns is irrelevant at batch granularity. (2) Keep the record framing language-neutral (msgpack body + crc32/xxh3 trailer, documented layout) so a Rust scanner can read files written by Python and vice versa. (3) Keep all indexes (roaring, Arrow mirrors, FTS5, usearch) rebuildable from the commit-delta/watermark pipeline so an engine swap migrates only data files. (4) If a Rust accelerator ships post-v1, follow the cryptography/pydantic 'optional accelerator' pattern: pyrsistance[turbo] extra with pure-Python fallback, never a hard compiled dependency.

## Recommendation

Pure-Python-first with Rust-ready seams. v0.x: no Rust at all — SQLite-blob storage, batch-oriented byte-level storage protocol, language-neutral record framing, rebuildable indexes; profile against the 1-5M-object envelope (projected ~2-3.5 s boot for 1M objects is already acceptable). v1: still no Rust; spend the iteration speed on API design, which precedents (pydantic-core's contribution barrier, tantivy-py's binding lag) show Rust would freeze prematurely. Extension (post-v1, only if real workloads show boot scan or compaction dominating): an optional maturin-built Rust wheel implementing the custom append-only log engine behind the same 3-method protocol (parallel mmap scan + CRC + off-thread compaction, GIL released), shipped as pyrsistance[turbo] with pure-Python fallback — and by then PEP 803/abi3t on Python 3.15 will have collapsed the wheel matrix. Never: Rust for dirty tracking, registry, swizzling, ghosts, or Condition AST (PyObject-bound, FFI-dominated), and do not adopt redb/fjall/sled/RocksDB (no maintained Python bindings or operational mismatch) over the decided SQLite path. Justification in one sentence: the design's compiled core already exists in its dependencies, the residual Python is PyObject-graph code where Rust helps least and costs a solo maintainer most, and the storage protocol is the one seam where Rust can be added later without rework.

## Sources

- https://pyo3.rs/main/free-threading.html
- https://peps.python.org/pep-0803/
- https://github.com/PyO3/pyo3/issues/1607
- https://pyo3.rs/main/performance
- https://pythonspeed.com/articles/python-extension-performance/
- https://docs.pydantic.dev/1.10/blog/pydantic-v2/
- https://github.com/pola-rs/polars
- https://deepwiki.com/pola-rs/polars/3.3-plugin-system-(pyo3-polars)
- https://github.com/quickwit-oss/tantivy-py
- https://deepwiki.com/lancedb/lancedb
- https://github.com/cberner/redb
- https://pypi.org/project/redb/
- https://fjall-rs.github.io/post/fjall-3/
- https://github.com/spacejam/sled
- https://github.com/rocksdict/RocksDict
- https://pypi.org/project/msgspec/
- https://github.com/jcrist/msgspec/issues/891
- https://github.com/pypa/cibuildwheel
- https://www.maturin.rs/changelog.html
- Local microbenchmarks: CPython 3.14.0, msgspec 0.21.1, Apple Silicon (decode 256 ns/obj into slots-dataclass; registry insert +510 ns; tracked setattr 156 ns vs 30 ns plain; crc32(600 B) 110 ns)
