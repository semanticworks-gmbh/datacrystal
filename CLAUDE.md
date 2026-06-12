# CLAUDE.md

datacrystal: an embedded object-graph database for Python (EclipseStore-inspired) — typed live
objects ARE the database; pickle-free msgpack records, roaring-bitmap queries, SQLite-blob
durability. Solo maintainer: Sven Hodapp. Pre-release (`0.1.0.dev0`); current milestone: M4
(M3 — watermark pipeline, snapshots, conformance kit, FTS5 spike — landed 2026-06-12).

## Commands

```
uv sync                              # env (Python 3.14 via .python-version)
uv run pytest -q                     # full suite incl. fitness gates + SIGKILL crash test
uv run ruff check .                  # lint (line length 100)
uvx pyright src tests examples       # 0 errors required (standard mode; strict at the v0.1.0 tag)
uv run python examples/minerals/demo.py   # run TWICE — second run must find the first run's data
```

If `uv run pytest` fails with "No module named datacrystal" after the repo moved/renamed:
stale venv shebangs — `rm -rf .venv && uv sync`.

## Where decisions live (read before proposing anything)

- `docs/design/ROADMAP.md` — **scope authority**, incl. the *Punted* and *Never* lists.
  Check both lists before suggesting features (no Rust core, no CRDT core, no multi-writer,
  no homegrown SPARQL/Cypher, …).
- `docs/design/KICKOFF.md` — execution plan: milestones M0–M4, the 18 fitness functions,
  perf-gate principles, the canonical mineral-cabinet example domain (one domain everywhere).
- `docs/design/ADR-001-concurrency-contract.md` — accepted owner-confinement contract.
- `docs/design/ADR-002-storage-read-views.md` — accepted `read_view()` protocol addition
  (snapshot isolation for `store.snapshot()`); storage-protocol growth always needs an ADR.
- `docs/design/ADR-003-delete-semantics.md` — accepted unchecked-delete contract
  (`store.delete()`, tombstone deltas, `CommitBatch.deletes`, `DanglingRefError`);
  checked delete waits for the v1 reverse-reference index.
- `docs/design/COMMIT-DELTA-v1.md` — the delta/watermark contract (DRAFT; locks at the tag).
  The applier + replay vectors are normative and byte-pinned; revisions need a draft-rev bump.
- `docs/GUIDE.md` — user-facing semantics. Documentation honesty rule: features that do not
  exist are marked `[planned — milestone]`, never described as if real.
- The API freezes at the v0.1.0 tag; PyPI publication follows it (names reserved earlier).

## Architecture map (`src/datacrystal/`)

| Module | Role |
|---|---|
| `_store.py` | facade: open/root/store/delete/commit/get/query/count/pluck/get_many/attach/detach/snapshot; P1 capture (+ prior reads + delta build when consumers watch) → P2 backend I/O → P3 flip + delta delivery; type lineage + hydration plans; decode-level reads (count/pluck) construct no entities; deletes are unchecked per ADR-003 (DanglingRefError on follow) |
| `_pipeline.py` | COMMIT-DELTA-v1 emission: `DeltaConsumer` protocol + `build_delta`; delivery in P3 post-durability; a raising consumer detaches loudly (never holds writes hostage) |
| `_snapshot.py` | `store.snapshot()` frozen `EntityView`/`Ref` reads at a commit watermark, callable from any thread (ADR-002 read views); `index_bitmaps()` slot reserved for M4 |
| `testing.py` | public conformance kit `check_delta_consumer` + `CountingConsumer` (incl. the snapshot-bootstrap recipe for mid-life attach) |
| `_entity.py` | `@entity` decorator → slots dataclass + engine slots; one-shot `__setattr__` dirty hook; `TypeInfo` (specs, defaults); metaclass turns class-attr access into query `FieldExpr`s |
| `_state.py` | leaf module: NEW/CLEAN/DIRTY constants + `touch()` (shared by hook and containers) |
| `_containers.py` | owner-bound `PersistentList`/`PersistentDict`: in-place mutation marks the owner dirty; assignment copies (by-value semantics) |
| `_conditions.py` | Condition AST (`Pred`/`And`/`Or`/`Not`), `FieldExpr`, `fields()` typed proxy |
| `_indexes.py` | rebuildable in-memory pyroaring bitmap indexes + unique maps; planner splits conditions into bitmap + Python residual |
| `_records.py` | msgspec msgpack codec; entity refs swizzled to OID extension values in an explicit pre-pass |
| `_registry.py` | WeakValueDictionary OID → live entity (identity contract) |
| `_lazy.py` | explicit `Lazy[T]` handles — the only deferred-loading mechanism in v0.x |
| `_ids.py` | partitioned 64-bit OID/CID/TID space; `FORMAT_VERSION` |
| `_storage/` | storage protocol (`boot/load_many/scan_type/apply/read_view` — growth needs an ADR, see ADR-002) + SQLite-blob backend + memory fake + lease lock |

## Load-bearing invariants (violating one = architectural regression, not a style issue)

1. **No pickle anywhere** — decode must stay structurally incapable of executing code.
2. Core deps exactly `{msgspec, pyroaring}`; `sqlite3` imported lazily at `Store.open`.
3. **Owner confinement (ADR-001)**: foreign threads raise `WrongThreadError` BEFORE any
   mutation lands. Every new write path must call the thread check pre-mutation.
4. Buffer-until-commit; `commit()` keeps the P1/P2/P3 three-phase shape even while synchronous
   (M2 moves P2 off-thread without changing the logic). Never a second commit path.
5. TIDs are sequence-derived, never wall-clock; a rejected commit leaves the TID sequence
   gapless (replay determinism is a public-contract property).
6. Identity: one live instance per OID. The root holder is **pinned** (strong ref) — root
   reachability = RAM; `Lazy[T]` is the explicit cut point. Non-root-reachable CLEAN entities
   must stay collectable (memory fitness gates assert this).
7. Every list/dict entering an entity field is wrapped as an owner-bound persistent container;
   wrapping copies. Frozen owners' containers raise on mutation.
8. Schema evolution is additive via **type lineage**: a changed field shape gets a new cid;
   records decode by NAME through their own persisted shape, missing fields fill from dataclass
   defaults, removed fields are ignored; no default → loud `SchemaMismatchError`. Old records
   are never rewritten in place.
9. Format honesty: opening a newer store raises `NewerStoreError`; on-disk migrations (like the
   types-table UNIQUE drop) must be idempotent.
10. One writer per store (lease lock); a lost lease refuses to write (`LeaseLostError`).
11. Indexes are rebuildable derived data — never persisted, never inside the commit txn.
12. Fitness/perf gates are same-run ratios, operation counts, or byte counts — never absolute
    wall-clock.

## Testing conventions

- Engine tests parametrize over both backends via the `store_factory` fixture (`tests/conftest.py`);
  memory and sqlite must behave identically.
- `tests/fitness/` are CI gates (pickle-free AST walk, dep budget, memory boundedness).
- The README quickstart must run verbatim, twice, from a clean directory.
- Schema-evolution tests fabricate classes dynamically with the same typename to simulate
  code changes between runs; their per-file pyright pragmas exist only for that.
- The test/demo domain is always the mineral cabinet — do not invent a second domain.

## Style & gotchas

- pyright standard mode must stay at 0 errors. The magic class-attribute query syntax
  (`Mineral.mohs >= 6.0`) is untypeable by design — use `dc.fields(Mineral)` in typed code,
  keep per-file pragmas in tests that deliberately exercise the magic path.
- Docstrings explain *why* and cite the design doc that ratified the behavior.
- Commit/PR style: small logical commits; CI (`.github/workflows/ci.yml`) runs on PRs and
  pushes to main.
- Working with Sven: when a genuine scope fork exists, ask 1–3 sharp questions first
  (he wants to be interviewed), then run autonomously. Prefer fixing a bug over documenting
  its workaround.
