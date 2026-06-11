# Kickoff plan — tracer angle

> Workflow runs wf_41ceea42-869 (resumed), 2026-06-10/11. Summary: Kickoff plan for datacrystal's first runnable version using a tracer-bullet/walking-skeleton approach, grounded in the ratified ROADMAP, ADR-001, DESIGN, SDA-LAYERING, and SCALING docs. Defines v0.1 as M0-M4 (~25.5 ideal dev-days): M0 reserves both PyPI names and stands up the CI/fitness harness; M1 delivers the end-to-end persist-kill-restore spine with a CI-required demo by day 5; M2 adds tri-state dirty tracking, ADR-001 three-phase commit, owner confinement, and the lease lock; M3 ships the versioned public commit-delta/watermark contract with snapshots as first consumer; M4 adds pyroaring bitmap queries, the Condition AST, and the unique secondary-key index. Includes a file-level two-week task list, src/datacrystal module tree, a four-layer test strategy (pytest, hypothesis, kill-9 crash-torture, contract-conformance kit, pytest-benchmark fitness functions), and top-5 risks with mitigations — all within ratified v0.x scope, 8999 characters.

# datacrystal — kickoff: first runnable version (tracer bullet)

The spine: `@entity` → `commit()` → `kill -9` → reopen → graph restored. Every milestone thickens it; the demo runs in CI from day 5 on.

## Definition of first runnable v0.1

v0.1 = M0–M4 (ROADMAP items 1, 2, 3-minimal, 4, 5 + SDA deltas; FTS later in v0.x). Verify by:

- `git clone && uv sync && uv run demo` — `@entity` slots-dataclasses, cyclic graph with `Lazy[T]` refs, commit, self-kill, relaunch: graph restored, identity preserved (`a.friend is b`), Lazy deferred until `.get()`. `--crash` SIGKILLs mid-commit; reopen yields the last committed state, never torn.
- `uv run pytest` green incl. crash-torture (random kill -9 → reopen always an exact commit prefix) and watermark conformance. Two concurrent demo runs → loud `StoreLockedError`.
- Foreign-thread access → `WrongThreadError`; `store.submit(fn)` → Future (returned live entity → `EntityEscapeError`); `store.snapshot()` readable from any thread during owner commits.
- `store.query((Person.city == "Berlin") & (Person.age >= 18))` returns live entities via bitmaps; unique secondary-key index upserts by natural key and rejects duplicates; `get_many(oids)` batch-hydrates; mutating `@entity(frozen=True)` raises.
- `uv run pytest benchmarks` — fitness budgets hold; runtime deps exactly msgspec + pyroaring (sqlite3 stdlib); both PyPI names ours.

## Milestones

**M0 — bootstrap, names, fitness harness (1.5 d).** In: reserve PyPI **`datacrystal` + `data-crystal`** (0.0.1.dev0 placeholders) — blocking; src layout; runtime deps pinned (msgspec, pyroaring); dev-deps per test strategy; CI (3.14): lint → typecheck → tests → demo smoke → fitness benchmarks; `_ids.py` partitioned 64-bit TID/OID/CID allocator; `_errors.py` taxonomy; header-v1 constants incl. reserved fields (sealed-flag/footer-offset/tid-watermark). Out: persistence logic. Exit: CI green; placeholders on PyPI; stub demo runs.

**M1 — tracer bullet: persist → kill → restore (5 d).** In: `@entity` (applies `dataclass(slots=True, weakref_slot=True)` if absent; harvests `Annotated` markers); WeakValueDictionary OID registry + stamping; msgspec-msgpack records (versioned header, checksums, ref→OID swizzling); JSON-lines type dictionary; 3-method storage protocol with SQLite-blob backend **and an in-memory fake**; `Store.open()/root/store()/commit()` (eager); `object.__new__` hydration; explicit `Lazy[T]`; owner binding + `WrongThreadError` guards. Out: dirty tracking, lock file, queries, snapshots, class-swap ghosts (deferred optimization). Exit: demo (SIGKILL+reopen) green in CI; hypothesis cyclic-graph round-trips on both backends.

**M2 — dirty tracking, three-phase commit, confinement, lock (7 d).** In: tri-state Ghost/Clean/Dirty one-shot `__setattr__` hook; buffer-until-commit storer **in the ADR-001 three-phase shape from version one** (P1 capture+encode on-owner; P2 I/O on bytes off-owner; P3 flip+re-arm+watermark on-owner; racing writes re-dirty); `store.submit()` + `EntityEscapeError`; `debug=True` always-armed hooks; lease-refreshed lock file; LazyReferenceManager as owner task / sentinel-post; `aopen()` + `store.transaction()`; fsync policy; `@entity(frozen=True)`; `store.get_many()`. Out: queries, snapshots, FTS. Exit: kill-9 torture + stateful dirty machine green; two-process lock test; foreign-thread tests; async demo.

**M3 — watermark pipeline as PUBLIC contract + snapshots (6 d).** In: versioned commit-delta schema; idempotency + ordering + replay-from-watermark locked and documented; conformance kit (`datacrystal.testing`); `store.snapshot()` (frozen-DTO reads + frozen bitmaps at watermarks, any-thread safe) as **first in-tree consumer**, plus a toy counter. Out: FTS (late v0.x, the contract's second validator), Arrow mirrors (v1). Exit: kit green (apply-twice ≡ apply-once; ordering; crash-mid-apply replays); snapshot reads from a thread pool during owner commits.

**M4 — queries: bitmaps + Condition AST + unique index (6 d).** In: `Annotated[…, dc.Index]` pyroaring bitmaps as a commit-delta consumer; Condition AST via operator overloading (contains/startswith iterate distinct keys, never entities); hits hydrate via `get_many()`; unique secondary-key index (alias → entity, upsert-by-natural-key); demo gains queries. Out: Arrow/DuckDB, reverse-ref index. Exit: hypothesis oracle (query ≡ brute-force filter); unique violation raises; indexes rebuildable after kill-9; query budget recorded.

Post-M4 → tag v0.1; then, still v0.x: schema evolution, `datacrystal[fts]` as pipeline validation harness, docs (workers=1 + asyncio doctrine).

## First two weeks

1. **Day 1 (M0):** `pyproject.toml`; `.github/workflows/ci.yml`; PyPI placeholders; `src/datacrystal/{__init__,_ids,_errors}.py` + tests; stub demo; `benchmarks/`.
2. **Day 2:** `_entity.py` + `tests/unit/test_entity.py`.
3. **Day 3:** `_records.py` (header v1, codec, swizzle, checksums); `_typedict.py`; unit + `tests/property/test_roundtrip.py`.
4. **Day 4:** `_storage/protocol.py`, `sqlite.py`, `memory.py`; `tests/unit/test_storage.py` over both.
5. **Day 5:** `_registry.py`; `_lazy.py`; `_store.py` (eager); real `examples/demo.py` (+ `--crash`); `tests/integration/test_tracer.py` (SIGKILL+reopen). **Tag `walking-skeleton`: M1 done.**
6. **Day 6:** `_dirty.py` + unit tests + `tests/property/test_dirty_machine.py`.
7. **Day 7:** `_storer.py` three-phase commit; rewire `_store.py`; re-dirty-during-P2 test.
8. **Day 8:** `_owner.py` guards, `store.submit()`, `EntityEscapeError`; `tests/unit/test_confinement.py`.
9. **Day 9:** `_storage/lock.py` lease lock; `tests/crash/test_lock_contention.py`; fsync policy.
10. **Day 10:** `tests/crash/test_kill9.py` torture harness (env-var fault injection); `bench_attr.py`, `bench_commit.py` baselines; buffer.

## Repo layout

```
src/datacrystal/
  __init__.py   # public API
  _errors.py    # error taxonomy
  _ids.py       # 64-bit TID/OID/CID allocator
  _entity.py    # @entity (+frozen=True), marker harvest
  _records.py   # header v1, msgspec codec, swizzle, checksums
  _typedict.py  # JSON-lines type dictionary
  _registry.py  # WeakValueDictionary registry, hydration
  _lazy.py      # Lazy[T] + LazyReferenceManager
  _dirty.py     # tri-state hook; PersistentList/Dict
  _storer.py    # buffer-until-commit, three-phase commit
  _owner.py     # owner binding, guards, transaction()
  _store.py     # facade: root/commit/submit/snapshot/query/get_many
  _storage/     # protocol.py (3 methods), sqlite.py, memory.py, lock.py
  _pipeline/    # deltas.py (PUBLIC contract), watermark.py, snapshot.py
  _index/       # bitmap.py, conditions.py, unique.py
  testing/      # contract-conformance kit
tests/          # unit/ property/ integration/ crash/ contract/
benchmarks/     # bench_attr, bench_commit, bench_query, bench_memory
examples/       # demo.py (tracer bullet, CI-required), demo_async.py
docs/spec/commit-deltas-v1.md
```

## Test strategy

- **Unit (pytest, pytest-asyncio):** per-module; storage tests parametrized over SQLite + in-memory fake; ruff + pyright strict gate every PR.
- **Property-based (hypothesis):** random graphs (cycles, Lazy edges) → store/reopen/compare incl. `is`-identity; codec invariants; query-vs-brute-force oracle; `RuleBasedStateMachine` dirty/commit machine vs dict oracle.
- **Crash-torture (pytest + subprocess):** fault-injection points (around P2/P3, commit→sidecar gap); random SIGKILL; reopen yields an exact commit prefix, checksums clean, indexes rebuildable; lock contention. Short per PR, long nightly.
- **Contract-conformance (`datacrystal.testing`):** idempotent re-apply, ordering, replay-from-watermark, rebuild-equivalence; against snapshots + toy consumer now, FTS later; shipped so Tier-3 consumers self-certify.
- **Fitness (pytest-benchmark + tracemalloc):** attr read == plain dataclass; first-write hook ≲150 ns; ~600 B/live object; commit latency. Regressions fail CI.

## Top 5 risks

1. **Dirty-tracking gaps silently lose writes** (the #1 DX killer in both ancestors). Mitigation: stateful hypothesis machine from day 6; fingerprint-diff safety net; `debug=True` warns on untracked mutation; PersistentList/Dict.
2. **Three-phase flip-window bugs** (ADR race b). Mitigation: storer in three-phase shape from version one; deterministic fault injection + kill-9 torture in CI from M2.
3. **Watermark contract ships wrong** — public, versioned, forever. Mitigation: spec + conformance kit before any consumer; snapshots dogfood it in-tree; FTS validates before stamping stable; version field from record one.
4. **Registry identity leaks / premature collection** (WeakValueDictionary/`weakref_slot` edge cases). Mitigation: `@entity` enforces `weakref_slot=True`; gc-stress tests; hypothesis identity properties; `weakref.finalize` fallback.
5. **Spine rot / seam erosion / scope creep.** Mitigation: demo as required CI job and release gate; in-memory fake in storage tests; protocol growth requires an ADR; punt/Never list checked in review.
