# CLAUDE.md

datacrystal: an embedded object-graph database for Python (EclipseStore-inspired) — typed live
objects ARE the database; pickle-free msgpack records, roaring-bitmap queries, SQLite-blob
durability, and two released-shape extras: `datacrystal[fts]` (FTS5 + Snowball) and
`datacrystal[arrow]` (persistent parquet mirrors). Solo maintainer: Sven Hodapp. Version
`0.1.0` — **API frozen at the v0.1.0 tag (2026-06-13)**: extras landed pre-tag as contract
validators (2026-06-12), COMMIT-DELTA-v1 LOCKED, the pyright-strict pass DONE (library `src/`
strict-clean and CI-gated). PyPI publication is the deferred next step (names reserved).

## Commands

```
uv sync --all-extras                 # env (Python 3.14 via .python-version; extras for their tests)
uv run pytest -q                     # full suite incl. fitness gates + SIGKILL crash test
uv run ruff check .                  # lint (line length 100)
uvx pyright src tests examples benchmarks  # standard mode, 0 errors (tests keep the magic-query pragmas)
uvx pyright -p pyrightconfig.strict.json   # STRICT, library src/ only — 0 errors, CI-gated (the lib is strict-clean)
uv run python examples/minerals/demo.py   # run TWICE — second run must find the first run's data
uv run pytest benchmarks -q -s       # KICKOFF §6 PR perf gates (warn-stage; DC_BENCH_STRICT=1 hardens)
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
- `docs/design/COMMIT-DELTA-v1.md` — the delta/watermark contract (**LOCKED v1**, 2026-06-12).
  The applier + replay vectors are normative and byte-pinned; changes now mean a NEW contract
  version, never an edit.
- `docs/GUIDE.md` — user-facing semantics. Documentation honesty rule: features that do not
  exist are marked `[planned — milestone]`, never described as if real.
- The API freezes at the v0.1.0 tag; PyPI publication follows it (names reserved earlier).

## Architecture map (`src/datacrystal/`)

| Module | Role |
|---|---|
| `_store.py` | facade: open/root/store/delete/upsert/commit/get/query/explain/count/pluck/get_many/attach/detach/snapshot; query/count/pluck/explain all take class-or-Condition (symmetry, 2026-06-12); explain() reports the two-rule QueryPlan — NEVER grow an optimizer (DuckDB over the mirror owns that tier); P1 capture (+ prior reads + delta build when consumers watch) → P2 backend I/O → P3 flip + delta delivery; type lineage + hydration plans; decode-level reads (count/pluck) construct no entities; deletes are unchecked per ADR-003 (DanglingRefError on follow); upsert merges into the surviving instance, writing only changed fields |
| `_pipeline.py` | COMMIT-DELTA-v1 emission: `DeltaConsumer` protocol + `build_delta`; delivery in P3 post-durability; a raising consumer detaches loudly (never holds writes hostage) |
| `_snapshot.py` | `store.snapshot()` frozen `EntityView`/`Ref` reads at a commit watermark, callable from any thread (ADR-002 read views); bitmap `query()`/`count()` + `index_bitmaps()` over snapshot-local indexes rebuilt from the pinned view (never shared with the owner's) |
| `testing.py` | public conformance kit `check_delta_consumer` + `CountingConsumer` (incl. the snapshot-bootstrap recipe for mid-life attach) |
| `_entity.py` | `@entity` decorator → slots dataclass + engine slots; one-shot `__setattr__` dirty hook; `TypeInfo` (specs, defaults); metaclass turns class-attr access into query `FieldExpr`s |
| `_state.py` | leaf module: NEW/CLEAN/DIRTY constants + `touch()` (shared by hook and containers) |
| `_containers.py` | owner-bound `PersistentList`/`PersistentDict`: in-place mutation marks the owner dirty; assignment copies (by-value semantics) |
| `_conditions.py` | Condition AST (`Pred`/`And`/`Or`/`Not` incl. contains/startswith), `FieldExpr`, `fields()` typed proxy |
| `_indexes.py` | rebuildable in-memory pyroaring bitmap indexes + unique maps (deliberately NOT a delta consumer — spec §5 says unwatched stores pay nothing); planner splits conditions into bitmap + Python residual; contains/startswith iterate distinct index keys; `build_class_indexes` is shared with snapshots |
| `_records.py` | msgspec msgpack codec; entity refs swizzled to OID extension values in an explicit pre-pass |
| `_registry.py` | WeakValueDictionary OID → live entity (identity contract) |
| `_lazy.py` | explicit `Lazy[T]` handles — the only deferred-loading mechanism in v0.x |
| `_ids.py` | partitioned 64-bit OID/CID/TID space; `FORMAT_VERSION` |
| `_storage/` | storage protocol (`boot/load_many/scan_type/apply/read_view` — growth needs an ADR, see ADR-002) + SQLite-blob backend + memory fake + lease lock |
| `fts.py` | `datacrystal[fts]` extra (imports snowballstemmer — never from core): FTS5 sidecar consumer; fold/stem symmetry is BY CONSTRUCTION (same Python normalize-stem-fold on column content and query — never index raw text in a searchable column); stem-first-fold-after (Russian й/ё); raw text lives in UNINDEXED r_ columns for Python-side highlighting |
| `arrow.py` | `datacrystal[arrow]` extra (imports pyarrow — never from core): persistent parquet mirrors; LSM segments + atomic fsync-ordered manifest.json; total type-promotion lattice with msgpack-binary fallback (schema evolution can never wedge it); newest-wins fold per OID; compact() ⇒ plain-parquet datalake dir; one owner process per mirror dir |
| `deltalog.py` | retained delta log (ROADMAP item 23, first post-tag PR): CORE module — no extra, deps stay {msgspec, pyroaring}; a `DeltaConsumer` appending raw COMMIT-DELTA-v1 bytes (length-prefixed frames) to rolling segments behind an atomic fsync-ordered manifest (segment fsynced BEFORE manifest → watermark never lies); reopen truncates partial appends + sweeps orphan segments (exact gapless commit prefix); `replay()`/`replayed_state()` = time-travel-by-replay (faithful from watermark 0); `bootstrap()` mid-life attach records the change-feed from the join; engine still never retains (§5 unchanged); retention/pruning is the operator's policy |
| `benchmarks/` (repo root) | KICKOFF §6 PR perf gates: same-run ratios only, warn until hardened (`DC_BENCH_STRICT=1`); `_gen.py` is the canonical scaled mineral-cabinet generator (Zipf hubs, provenance cycles, frozen events) |

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
