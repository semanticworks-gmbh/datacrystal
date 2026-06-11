# Performance validation suite

> Workflow runs wf_41ceea42-869 (resumed), 2026-06-10/11. Summary: Designed datacrystal's v0.x performance validation suite: a 13-row benchmark table where every gate is a same-run ratio (engine vs re-measured floor, big-corpus vs small-corpus) rather than absolute wall-clock, anchored to the feasibility numbers (600 B/obj, 256 ns decode, 20-26 ns/obj scans, 306 us roaring AND) and including the two O() proofs — boot O(checkpoint) vs write-history churn and watermark apply O(delta) at fixed delta across 10k vs 1M corpora (the sda_store lesson). A deterministic stdlib-random generator (seed 0xDC, no wall-clock/uuid4) reuses the Person/City/Note/Tag demo vocabulary with frozen append-only Notes, hub fan-in, and slug unique keys at N=10k/100k/1M. Harness is pytest-benchmark (gates as same-run asserts, non-timing metrics via extra_info) run via `uv run --group bench`, with a <2-min PR gate suite at N<=100k, a nightly 1M sweep feeding a github-action-benchmark trend (repo-internal until PyPI reservation), and release comparison against committed tag baselines.

# datacrystal v0.x performance validation suite

Principle: every **gate** is a ratio of two quantities measured in the *same run on the same machine* (engine vs floor, big-corpus vs small-corpus, churned vs clean) or a byte count — never absolute wall-clock. The feasibility anchors (~600 B/obj, ~256 ns msgspec decode, 20–26 ns/obj scan, ~306 µs roaring AND/10M) are re-measured each run as "floors"; the engine is gated on its overhead *over* its own floor. Absolute numbers are tracked as **trends** only.

## 1. Benchmark table

| name | guards | metric | threshold form | cadence |
|---|---|---|---|---|
| `mem_bytes_per_object` | 600 B/obj all-in envelope → the 1–5 M-object positioning | (RSS_after − RSS_before)/N, fresh subprocess, N live registered entities (registry+indexes+Lazy) | ≤ 690 B/obj (anchor +15%); bytes, runner-stable; also ratio vs bare slots-dataclass RSS ≤ 5× (trend) | PR @100k; nightly @1M |
| `commit_tput_small` | buffer-until-commit storer + 3-phase commit overhead (ADR-001 P1/P2/P3) | obj/s over 100 txns × 100 dirty objs | ≥ 1/3 of same-run floor = msgspec-encode + SQLite `executemany` blob insert of identical payloads (engine ≤ 3× floor) | every PR |
| `commit_tput_large` | large-txn amortization; P1 capture must not be quadratic | obj/s, 1 txn × N objs | engine ≤ 2× same floor; t(100k)/t(10k) ≤ 12 (linear) | PR @10k; nightly @100k |
| `commit_latency_fsync` | honest fsync policy triad; fsync floor never hidden | p50/p99 of 1-obj commit under `never`/`interval`/`per-commit` | t(per-commit) − t(never) within 0.5–2× same-run raw `os.fsync` (4 KB) floor; t(never) ≤ 5× encode floor; p99 trend | every PR |
| `boot_vs_liveset` | boot = O(persisted checkpoint); SQLite B-tree *is* the boot index | cold open→root-ready wall time, fresh subprocess, checkpoint of N objs | boot(10·N)/boot(N) ≤ 12 (≈linear in checkpoint, no superlinear) | PR 10k/100k; nightly 1M |
| `boot_vs_history` | **O(checkpoint), never O(write history)** | same 100k live set; history A = clean insert, history B = 20× churn (rewrites+deletes, no GC) | boot(B)/boot(A) ≤ 1.25 | PR (10k live); nightly (100k) |
| `hydrate_batch` | 256 ns/obj decode anchor; batch hydration API (SDA delta) | ns/obj, load-many of 10k ghost OIDs | ≤ 4× same-run raw msgspec decode-to-dataclass loop (engine adds ≤ 3× decode); ns/obj trend | every PR |
| `hydrate_n_plus_1` | "N+1 must never be the user's problem" | batch(1k OIDs) vs loop of 1k single `Lazy.get()` | batch speedup ≥ 5× | every PR |
| `query_bitmap_vs_scan` | why bitmap indexes exist (scans 20–26 ns/obj) | 2-predicate AND (`(Person.city=='Berlin') & (Person.age>=18)`, ~1% selectivity): Condition-AST query (no hydration) vs full Python scan | speedup ≥ 10× @100k, ≥ 50× @1M; query ≤ 20× same-run bare pyroaring AND (306 µs anchor); scan ns/obj trend | PR @100k; nightly @1M |
| `unique_key_lookup` | unique secondary-key index (URIs/slugs, upsert-by-natural-key — SDA delta) | lookups/s, hit + miss | t(@1M)/t(@10k) ≤ 2 (≈O(1)); ≤ 50× same-run plain dict lookup | PR 10k/100k; nightly 1M |
| `watermark_apply_fixed_delta` | **O(delta), never O(corpus)** — the sda_store post-mortem; public pipeline contract | apply identical 1k-op delta (bitmap index + FTS5 sidecar once it lands) at two corpus sizes | t(@big)/t(@small) ≤ 1.2 — PR: 10k vs 100k; nightly: 10k vs 1M | every PR + nightly |
| `snapshot_cost` | ADR-001 rider 2: `store.snapshot()` is the cheap pressure valve | time to take watermark view (frozen DTOs/bitmaps) | t(@1M)/t(@100k) ≤ 3 (sublinear; should be ≈O(1)) | nightly |
| `file_size_amplification` | SQLite-blob honesty; churn must not balloon the store | bytes-on-disk / Σ live msgpack payload bytes | ≤ 1.6 after clean insert; ≤ 3.0 after 10× churn + housekeeping (VACUUM/`incremental_vacuum` policy) | nightly |

Recovery/crash benches are correctness tests (torn-tail fuzzing), not perf — excluded here.

## 2. Synthetic generator

`benchmarks/_domain.py` + `benchmarks/_gen.py`, importable by tests and docs. Fully deterministic: single `random.Random(0xDC)` (stdlib), **no wall-clock, no uuid4, no `os.urandom`** in generated data; timestamps are `epoch + seq` ints; ids are sequence-derived. Same `(seed, N, generator_version)` ⇒ byte-identical store, so store files for boot benches are built once per session and cached in tmp keyed by that tuple plus format version.

**Entity mix** (reuses the demo-domain vocabulary already in DESIGN.md/SDA docs — `Person.first_name/city/age`, 'Berlin', 'Thomas', slugs-as-unique-keys, frozen event records):
- 70% `Person(slug, first_name, city: Lazy[City], age, manager: Lazy[Person]|None, notes: list[Lazy[Note]], tags)` — `slug = f"person/{i:08d}"` feeds `unique_key_lookup`; `age`/`city` distributions engineered so the canonical predicate hits ~1%.
- 20% `Note` with `@entity(frozen=True)` (append-only agent-memory persona; exercises never-arming dirty tracking) → `author: Lazy[Person]`, text from a fixed 5k-word vocabulary (later feeds FTS).
- 8% `Tag`, 2% `City` — **hub objects**: cities ≈ N/500 with zipf-ish fan-in; 10 "mega-tags" referenced by ~10% of persons (worst-case bitmap density).

**Topology knobs**: ref fan-out 0–5 notes/person; `manager` chains form org-trees of depth ≤ 6 (ghost-chain traversal); ~0.1% cycles (person↔person) to keep the cyclic-GC path honest. **Scale knob** N ∈ {10k, 100k, 1M}. **Generation target**: ≥ 200k entities/s pure Python (1M ≤ ~10 s) so setup never dominates; generation time itself is asserted ≤ 30 s @1M as a tripwire.

## 3. Harness

**pytest-benchmark**, not a custom timer. Justification: calibration, warmup, GC-disabled timing, IQR outlier rejection, `--benchmark-autosave` JSON history, `--benchmark-compare-fail` — a custom harness re-implements all of that and bitrots; pytest integration means benches share fixtures with the test suite. Two adaptations: (a) **gates are plain `assert`s on same-run ratios** inside one test (two `benchmark.pedantic`-style timed blocks compared directly) — they do not depend on stored JSON; (b) non-timing metrics (RSS bytes, file bytes, computed ratios) are attached via `benchmark.extra_info` so they ride the same JSON history. Subprocess benches (boot, RSS) use a tiny `runpy` child + `resource.getrusage`; the parent test times/records them through the same mechanism.

Local UX (`bench` dependency-group in pyproject; pytest-benchmark + psutil stay out of core deps):

```
uv run --group bench pytest benchmarks -m bench_pr            # fast gate suite
uv run --group bench pytest benchmarks --benchmark-autosave   # full + save history
make bench / make bench-full                                  # aliases of the above
```

Results: `.benchmarks/<machine-id>/*.json` locally (gitignored). CI uploads the JSON as an artifact; nightly appends to a `bench-data` branch consumed by `github-action-benchmark` (dashboard stays repo-internal until the PyPI name is reserved and the repo goes public). Release baselines are committed as `benchmarks/baselines/<tag>.json`.

## 4. CI stability tactics

- **Trend-vs-gate split (the core tactic)**: gates = same-run ratios (engine/floor, big/small corpus, churn/clean), byte budgets, and the two O() proofs (`boot_vs_history`, `watermark_apply_fixed_delta`) — these fail the build. Trends = all absolute ns/ops numbers — `github-action-benchmark` alerts at 130% regression vs rolling median, never fails a PR.
- **Pinned execution**: `benchmark.pedantic(rounds=20, warmup_rounds=5, iterations=pinned)` — no auto-calibration drift between runs; one pinned runner class; pinned CPython 3.14.x patch version (bumped deliberately, with a baseline reset note).
- **GC discipline**: timing with GC disabled (pytest-benchmark default), explicit `gc.collect()` between rounds, `gc.freeze()` after corpus build — which also dogfoods the design's own pause mitigation.
- **Subprocess benches**: 5 repeats; *min* for boot (noise is strictly additive), *median* for RSS.
- **Threshold headroom**: every gate ratio is set ≥ 2× the noise observed over the first two weeks of nightlies before it becomes blocking (gates start as warnings, flip to hard after 14 green nights).
- **Every PR** (`-m bench_pr`, < 2 min): N ≤ 100k only — both O() ratio gates at reduced scale (10k vs 100k), commit tput/latency, hydration pair, bitmap-vs-scan @100k, unique-key @10k/100k, RSS @100k. Store fixtures cached across the job via the deterministic generator key.
- **Nightly** (`-m bench_full`, ~30–45 min): full 1M sweep, 10k-vs-1M O(delta)/O(checkpoint) proofs, fsync p99, snapshot cost, file amplification with churn + housekeeping, trend upload.
- **Release**: nightly suite + `--benchmark-compare-fail=median:20%` against the previous release tag's committed baseline, reviewed by hand before tagging.
