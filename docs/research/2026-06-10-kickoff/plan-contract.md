# Kickoff plan — contract angle

> Workflow runs wf_41ceea42-869 (resumed), 2026-06-10/11. Summary: Kickoff plan for datacrystal v0.1 built around the contract-first angle: M1 turns the record format, commit-delta/watermark schema, and ADR-001 semantics into versioned golden fixtures, replay vectors, and an executable conformance suite before any engine code; M2-M4 then implement the engine (in-memory core, SQLite blob store with three-phase commit and crash torture, pyroaring queries plus snapshots) until conformance passes. M0 reserves PyPI datacrystal + data-crystal and stands up the fitness/CI harness. Total estimate is roughly 26 ideal dev-days across five milestones, with a file-level two-week task list, repo layout, four-tier test strategy (pytest/hypothesis/torture/conformance), and five mitigated risks — all inside ratified v0.x scope and the accepted SDA deltas. Deliverable is 8,999 characters, within the 9,000 budget.

# datacrystal v0.1 kickoff — contract first

Contracts become versioned executable fixtures/tests BEFORE engine code; the engine is built until conformance passes. Scope = ROADMAP items 1–6 + SDA deltas.

## 1. Definition of first runnable v0.1

- `pip install datacrystal==0.1.0` works; runtime deps exactly msgspec + pyroaring (stdlib sqlite3); pure Python 3.14.
- `python examples/agent_memory/app.py` twice: run 1 stores an `@entity` graph and commits; run 2 reopens and prints identical data.
- Running it while instance 1 holds the store fails loudly: lease-lock single-writer error.
- `(Note.kind == "episodic") & (Note.score >= 3)` Condition AST answered from pyroaring bitmaps; unique secondary-key index lookup + upsert-by-natural-key.
- `Lazy[T]` stays cold until `.get()`; `store.get_many(oids)` batch-hydrates (no N+1); `@entity(frozen=True)` round-trips, mutation raises.
- Foreign thread: live-graph access raises `WrongThreadError`; `store.submit(fn)` returns a Future (escaping live entity → `EntityEscapeError`); `store.snapshot()` readable from any thread at a stable watermark.
- `pytest tests/{conformance,contracts,torture}` green: ADR-001 contract tests, golden fixtures + replay vectors, kill -9/fault-injection.
- `python -c "import datacrystal.contract"` works with no store — sidecars/indexers (late-v0.x FTS first) build against shipped contracts + fixtures before the engine exists.

## 2. Milestones

**M0 — bootstrap, names, fitness harness (1.5 dev-days).** In: reserve PyPI **`datacrystal` + `data-crystal`** (0.0.1a0 placeholder + redirect stub); dev-deps (pytest, hypothesis, ruff, pyright strict, pytest-benchmark, pytest-timeout); CI (3.14, ubuntu+macos); fitness skeleton: dependency-allowlist test, API-surface snapshot, import-time budget, msgspec+pyroaring import probe. Out: engine code. Exit: names owned; CI green; `pytest tests/fitness` passes.

**M1 — contracts as executable artifacts (5 dev-days).** In: (a) `RECORD-FORMAT-v1.md` + `contract/records.py` — msgpack record structs, versioned header with reserved sealed-flag/footer-offset/tid-watermark fields, partitioned TID/OID/CID constants, golden fixtures + generator; (b) `COMMIT-DELTA-v1.md` + `contract/delta.py` — CommitDelta (watermark, ordered upsert/delete by OID, index deltas), idempotency/ordering rules, reference applier, replay vectors (duplicates, crash-resume, gaps); (c) ADR-001 conformance suite against a `StoreLike` protocol + in-memory fake. Out: storage, real store. Exit: fixtures byte-stable; vectors pass on reference applier; conformance passes on fake (xfail without engine); v1-draft field names frozen.

**M2 — object engine core (7 dev-days).** In: `@entity` (slots+weakref_slot, `frozen=True`, Annotated markers); WeakValueDictionary OID registry; tri-state Ghost/Clean/Dirty one-shot `__setattr__` (spike; kill criterion → fingerprint-diff); `Lazy[T]`; owner binding at `open()`/`aopen()`, `store.submit`; encode-to-contract-records; buffer-until-commit storer in three-phase shape; memory backend. Out: SQLite, lock, queries. Exit: identity/dirty/frozen/confinement/submit sections green on engine; stateful dirty machine green.

**M3 — durability: SQLite blob store + 3-phase commit + torture (6 dev-days).** In: 3-method storage protocol; `SQLiteBlobStorage`; lease-refreshed lock file; P1 on-owner capture+encode / P2 off-loop I/O on bytes / P3 on-owner flip+re-arm+watermark bump (writes racing P2 re-dirty); commits emit contract-valid CommitDeltas; restart-restore; `get_many`; torture harness. Out: queries, snapshots, FTS. Exit: restart works; torture green; second process gets lock error; emitted deltas validate against the M1 schema and replay identically.

**M4 — queries, snapshots, release (7 dev-days).** In: pyroaring bitmap indexes as the FIRST internal consumer of the commit-delta pipeline; Condition AST; unique secondary-key index; `store.snapshot()` frozen-DTO + frozen-bitmap views; LazyReferenceManager as owner task/sentinel-post; demo; docs (workers=1 + asyncio doctrine); benchmark baselines; publish 0.1.0. Out (per ROADMAP): `datacrystal[fts]` (late v0.x, after 0.1 hardens the contract), `store.transaction()` rider, Arrow mirrors, reverse-ref index, custom log, Rust. Exit: all suites green; example runs per §1; `pip install datacrystal==0.1.0`.

Total ≈ 26 dev-days.

## 3. First two weeks

1. D1: reserve PyPI (`datacrystal` 0.0.1a0 + `data-crystal` stub); `pyproject.toml` dev group, ruff/pyright config; `.github/workflows/ci.yml`.
2. D2: `src/datacrystal/__init__.py`; `_ids.py` (ID partitions + `classify()`); `tests/fitness/test_deps_allowlist.py`, `test_api_surface.py`, `test_import_time.py`.
3. D3–4: `docs/spec/RECORD-FORMAT-v1.md`; `contract/records.py`; `scripts/gen_fixtures.py` → `tests/contracts/fixtures/v1/`; `test_record_fixtures.py`.
4. D5–6: `docs/spec/COMMIT-DELTA-v1.md`; `contract/delta.py` + `replay.py`; `tests/contracts/vectors/v1/*.json`; `test_replay_vectors.py`.
5. D7: `tests/conformance/conftest.py` (store_factory: fake now, engine later); `datacrystal/_testing/fake_store.py`.
6. D8–9: `tests/conformance/test_confinement.py` (foreign-thread matrix, submit, escape, `aopen` loop binding); `test_commit_phases.py` (P1/P2/P3 hooks; write-racing-P2 re-dirties); `test_snapshot.py` (watermark stability, any-thread reads).
7. D10: `test_identity.py`, `test_dirty.py`, `test_frozen.py`, `test_batch.py`; `tests/strategies.py`; contract review — freeze v1-draft, tag.
8. D11–12: M2 spike — `dirty.py` one-shot `__setattr__` on slots/frozen/inherited entities; go/no-go vs fingerprint-diff fallback.
9. D12–14: `entity.py`, `registry.py`, `errors.py`, `lazy.py`; flip identity/dirty/frozen conformance from fake-only to engine.

## 4. Repo layout

```
src/datacrystal/
  __init__.py        # public API: entity, open/aopen, Lazy, markers, errors
  errors.py          # WrongThreadError, EntityEscapeError, StoreLockedError
  _ids.py            # partitioned 64-bit TID/OID/CID space
  contract/          # PUBLIC: records.py, delta.py, replay.py (engine-free)
  entity.py          # @entity (incl. frozen=True), Annotated markers
  registry.py        # WeakValueDictionary OID registry
  dirty.py           # tri-state Ghost/Clean/Dirty hook
  lazy.py            # Lazy[T] + LazyReferenceManager (owner task)
  store.py           # submit, snapshot, get_many, 3-phase commit
  snapshot.py        # frozen watermark views
  storage/           # protocol.py, sqlite_blob.py, memory.py, lock.py (lease)
  index/             # bitmap.py (delta consumer), unique.py, conditions.py
  _testing/          # fake_store.py, fault-injection storage wrapper
tests/
  fitness/  contracts/{fixtures/v1,vectors/v1}  conformance/  unit/
  torture/  strategies.py
benchmarks/          # commit, query, hydration baselines
examples/agent_memory/  # frozen episodic log + queries + snapshot
docs/spec/           # RECORD-FORMAT-v1.md, COMMIT-DELTA-v1.md, CONFORMANCE.md
scripts/gen_fixtures.py
```

## 5. Test strategy

- **Unit**: pytest, pytest-timeout everywhere (deadlocks fail fast).
- **Property-based**: hypothesis — record round-trips; dirty tracking as `RuleBasedStateMachine`; replay convergence (redundant delta delivery yields identical state); query equivalence (Condition AST == brute-force scan).
- **Contract conformance**: golden fixtures pinned in git — byte change without version bump fails CI; one suite parametrized over every `StoreLike` (fake + engine); "evil twin" broken stores must FAIL each section; fixtures ship in the wheel.
- **Crash-torture**: subprocess kill -9 at random commit points; fault-injection wrapper (hypothesis picks the failing I/O step); post-crash open lands on a committed watermark, never a partial commit; lagging index forces rebuild.
- **Fitness/CI**: ruff, pyright strict, dependency allowlist, API-surface snapshot, import-time budget, pytest-benchmark baselines (tracked, not gating).

## 6. Top 5 risks

1. **Contract frozen wrong, unbreakable after v0.1.** Version + reserved fields everywhere; additive-evolution policy in the specs; contracts draft until M4 exit; versioned fixture dirs.
2. **Dirty-tracking hook breaks on slots/frozen/inheritance edges.** M2-start spike with kill criterion; ratified fallback = msgspec-fingerprint diff at commit; hypothesis stateful machine; `debug=True` keeps hooks armed and warns.
3. **Crash-consistency distrust.** v0.x rides SQLite's proven transactionality (no custom log); torture gates every merge from M3; recovery watermark-driven; indexes rebuildable, so only the blob store must be perfect.
4. **Vacuous conformance suite (tests the fake, not reality).** Suite parametrized over fake AND engine; evil-twin mutants must fail; observability hooks are public API — tests observe real P1/P2/P3.
5. **3.14 dependency drift (pyroaring small-maintainer wheels, msgspec).** M0 fitness job imports both on every CI target; pinned minimums; bitmap index behind an internal protocol with a pure-Python set fallback for tests.
