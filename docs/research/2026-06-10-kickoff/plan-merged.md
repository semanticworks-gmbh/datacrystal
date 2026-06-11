# Merged kickoff plan (judge)

> Workflow runs wf_41ceea42-869 (resumed), 2026-06-10/11. Summary: Judged the three datacrystal v0.1 kickoff plans against the ratified docs: tracer wins (23/25) on time-to-runnable and risk front-loading, dogfood second (19) on ergonomics, contract third (18) on churn protection. Produced a merged plan on the tracer spine, grafting contract's executable-contract discipline (engine-free delta package, byte-pinned golden fixtures, evil-twin conformance, fitness tests) and dogfood's executable-docs loop (sybil README, journal example as SDA-delta acceptance gate), with six explicit conflict rulings — notably draft-the-delta-contract-in-M2/lock-at-M3, and classifying M1's eager commit as the ratified opt-in eager storer so ADR-001's three-phase clause is never violated.

# Plan panel — datacrystal v0.1 kickoff: verdict + merged plan

## Scoring matrix

| Criterion | tracer | contract | dogfood |
|---|---|---|---|
| Fidelity to ratified docs | 5 | 5 | 4 |
| Time-to-first-runnable | 5 | 2 | 3 |
| Risk front-loading | 5 | 4 | 3 |
| Contracts churn-protected | 4 | 5 | 4 |
| Ergonomics loop | 4 | 2 | 5 |
| **Total** | **23** | **18** | **19** |

**tracer** — F5: all ratified items correctly placed (the M1 eager commit is legal — see ruling 3). T5: kill -9 → reopen demo CI-gated from day ~5, never removed. R5: crash consistency, dirty machine, flip window, confinement, lock, torture all inside two weeks. C4: spec precedes consumers, but records ship weeks before the delta spec. E4: demo grows each milestone, but synthetic; no executable docs.

**contract** — F5: literally executes ROADMAP item 3's "locked before any consumer ships"; every SDA delta placed. T2: two weeks of fixtures/fakes; real persist-restart only at M3 (~day 18). R4: best on contract-frozen-wrong, but the #1-ranked risk (dirty tracking) waits to day 11, torture to week 4. C5: byte-pinned fixtures, version-bump-or-fail CI, evil twins, engine-free contract package — strongest armor. E2: example/docs last; API emerges from conformance tests, not users' hands.

**dogfood** — F4: every SDA delta covered, but the watermark contract slides to M4 (latest) and "aopen() polish → v0.2" thins ADR-001 loop-binding in v0.1. T3: failing tests day 3, first passing README block day 10, restart-restore at M3. R3: day-5 API freeze before implementation creates churn risk instead of retiring it; torture and contract late. C4: README-as-test makes API changes reviewed docs diffs, but the wire contract is defined beside its consumers. E5: sybil README, ≤25-line quickstart, journal exercising every SDA delta — docs cannot rot.

**Winner: tracer (23) over dogfood (19) and contract (18)** — it front-loads the register's top risks (dirty tracking, crash consistency) on a permanently CI-gated spine while still sequencing the public contract before any consumer.

## Conflicts resolved

1. **Spec-first (contract) vs engine-first (tracer).** Ruling: COMMIT-DELTA-v1 is *drafted* during M2 (spec + engine-free reference applier + replay vectors — contract's discipline) and *locked at M3 exit, before any consumer ships* (ROADMAP item 3's letter); the real engine emits deltas pre-lock. Kills both failure modes: contract-frozen-wrong and spec-tested-only-on-a-fake.
2. **API freeze day 5 (dogfood) vs never (tracer).** Ruling: no pre-implementation freeze; brakes = API-surface-snapshot test [contract] + README-as-docs-diff [dogfood]; the freeze is the v0.1.0 tag.
3. **M1 eager commit vs ADR-001 "three-phase from version one".** Ruling: the clause binds the buffer-until-commit storer, which only ever exists three-phase (M2); M1's eager commit() is the ratified opt-in eager storer, kept forever.
4. **First pipeline consumer: bitmaps (contract) vs snapshot (tracer).** Ruling: snapshot first (ADR-001 rider 2, simplest consumer), bitmaps second (M4), datacrystal[fts] stays the late-v0.x external validator (SDA delta).
5. **Dirty hook: spike (contract/dogfood) vs committed (tracer).** Ruling: the tri-state one-shot setattr hook is ratified scope — only class-swap *ghosts* are deferred. Build day 7; the kill criterion applies to the mechanism (swap vs permanently-armed hook + fingerprint diff), never the feature.
6. **Demo identity: kill-demo (tracer) vs journal (dogfood).** Ruling: both — demo.py gates durability from week 1; journal gates ergonomics + SDA deltas from M2.

# Merged plan (tracer spine; grafts tagged [contract]/[dogfood])

## 1. Definition of first runnable v0.1

- git clone && uv sync && uv run demo: @entity slots-dataclasses, cyclic graph with Lazy[T], commit, SIGKILL (--crash), reopen → graph restored, identity preserved (a.friend is b), never torn.
- README quickstart (≤25 lines) runs under sybil in CI; examples/journal runs twice (run 2 finds run 1's data), exercising every SDA delta: unique slugs, frozen Event log (mutation raises), tag queries, get_many backlinks, Lazy[T] attachments, snapshot() report from a worker thread [dogfood].
- Foreign-thread access → WrongThreadError (message embeds the submit/snapshot recipe [dogfood]); store.submit(fn) → Future, live-entity escape → EntityEscapeError; store.snapshot() readable from any thread during owner commits; second process → loud StoreLockedError.
- store.query((Person.city == "Berlin") & (Person.age >= 18)) via pyroaring bitmaps + Condition AST; unique secondary-key index rejects duplicates.
- uv run pytest green incl. kill-9 torture (reopen always an exact commit prefix), conformance on byte-pinned fixtures [contract], fitness (deps exactly {msgspec, pyroaring}) [contract]. Both PyPI names ours; 0.1.0 published at tag.

## 2. Milestones (~26.5 dev-days)

**M0 — bootstrap, names, fitness (1.5 d).** Reserve PyPI datacrystal + data-crystal (blocking); src layout; CI (3.14, ubuntu+macos [contract]): lint → pyright strict → tests → demo → bench; _ids.py 64-bit TID/OID/CID partitions; _errors.py; header-v1 constants with reserved sealed-flag/footer-offset/tid-watermark fields; fitness skeleton (dep allowlist, API snapshot, import-time, dep import probe) [contract]; sybil + quickstart stub xfail [dogfood]. Exit: CI green; placeholders live.

**M1 — tracer bullet: persist → kill → restore (5 d).** @entity (slots + weakref_slot, Annotated markers, frozen=True); WeakValueDictionary OID registry + stamping; msgspec-msgpack records (versioned header, checksums, ref→OID swizzling); JSON-lines type dictionary; 3-method storage protocol with SQLite-blob backend + in-memory fake; Store.open()/root/store()/commit() (eager, ruling 3); object.__new__ hydration; explicit Lazy[T]; owner binding + WrongThreadError. Exit: SIGKILL+reopen demo green in CI; hypothesis cyclic round-trips on both backends; first README block passes [dogfood].

**M2 — dirty tracking, three-phase commit, confinement, lock (7.5 d).** Tri-state Ghost/Clean/Dirty one-shot setattr hook; buffer-until-commit storer in ADR-001 three-phase shape from version one (P1 on-owner capture+encode; P2 off-owner I/O on bytes; P3 on-owner flip+re-arm+watermark; racing writes re-dirty); store.submit() + EntityEscapeError; lease lock file; LazyReferenceManager as owner task/sentinel-post; aopen() + store.transaction(); fsync policy; get_many(); COMMIT-DELTA-v1 draft + engine-free contract/ applier + replay vectors [contract]. Exit: torture + stateful dirty machine green; two-process lock test; async demo; journal starts passing [dogfood].

**M3 — watermark pipeline locked as PUBLIC contract + snapshots (6 d).** Commit-delta schema locked (idempotency, ordering, replay-from-watermark); golden fixtures byte-pinned, CI fails on byte change without version bump [contract]; conformance kit (datacrystal.testing) with evil twins that must fail [contract]; store.snapshot() (frozen-DTO + frozen-bitmap views at watermarks) as first in-tree consumer + toy counter. Exit: kit green (apply-twice ≡ apply-once; crash-mid-apply replays); snapshot reads from a thread pool during owner commits.

**M4 — queries: bitmaps + Condition AST + unique index (6.5 d).** Annotated[…, dc.Index] pyroaring bitmaps as second commit-delta consumer; Condition AST (contains/startswith iterate distinct keys, never entities); hydration via get_many(); unique secondary-key index (upsert-by-natural-key, duplicate raises); journal + README fully green as acceptance gate [dogfood]. Exit: hypothesis query-vs-brute-force oracle; indexes rebuildable after kill-9; budgets hold → tag + publish v0.1.0.

Post-v0.1 (still v0.x): schema evolution; datacrystal[fts] as the contract's external validation harness; docs (workers=1 + asyncio doctrine).

## 3. First two weeks

1. **D1 (M0):** pyproject; CI; PyPI placeholders; _ids.py, _errors.py + tests; stub demo; benchmarks/.
2. **D2:** fitness suite [contract]; sybil + quickstart xfail [dogfood]; start _entity.py.
3. **D3:** finish _entity.py (+frozen=True) + tests.
4. **D4:** _records.py (header v1, codec, swizzle, checksums); _typedict.py; property round-trips.
5. **D5:** _storage/{protocol,sqlite,memory}.py; parametrized tests over both.
6. **D6:** _registry.py; _lazy.py; _store.py (eager); examples/demo.py --crash; SIGKILL+reopen integration test. Tag walking-skeleton (M1); quickstart passes [dogfood].
7. **D7:** _dirty.py + stateful hypothesis machine.
8. **D8:** _storer.py three-phase commit; re-dirty-during-P2 test; COMMIT-DELTA-v1.md draft + contract/delta.py [contract].
9. **D9:** _owner.py guards, submit(), EntityEscapeError (recipe messages [dogfood]); confinement tests; replay vectors [contract].
10. **D10:** _storage/lock.py lease lock + two-process test; kill-9 torture harness; bench baselines.

## 4. Repo layout

```
src/datacrystal/
  __init__.py  _errors.py  _ids.py  _entity.py
  _records.py  _typedict.py  _registry.py  _lazy.py
  _dirty.py    # tri-state hook; PersistentList/Dict
  _storer.py   # buffer-until-commit, three-phase
  _owner.py    # binding, guards, transaction()
  _store.py    # facade: root/commit/submit/snapshot/query/get_many
  _storage/    # protocol, sqlite, memory, lock
  contract/    # PUBLIC engine-free delta structs/applier/replay [contract]
  _pipeline/   # watermark, snapshot (engine side)
  _index/      # bitmap, conditions, unique
  testing/     # conformance kit for Tier-3 consumers
tests/       # unit property integration crash contract fitness acceptance
benchmarks/  examples/{demo.py,journal/} [dogfood]
docs/spec/commit-deltas-v1.md  README.md  # executable via sybil [dogfood]
```

## 5. Test strategy

- **Unit:** pytest + pytest-asyncio + pytest-timeout everywhere [contract]; storage tests parametrized over SQLite + memory fake; ruff + pyright strict per PR.
- **Executable docs [dogfood]:** sybil runs every fenced README block; journal runs twice as a subprocess test — API changes are docs diffs first.
- **Property-based:** hypothesis cyclic graphs round-trip with is-identity; codec invariants; query-vs-brute-force oracle; RuleBasedStateMachine dirty/commit machine vs dict oracle.
- **Crash-torture:** fault injection (P2/P3, commit→sidecar gap); random SIGKILL; reopen is an exact commit prefix, checksums clean, indexes rebuildable; lock contention. Short per PR, long nightly.
- **Contract-conformance:** byte-pinned fixtures, version-bump-or-fail; evil-twin stores must fail each section [contract]; idempotent re-apply, ordering, replay-from-watermark via datacrystal.testing — snapshot + toy consumer now, FTS later.
- **Fitness:** dep allowlist, API snapshot, import-time [contract]; pytest-benchmark + tracemalloc (attr read == plain dataclass, hook ≲150 ns, ~600 B/object); regressions fail CI.

## 6. Top 5 risks

1. **Dirty-tracking gaps silently lose writes** (#1 DX killer in both ancestors). Stateful hypothesis machine from D7; fingerprint-diff safety net; debug=True warns on untracked mutation; PersistentList/Dict.
2. **Three-phase flip-window bugs** (ADR-001 race b). Storer three-phase from version one; deterministic fault injection + kill-9 torture in CI from M2.
3. **Watermark contract ships wrong — public, versioned, forever.** Draft-in-M2/lock-at-M3 (ruling 1); engine-free applier + replay vectors + byte-pinned fixtures [contract]; snapshot dogfoods in-tree; FTS validates externally before any "stable" stamp; version field from record one.
4. **Registry identity leaks / premature collection.** @entity enforces weakref_slot=True; gc-stress + hypothesis identity properties; weakref.finalize fallback.
5. **Spine rot / scope creep / API churn.** Demo + journal as required CI jobs and release gates; README-as-docs-diff [dogfood]; API-surface snapshot [contract]; protocol growth requires an ADR; punt/Never list checked in review.
