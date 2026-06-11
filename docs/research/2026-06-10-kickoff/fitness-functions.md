# Architectural fitness functions

> Workflow runs wf_41ceea42-869 (resumed), 2026-06-10/11. Summary: Defined 17 prioritized architectural fitness functions for datacrystal, each tied to a ratified decision (ADR-001, ROADMAP, DESIGN amendments, SDA lessons, Never list) with concrete executable mechanisms, thresholds, gate/trend classification, cadence, and failure actions. Named the "M0 five" that must exist before any engine code (pickle-free AST gate, dependency/import isolation, crash-torture harness, ADR-001 conformance spec, replay-determinism/watermark contract spec) and sketched uv-run local commands plus a three-workflow CI wiring (PR gate / nightly deep / release golden-matrix) with complexity-ratio gating to stay stable on shared runners.

# datacrystal — Architectural Fitness Functions (v0 kickoff)

Automated, continuously-evaluated guards on architectural characteristics (Ford/Parsons), derived from the ratified docs. Feature correctness lives in the normal test suite; these exist so the *architecture* cannot silently erode.

## 1. Prioritized catalog

| # | Name | Guards (source) | Mechanism (executable check) | Threshold | Gate/Trend | Cadence | On failure |
|---|---|---|---|---|---|---|---|
| 1 | crash-torture | No partial commit ever visible; three-phase commit; "torn-write/kill -9 fuzzing from v0.1" (ADR-001; DESIGN risk 4) | hypothesis `RuleBasedStateMachine` drives store ops in a child process; fault hook in a `FaultyStorage` wrapper of the 3-method protocol injects `os._exit`/short writes at random byte offsets, plus real `SIGKILL` at randomized commit-phase boundaries; reopen, assert content == last acked watermark, byte-exact | 0 violations (200 examples/PR, 20k nightly) | Gate | PR (small) + nightly (deep) | P0, merge blocked; failing seed committed as regression case |
| 2 | pickle-free | "NO pickle anywhere" promise (DESIGN: codec; ROADMAP item 1) | `ast` walk over `src/datacrystal/**`: forbid `import`/`importlib` of pickle, dill, cloudpickle, shelve, joblib; forbid `marshal.dumps/loads`, `copyreg.pickle`; format fixtures scanned for pickle protocol magic bytes | 0 findings | Gate | every PR | merge blocked |
| 3 | dep-budget / import-isolation | Core deps = msgspec + pyroaring only; extras stay extras (ROADMAP; DESIGN amendment; SDA hard boundaries) | (a) assert `[project.dependencies]` ⊆ {msgspec, pyroaring}; (b) fresh subprocess `import datacrystal`, assert `sys.modules` ∩ {pyarrow, duckdb, polars, usearch, pydantic, numpy, psutil, fastapi, strawberry, sqlite3*} = ∅ (*sqlite3 lazily, only on store open) | 0 leaks | Gate | every PR | merge blocked |
| 4 | ADR-001 conformance | Owner-confinement Option D contract (ADR-001, all six bound sub-decisions) | executable spec suite: foreign-thread read/write/`Lazy.get()`/`commit()` → `WrongThreadError`; `submit()` returning live entity → `EntityEscapeError`; `snapshot()` read from N threads while owner mutates (no tearing, stable watermark); `aopen()` loop binding; `debug=True` keeps hooks armed; writes racing commit-P2 re-dirty into next commit | 100% pass | Gate | every PR | merge blocked; contract change requires ADR amendment |
| 5 | replay determinism | Commit-delta/watermark pipeline is a PUBLIC VERSIONED contract (ROADMAP item 3; SCALING tier 3) | scripted + hypothesis op sequences; run twice (incl. different `PYTHONHASHSEED`): assert byte-identical delta stream, identical store content hash; replay deltas into a reference consumer == direct store state; delta application idempotent (apply twice == once) | byte-identical; idempotent | Gate | every PR | merge blocked; pipeline version bump procedure triggered |
| 6 | golden-file compat | Every release opens stores written by ALL prior releases (DESIGN amendment 7; format hygiene) | `goldens/v0.N/` store dirs committed at each release tag; test opens each with current code, verifies content checksums + reference queries | all goldens open + verify | Gate | every PR (cheap) + full matrix at release | format break: ship migration path or revert; never silent |
| 7 | format-header lock | Versioned header with reserved sealed-flag/footer-offset/tid-watermark fields (ROADMAP item 5) | byte-level test: encode reference record, compare against checked-in hex fixture incl. version byte + reserved fields | exact bytes | Gate | every PR | change requires version increment + fixture bump + golden plan |
| 8 | single-writer lease | Lease lock file, loud error; `--workers 4` is the #1 foreseeable user error (ROADMAP item 2; SCALING tier 1) | spawn 2nd process opening same store dir → clean `LockHeldError` (no hang/corruption); kill holder + age lease → takeover succeeds; N simultaneous openers → exactly one wins | all 3 scenarios pass | Gate | every PR | merge blocked |
| 9 | O(delta) watermark apply | Sidecars apply deltas O(delta), never rebuild-the-world (SDA lesson: per-write FTS rebuild; ROADMAP item 3) | apply a fixed 100-record commit to reference consumer (bitmap index; FTS5 when it lands) against stores of 10³/10⁵/10⁶ entities; fit time vs store size | t(10⁶)/t(10³) ≤ 2× | Gate (ratio) + Trend (absolute) | nightly | P1; consumer not certified on pipeline until fixed |
| 10 | boot O(checkpoint) | Startup O(checkpoint), not O(write history) (DESIGN amendment 5; ROADMAP punt 14 rationale) | stores with identical 10⁴-object live set but 1×/10×/100× update churn; measure `open()` time | t(100×)/t(1×) ≤ 1.5× | Gate (ratio) + Trend | nightly | P1 before next release |
| 11 | memory envelope | ~600 B/live registered object all-in; 1–5M object positioning (DESIGN amendment 5) | load reference dataset (1M mixed entities, Lazy boundaries per docs); RSS delta + tracemalloc ÷ object count | ≤ 600 B/obj | Gate + Trend chart | nightly | regression bisected before release; docs envelope never widened silently |
| 12 | import-time budget | CLI/agent persona: scripts must start fast (DESIGN amendment 1) | p50 of 10 cold-subprocess `python -c "import datacrystal"` runs; `-X importtime` profile archived | ≤ 100 ms CI runner AND ≤ 1.3× stored baseline | Gate + Trend | every PR (timing), nightly (profile) | merge blocked; lazy-import the offender |
| 13 | sidecar rebuildability | Indexes = rebuildable derived data, never txn participants (DESIGN search pillar; SCALING tier 3) | delete sidecar/index files, reopen, rebuild from graph; assert bitmap/FTS query results identical to incrementally-maintained version | exact equality | Gate | every PR | merge blocked |
| 14 | no-N+1 hydration | Batch hydration; "N+1 must never be the user's problem" (SDA delta 5) | counting wrapper on storage protocol: batch-load 500 entities → count read calls | ≤ 4 storage reads | Gate | every PR | merge blocked |
| 15 | steady-state overhead | One-shot `__setattr__`; zero steady-state cost (DESIGN dirty tracking; risk 1) | pytest-benchmark: attr read on Clean entity and 2nd+ write on Dirty entity vs plain slots-dataclass | ≤ 1.05× plain dataclass | Gate (ratio) + Trend | nightly | P1; hook disarm logic audited |
| 16 | ergonomics gate | "hello-world in 4 lines"; public API only (DESIGN MVP; positioning) | README quickstart extracted (pytest-examples) and executed verbatim in clean venv with core-only install; `examples/quickstart.py` ≤ 60 lines, AST check: imports only public `datacrystal` names (no `_internal`) | runs green; ≤ 60 lines | Gate | every PR | merge blocked |
| 17 | wheel purity | Pure-Python-first; Never-list: no Rust in core (ROADMAP Never) | `uv build`; assert wheel tag `py3-none-any`, no `.so`/`.pyd` members | pure wheel | Gate | every PR + release | merge blocked |

## 2. The M0 five (before any engine code lands)

1. **#2 pickle-free** — the no-pickle promise is unrecoverable once the first format byte ships; trivially green at zero code, contamination-proof from commit one.
2. **#3 dep-budget / import-isolation** — dependency creep is a one-way door; the budget must be enforced before the first convenient `import numpy` appears.
3. **#1 crash-torture harness** — the buffer-until-commit storer must be *born inside* this harness (DESIGN: "from day one"); the fault-injection wrapper also forces the 3-method storage protocol to exist first.
4. **#4 ADR-001 conformance suite** — written as the executable spec of the accepted contract, so the engine API is shaped by failing tests instead of retrofitted guards.
5. **#5 replay determinism / watermark contract** — ROADMAP: semantics "locked before any consumer ships"; the most load-bearing undelivered component gets its contract test before its implementation.

## 3. How they run

**Layout:** all fitness functions live in `tests/fitness/` as pytest (static AST checks included, so one runner does everything), markers `gate`, `trend`, `slow`; shared fault-injection harness (`FaultyStorage`, child-process runner) in `tests/fitness/_harness.py` — deliberately reusable by extension packages as pipeline-consumer certification.

**Local:**
- `uv run pytest tests/fitness -m gate` — full PR-gate set (#1-small, 2–8, 12–14, 16, 17), target < 3 min.
- `uv run pytest tests/fitness -m "slow or trend" --hypothesis-profile=nightly` — deep torture (20k examples), perf fits, memory envelope.
- `uv run python -X importtime -c "import datacrystal"` — ad-hoc import budget drill-down.

**CI (GitHub Actions):**
- `pr.yml` (ubuntu + macos, py3.14): the `-m gate` set. Cheap on every PR: #2, 3, 6, 7, 8, 12, 13, 14, 16, 17 are sub-second-to-seconds; #1 runs the 200-example profile; #4, 5 are plain unit-speed suites.
- `nightly.yml` (cron): #1 deep, #9, 10, 11, 15; perf checks **gate on size-scaling ratios within one run** (slope/ratio, machine-independent) and only **trend** absolute wall times — results appended to `benchmarks/history.jsonl`, alert at ≥ 20% drift vs 7-day median. Avoids flaky absolute thresholds on shared runners.
- `release.yml` (tag): full golden matrix across all prior `goldens/v0.*`, then writes + commits the new release's golden store; wheel-purity audit; publish blocked until PyPI names `datacrystal`/`data-crystal` are reserved.

Threshold changes to any gate require a PR touching both the threshold and the doc it cites — fitness functions are part of the public contract surface, versioned with it.
