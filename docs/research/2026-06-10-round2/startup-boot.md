# Round-2 finding: startup-boot

_Round-2 research, 2026-06-10. Confidence: high. Cross-examined verdicts in [cross-examination.md](cross-examination.md)._

## Summary

Boot does NOT need parallel compute — it needs persisted index structures, and the OID-as-sequential-counter design makes them exceptionally cheap. Benchmarks on M1 Pro (Python 3.14, numpy 2.4): a dense-array checkpoint (numpy arrays indexed by OID, mmap'd at open) makes clean-shutdown boot ~10-100 ms independent of history size; sealed-file footers merged via vectorized scatter (no sort!) handle crash recovery at 19-23 ns/raw-record-version (0.2 s at 10M, 1.15 s at 50M) vs 105-547 ns/record for the pure-Python full scan (1-5 s / 5-27 s). Parallelism is a dead end in Python: a 2-thread pure-Python scan measured 1.5x SLOWER than 1 thread (GIL), and EclipseStore's channel partitioning (verified in StorageChannelTaskInitialize/StorageEntityInitializer source) only works because JIT-compiled Java parses headers at ~ns/record — it has no persisted index at all and re-scans every file on every boot. Vectorizing the variable-length header chain directly is impossible (sequential offset dependency), which is precisely why offsets must be persisted out-of-band — i.e., the footer IS the vectorization enabler. Recommended: checkpoint + footers + full-scan fallback chain when the custom log lands; SQLite-blob-store v0.x has no boot problem at all (B-tree = index is the storage, recovery bounded by WAL).

## Key findings

### EclipseStore boot verified: full re-scan every time, parallelized only by channel threads

Source confirms no persisted index exists. /Users/sh/pyrsistance/resources/store/.../types/StorageEntityInitializer.java (registerEntities/indexEntities): each channel reads every data file fully into a direct ByteBuffer, walks the length-prefixed header chain sequentially (address += length), then registers entities in reverse file order so the first OID occurrence (latest version) wins. StorageChannelTaskInitialize.java internalProcessBy() runs channel.readStorage() once per channel worker thread (StorageChannel.java line 47: 'A single storage worker thread, responsible for a fixed slice'); OIDs are hash-partitioned objectId % channelCount, default channel-count=1 per https://docs.eclipsestore.io/manual/storage/configuration/properties.html. The transaction log (StorageTransactionsAnalysis) records only file-level lengths/timestamps for truncation validation — never per-entity offsets. Java affords this because JIT-compiled header walking runs at a few ns/record; Python's ~50-100x interpreter penalty is the entire problem.

### Partitioning/threads buy nothing in Python — measured negative scaling under the GIL

Benchmark (M1 Pro, CPython 3.14.5 GIL build): pure-Python header walk of 2M records = 169 ms single-threaded (85 ns/rec; 105 ns/rec with dict insert), but TWO threads splitting the same work took 253 ms — 1.5x slower due to GIL contention. struct.unpack_from and dict inserts hold the GIL; mmap page faults do NOT release it. numpy DOES release the GIL: 2 threads each doing load+sort of 50M int64 finished in 1.04x single-thread time (near-perfect scaling) — so numpy-based footer loading could be threaded, but at 0.2-1.2 s total it is not worth it. Multiprocessing works (numpy arrays pickle at ~memcpy speed) but macOS spawn costs ~0.3-0.5 s before any work — only conceivably useful for the full-scan fallback, never for the primary path.

### Owner's 547 ns/record envelope confirmed plausible; floor is ~105 ns/rec

Minimal pure-Python walk (24-byte header, struct.unpack_from, dict insert) measured 105 ns/record; the owner's 547 ns presumably includes checksum verification and richer headers. Full-scan boot therefore: 10M record-versions = 1.1-5.5 s, 50M = 5.3-27 s — matching the stated 5-30 s range. Dict materialization alone is a hidden cost: building a 10M-entry Python dict from arrays took 1.75 s (175 ns/insert) and ~1 GB RAM, meaning even a fast-loading checkpoint is ruined if it is materialized into a dict. The in-RAM boot index must stay numpy-native.

### Lever 1 — Checkpoint as DENSE numpy arrays indexed by OID: boot in ~10-100 ms at any scale

Because OIDs come from a sequential counter, the index needs no hash and no sort: dense arrays offset[oid] i8, length[oid] i4, file[oid] i4 (+optional tid i8) = 16-24 B/OID-slot (240 MB at 10M, 1.2 GB at 50M — 3-4% of the 600 B/object envelope; deleted-OID holes cost 24 B each until compaction). Measured: np.load(mmap_mode='r') reopen = 5-8 ms at both 10M and 50M; random lookups on the mmap'd array 24-94 ns each (noise vs the msgspec decode that follows); full warm read 44 ms (320 MB) / 217 ms (1.6 GB), cold adds ~0.1-0.5 s at NVMe rates. Checkpoint header carries (format_version, max_oid, last_tid, log watermark, xxhash); written atomically (temp+rename) at clean close and during housekeeping. Boot: if checkpoint tid == log tail tid, mmap and go; else replay tail from watermark in pure Python. Checkpoint is rebuildable derived data — same philosophy as the FTS5/usearch sidecars.

### Lever 2 — Sealed-file footers + scatter merge: crash recovery in 0.2 s (10M) / 1.15 s (50M), NO sort needed

SSTable/Parquet pattern (https://github.com/google/leveldb/blob/main/doc/table_format.md, https://parquet.apache.org/docs/file-format/metadata/): when a data file rolls over, append a columnar footer (oid[], offset[], length[] arrays + length + magic + checksum at EOF) — the writer already holds this table in RAM, so sealing is nearly free. Boot without checkpoint: read footers tail-first, scatter into the dense arrays in file order (later files overwrite) — measured 194 ms for 10M raw entries across 100 footers, 1.15 s for 50M across 500 (19-23 ns/raw-entry), then pure-Python-scan only the unsealed head file (64-128 MB rollover ≈ 110-220k records ≈ 60-120 ms at 547 ns). Critical negative result: the sort-based merge (argsort kind='stable' + last-wins) measured 3.5 s at 10M and 33 s at 50M — numpy's stable argsort on int64 is the bottleneck; dense scatter avoids it entirely and is 25-50x faster than any pure-Python scan. A file whose footer is missing (crash during seal) degrades to a bounded single-file scan.

### Lever 3 — Vectorizing the header chain without footers is structurally impossible

Each header position depends on the previous record's length (sequential offset chain), so numpy cannot parse it data-parallel. The theoretical workaround (pointer-jumping: compute next[i]=i+len(i) for every byte offset, then log-step doubling) needs ~8x file size in scratch memory per pass — impractical at multi-GB files. np.fromfile/mmap only accelerate the I/O, not the chain walk. Conclusion: vectorization REQUIRES offsets stored out-of-band, which is by definition the footer/checkpoint. There is no third option.

### Lever 4 — Free-threaded Python and Rust: real but unnecessary for the primary path

Python 3.14 free-threading is officially supported (PEP 779, https://docs.python.org/3.14/whatsnew/3.14.html) with 5-10% single-thread penalty, and would make EclipseStore-style channel partitioning actually scale — but footers already beat an 8-way parallel pure-Python scan by >3x with zero threading complexity, and msgspec/ecosystem free-threaded wheels remain a compatibility risk. Rust (PyO3 + rayon, GIL-released mmap scan, hardware CRC32C at ~10-20 GB/s/core) would take the catastrophic full-scan fallback from 5-27 s to roughly 0.3-1 s at 50M and make per-record checksum verification at boot affordable — worthwhile only as a later extension if crash-without-footers recovery ever matters in practice.

### How LSM engines and SQLite avoid the problem entirely — and what to copy

RocksDB: MANIFEST is a transactional log of version edits (the list of SST files + key ranges); CURRENT points to the latest manifest; boot = read manifest + replay only the WAL into the memtable; SST index blocks load lazily (https://github.com/facebook/rocksdb/wiki/MANIFEST). SQLite: the B-tree IS the storage so the index is never rebuilt; crash recovery replays only the WAL, auto-checkpointed at 1000 pages (~4 MB) by default, so recovery is O(checkpoint interval) not O(history) (https://www.sqlite.org/wal.html, https://www.sqlite.org/fileformat2.html). Mapping: footer = SSTable index block, checkpoint = manifest snapshot, log tail = WAL. EclipseStore is the outlier in this comparison set — it is the only one that rescans data files at boot.

### SQLite-blob-store v0.x sidesteps the problem completely

With SQLite as the blob backend, OID->blob is a rowid B-tree lookup; open is O(1) page reads and crash recovery is bounded by the WAL. The boot problem only materializes when the custom append-only log lands. Action for v0.x: reserve the format affordances now — record header layout, a 'sealed' flag + footer-pointer field in the data-file trailer, and a tid watermark in the transaction log — so footers and checkpoints bolt on without a format migration. Bonus: footers double as free per-file live-ratio statistics for the future compactor/GC (occupancy without scanning), which EclipseStore computes by scanning.

### Expected boot times (M1 Pro class hardware, warm cache; cold adds ~0.1-0.5 s NVMe read)

10M record-versions / ~4.3M live: clean close + checkpoint ~10-50 ms; crash + footers ~0.3-0.5 s (0.2 s merge + head-file scan); no footers, pure-Python full scan 1-5.5 s; Rust full scan <0.2 s. 50M record-versions / ~21M live: checkpoint ~10-100 ms (mmap, lazy paging) or ~0.5-1 s if eagerly faulted cold; crash + footers ~1.3-2 s; pure-Python full scan 5-27 s; Rust full scan ~0.3-1 s. Checkpoint write cost at close: 240 MB-1.2 GB sequential write ≈ 0.1-0.5 s, amortizable into housekeeping. Benchmark scripts preserved at /tmp/pyrs_bench/bench1.py through bench4.py.

## Recommendation

v0.x: ship SQLite-blob-store (boot problem does not exist there) but freeze the custom-log record/trailer format now with a sealed-flag + footer slot and tid watermark. Custom-log v1: implement BOTH levers — columnar footer appended at file seal (writer already has the table in RAM) and dense-numpy-array checkpoint (mmap at open, never materialize a Python dict) with the boot chain checkpoint -> footers -> full-scan fallback, giving ~10-100 ms typical boot and <2 s crash recovery at 50M records. Extension (only if telemetry shows footer-less recovery happening): Rust GIL-released parallel scan for the fallback path. Never: GIL-build thread partitioning (measured 1.5x slower), multiprocessing channels, free-threaded-Python channel scans — persisted indexes beat all parallelism options by an order of magnitude at zero concurrency complexity, exactly as the LSM/SQLite precedent predicts.

## Sources

- /Users/sh/pyrsistance/resources/store/storage/storage/src/main/java/org/eclipse/store/storage/types/StorageChannelTaskInitialize.java
- /Users/sh/pyrsistance/resources/store/storage/storage/src/main/java/org/eclipse/store/storage/types/StorageEntityInitializer.java
- /Users/sh/pyrsistance/resources/store/storage/storage/src/main/java/org/eclipse/store/storage/types/StorageFileManager.java
- /Users/sh/pyrsistance/resources/store/storage/storage/src/main/java/org/eclipse/store/storage/types/StorageChannel.java
- /tmp/pyrs_bench/bench1.py
- /tmp/pyrs_bench/bench2.py
- /tmp/pyrs_bench/bench3.py
- /tmp/pyrs_bench/bench4.py
- https://docs.eclipsestore.io/manual/storage/configuration/properties.html
- https://github.com/facebook/rocksdb/wiki/MANIFEST
- https://github.com/google/leveldb/blob/main/doc/table_format.md
- https://parquet.apache.org/docs/file-format/metadata/
- https://www.sqlite.org/wal.html
- https://www.sqlite.org/fileformat2.html
- https://docs.python.org/3.14/whatsnew/3.14.html
- https://peps.python.org/pep-0779/
