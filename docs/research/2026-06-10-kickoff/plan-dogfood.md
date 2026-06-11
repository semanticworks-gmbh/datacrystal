# Kickoff plan — dogfood angle

> Workflow runs wf_41ceea42-869 (resumed), 2026-06-10/11. Summary: Designed the example-driven kickoff plan for datacrystal v0.1: the README quickstart and an examples/journal demo (covering all SDA deltas — unique secondary keys, frozen append-only entities, batch hydration — as natural features) are written first as failing sybil-executed acceptance tests, then five milestones (M0 bootstrap incl. PyPI name reservation + fitness-function CI, M1 executable spec, M2 in-memory object engine, M3 SQLite durability with three-phase commit and lease lock, M4 queries + watermark contract + snapshots) deliver v0.1 in ~29 ideal dev-days. Includes a file-level two-week task list, src/datacrystal module tree, a five-layer test strategy (sybil, pytest, hypothesis, crash-torture, contract-conformance), and top-5 risks with mitigations — all within ratified v0.x scope per ROADMAP.md, ADR-001, and SDA-LAYERING.md. Deliverable is 8,987 characters, under the 9,000 limit.

# datacrystal v0.1 kickoff — example-driven, dogfood-first

Quickstart + demo are written FIRST as failing executable acceptance tests — the API is pulled into existence by usage; docs are tests and cannot rot. Demo: `examples/journal/`, a local-first research journal; every SDA delta is a natural feature (unique slugs = secondary-key index + upsert-by-natural-key; frozen `Event` log; tag queries; `get_many` backlinks; `Lazy[T]` attachments; `snapshot()` report on a worker thread).

## Definition of first runnable v0.1

- `uv sync && uv run pytest` fully green — incl. the **executable README**: every fenced block runs under sybil. Quickstart (<=25 lines, CI-enforced): `@entity`, `datacrystal.open(path)`, mutate, `commit()`, restart-restore, one bitmap query.
- `uv run python examples/journal/journal.py` (<=150 lines, CI-enforced) succeeds **twice in a row**: run 2 finds run 1's data and exercises all SDA features above, incl. mutation of a frozen `Event` raising and a snapshot read from a non-owner thread.
- `uv run pytest tests/torture` green: kill -9 / torn-write fuzzing never yields a store that fails to open or shows a half-committed graph; a second `open()` on the same directory raises the lease-lock error.
- `uv run pytest tests/contract` green: watermark contract v1 conformance (ordering, idempotency, replay, versioning); golden fixtures byte-identical (versioned header with reserved fields).
- Off-owner access raises `WrongThreadError`; `store.submit(fn)` returning a live entity raises `EntityEscapeError`.
- `uv run pytest benchmarks` stays within committed baselines (hook cost, commit latency).
- Wheel runtime deps exactly `{msgspec, pyroaring}` (fitness test); `pip install datacrystal` resolves on PyPI (placeholder ok).

## Milestones

**M0 — repo bootstrap (1.5 days).** Goal: names secured, harness up. In: reserve PyPI **`datacrystal` + `data-crystal`** (stub sdists, `uv publish`); src layout; dev-deps; CI (ruff, pyright strict, pytest, benchmark smoke); **fitness-function skeleton** (line budgets, runtime-dep allowlist, import-time <50 ms). Out: engine code. Exit: CI green on a PR; `pip install datacrystal` + `import datacrystal` work.

**M1 — executable spec (3 days).** Goal: quickstart + demo as *failing* acceptance tests; public API as typed stubs raising `NotImplementedError`. In: README blocks; `examples/journal/`; sybil wiring; full export list (`entity`, `open`, `Lazy`, `Index`, `Unique`, `Transient`, errors, `Store` methods); API-freeze review. Out: passing behavior. Exit: `pytest tests/acceptance` **collects and fails (never errors)**; pyright strict green; xfail list = backlog.

**M2 — object engine, in memory (8 days).** Goal: quickstart works to `commit()` without disk. In: `@entity` (slots + `weakref_slot`; `frozen=True` — tracking never arms); WeakValueDictionary OID registry + partitioned 64-bit ID space; tri-state Ghost/Clean/Dirty one-shot `__setattr__` (class-swap **spike with kill criterion**; fallback = permanently armed hook); `Lazy[T]`; msgspec codec + OID swizzling; owner binding, error taxonomy, `store.submit`. Out: disk, queries, snapshots. Exit: unit + property + confinement suites green; first README block passes.

**M3 — durability on SQLite (7 days).** Goal: restart-restore, crash-safe. In: 3-method storage protocol; SQLite-as-blob-store; versioned record header with reserved sealed-flag/footer-offset/tid-watermark fields; **three-phase commit** (P1 on-owner capture, P2 off-loop I/O on bytes, P3 on-owner flip + re-arm + watermark bump); buffer-until-commit storer; lease lock file; crash-torture harness. Out: FTS, disk GC, migrations. Exit: demo run-twice, torture, and lock tests green; golden fixtures pinned.

**M4 — queries, contract, snapshots = v0.1.0 (9 days).** Goal: demo fully green; watermark pipeline is a public versioned contract. In: pyroaring bitmap indexes + Condition AST; **unique secondary-key index**; **batch hydration**; commit-delta/watermark spec + conformance suite; `store.snapshot()` frozen-DTO + frozen-bitmap views, any-thread reads. Out: `datacrystal[fts]` (late v0.x — first *external* contract consumer), Arrow mirrors, vector, full `aopen()` polish (v0.2). Exit: **all** acceptance tests (incl. snapshot-from-thread) + contract suite green; baselines committed; `git tag v0.1.0`.

Total ~29 dev-days.

## First two weeks

1. **Day 1:** Reserve PyPI `datacrystal` + `data-crystal` (stub sdists, `uv publish`); commit `.gitignore`/`.python-version`; dev-dep group; `src/datacrystal/__init__.py`; delete `main.py`.
2. **Day 2:** `.github/workflows/ci.yml`; `tests/conftest.py` (sybil README collection); `tests/fitness/test_budgets.py`.
3. **Days 3–4:** `README.md` quickstart (4-line hello-world + one query); `examples/journal/journal.py` + its `README.md`; `tests/acceptance/test_readme.py` and `test_journal.py`.
4. **Day 5:** `_errors.py`; typed stubs in `_entity.py`, `_store.py`, `_lazy.py`, `_query/conditions.py`; pyright strict green; API-freeze review; tag `api-freeze-v0.1`.
5. **Days 6–7:** `_registry.py` + `tests/unit/test_registry.py` + `tests/property/test_oid_space.py`; real `@entity` incl. `frozen=True` + `tests/unit/test_entity.py`.
6. **Days 8–9:** class-swap dirty-tracking **spike** in `_dirty.py` + `tests/unit/test_dirty.py` (frozen/slots/inheritance edges); go/no-go vs kill criterion; `_codec.py` + `tests/property/test_codec_roundtrip.py`.
7. **Day 10:** `_store.py` owner binding + guards + `submit()`; `tests/unit/test_confinement.py`; first README block passes.

## Repo layout

```
pyproject.toml  README.md  # README is executable (sybil)
docs/design/               # ratified docs (existing)
examples/journal/journal.py, README.md  # the demo = the docs
src/datacrystal/
  __init__.py    # entire public surface
  _errors.py     # WrongThreadError, EntityEscapeError, ...
  _entity.py     # @entity, frozen=True, Annotated markers
  _registry.py   # WeakValueDictionary registry, ID space
  _dirty.py      # Ghost/Clean/Dirty one-shot hook
  _lazy.py       # Lazy[T], LazyReferenceManager (owner task)
  _codec.py      # msgspec records, format header, swizzle
  _store.py      # Store, open(), confinement, submit, get_many
  _commit.py     # three-phase commit, buffered storer
  _delta.py      # commit-delta/watermark PUBLIC contract v1
  _snapshot.py   # frozen-DTO/-bitmap watermark views
  _storage/{protocol,sqlite,lock}.py  # 3-method protocol, blob store, lease lock
  _query/{conditions,bitmap,unique}.py
tests/{acceptance,unit,property,torture,contract,fitness}/
benchmarks/      # pytest-benchmark baselines
```

## Test strategy

- **Executable docs:** sybil runs every fenced block in `README.md` and example READMEs; `journal.py` runs twice as a subprocess test. Any API change is a docs diff first.
- **Unit:** pytest + pytest-xdist, one test module per source module; confinement tests use real threads and a second process.
- **Property-based:** hypothesis — codec round-trip over generated entity graphs (cycles, Lazy edges); OID-space invariants; dirty tracking as `RuleBasedStateMachine` (invariant: persisted bytes == live graph); Condition AST vs brute-force scan oracle.
- **Crash-torture:** subprocess workloads SIGKILLed at random commit phases; `FaultyStorage` wrapper over the 3-method protocol injects torn writes/short reads/IO errors; every survivor opens cleanly at the last watermark. Reduced set on PRs, full nightly.
- **Contract-conformance:** `tests/contract/` is a reusable suite for the watermark pipeline (ordering, idempotent re-apply, replay-from-watermark, versioning) — `snapshot()` is consumer #1, FTS runs the same suite later; golden fixtures pinned byte-for-byte.
- **Static + fitness:** ruff, pyright strict; fitness tests enforce line budgets, dep allowlist, import time; pytest-benchmark fails CI on >20% regression.

## Top 5 risks

1. **Class-swap dirty tracking breaks on edge cases** (frozen/slots layouts, inheritance). Mitigation: never load-bearing — days 8–9 spike with kill criterion; fallback = permanently-armed hook + fingerprint diff; explicit `Lazy[T]` stays the documented API.
2. **API churn after freeze.** Mitigation: the README *is* the test, so changes are reviewed docs diffs first; small surface (~15 names); stubs-first catches breakage at pyright/collect time.
3. **One corruption report kills adoption.** Mitigation: no homegrown durability in v0.x — SQLite transactions carry it; torture suite from M3 day 1; indexes are rebuildable derived data.
4. **Watermark contract goes public with wrong semantics.** Mitigation: idempotency + ordering locked in spec before M4 code; `snapshot()` as in-tree consumer #1; conformance suite published; `datacrystal[fts]` validates it in late v0.x.
5. **Owner-confinement DX friction** (`WrongThreadError` in notebooks/async). Mitigation: errors embed the recipe (`submit`/`snapshot`/transaction scope); demo shows the blessed pattern; `debug=True` mode; asyncio doctrine in the README from day one.
