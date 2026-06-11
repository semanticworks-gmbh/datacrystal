# datacrystal

[![ci](https://github.com/themerius/datacrystal/actions/workflows/ci.yml/badge.svg)](https://github.com/themerius/datacrystal/actions/workflows/ci.yml)

**Your live objects, crystallized.** An embedded object-graph database for Python:
your typed objects **are** the database. Mutate them, call `commit()` — datacrystal keeps the
graph durable, queryable and identical across restarts. No ORM, no SQL, no schema files,
no pickle.

```python
from typing import Annotated
import datacrystal as dc

@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]                # unique key  → store.get()
    name: str

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None   # bitmap index → store.query()
    type_locality: dc.Lazy[Locality] | None = None            # loads on first .get()

store = dc.Store.open("cabinet.store")
if store.root is None:                            # first run only — reruns find the data
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.root = {"runs": 0, "minerals": [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"),
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic",
                type_locality=dc.Lazy.of(tsumeb)),
    ]}
store.root["runs"] += 1                           # in-place mutation is tracked — no ORM
store.commit()                                    # session, no dirty flags, no save() calls

hits = store.query(Mineral.crystal_system == "monoclinic")
print(sorted(m.name for m in hits))               # ['azurite']
print(store.root["runs"])                         # increments every run — it's a database
store.close()
```

Why this exists: the workhorse trio — SQLite + ORM, JSON files, pickle — makes you either
flatten a graph into tables, lose your types, or trust arbitrary code execution on load.
datacrystal (inspired by [EclipseStore](https://eclipsestore.io)) takes the fourth path:
slots-dataclasses as the canonical form, [msgspec](https://jcristharif.com/msgspec/) msgpack
records (decoding is structurally incapable of executing code),
[pyroaring](https://github.com/Ezibenroc/PyRoaringBitMap) bitmap indexes for queries, SQLite's
journal for crash safety, and one live instance per object — `a.friend is b` survives a restart.

**Works today** (163 tests, Python 3.14): entities, commit/reopen with identity, transparent
dirty tracking incl. in-place list/dict mutation, lazy references, bitmap queries with a
condition AST, unique keys, frozen (append-only) entities, additive schema evolution
(add fields with defaults / remove fields — handled on load), single-writer lease lock,
SIGKILL crash safety.
**Not yet** (see the [roadmap](docs/design/ROADMAP.md)): async three-phase commit,
`store.snapshot()` / `store.submit()` for threads, the public commit-delta contract,
full-text & vector search, Arrow/pandas/DuckDB mirrors, GraphQL.

## Try it

```
uv sync
uv run python examples/minerals/demo.py   # run it twice — the data is still there
uv run pytest
```

## Learn it

- **[docs/GUIDE.md](docs/GUIDE.md) — the user guide**: every feature that exists, every
  planned feature clearly marked as planned.
- [docs/design/](docs/design/) — design documents: [DESIGN.md](docs/design/DESIGN.md)
  (architecture), [ROADMAP.md](docs/design/ROADMAP.md) (ratified plan),
  [KICKOFF.md](docs/design/KICKOFF.md) (active execution plan, milestones),
  [ADR-001](docs/design/ADR-001-concurrency-contract.md) (owner-thread concurrency contract),
  [SCALING.md](docs/design/SCALING.md), [NAME.md](docs/design/NAME.md) (the metaphor),
  and the adversarial reviews.
- [docs/research/](docs/research/) — per-topic evidence (EclipseStore internals, ZODB prior
  art, CPython mechanics with benchmarks, engine surveys). Snapshots predating the 2026-06-10
  rename still say `pyrsistance`.

Status: **pre-release** (`0.1.0.dev0`, M1 walking skeleton complete + M2 in progress,
started 2026-06-11). The API freezes at the v0.1.0 tag; PyPI publication follows it.
License: [MIT](LICENSE).
