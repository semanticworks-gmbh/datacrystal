# datacrystal

An embedded object-graph database for Python, inspired by [EclipseStore](https://eclipsestore.io):
your typed Python objects **are** the database — pickle-free, crash-safe, with built-in
bitmap-indexed queries, zero-copy Arrow/DuckDB analytics, full-text and vector search.

Your live objects, crystallized: captured in a transparent, ordered lattice that grows
append-only and preserves their structure perfectly. The name and its metaphor are documented
in [docs/design/NAME.md](docs/design/NAME.md). (Earlier working title: `pyrsistance` — renamed
2026-06-10; the research snapshots under `docs/research/` still use the old name.)

**Status: walking skeleton (M1 of [KICKOFF.md](docs/design/KICKOFF.md), started 2026-06-11).**
The tracer-bullet loop works end-to-end: entities → commit → kill the process → reopen → graph
restored with identity, plus dirty tracking, lazy refs, bitmap queries, the unique key index,
frozen entities, the single-writer lease lock, and a SIGKILL crash test — 95 tests green on
Python 3.14. Not yet: three-phase async commit, snapshots, the public commit-delta contract,
PersistentList/Dict (see the roadmap).

## Quickstart

```python
from typing import Annotated
import datacrystal as dc

@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None
    type_locality: dc.Lazy[Locality] | None = None

store = dc.Store.open("cabinet.store")
if store.root is None:                      # first run only — reruns find the data
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.root = [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"),
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic",
                type_locality=dc.Lazy.of(tsumeb)),
    ]
store.commit()
hits = store.query(Mineral.crystal_system == "monoclinic")
print(sorted(m.name for m in hits))         # ['azurite']
store.close()
```

No ORM, no schema files, no SQL: mutate your objects, `commit()`. Queries run on
[pyroaring](https://github.com/Ezibenroc/PyRoaringBitMap) bitmap indexes declared with
`Annotated[..., dc.Index]`; records are [msgspec](https://jcristharif.com/msgspec/) msgpack
(never pickle); durability rides SQLite's journal. One writer per store, enforced by a lease
lock file (ADR-001's owner-thread contract is enforced at every boundary).

Try the demo (run it twice — the second run finds the first run's data):

```
uv sync
uv run python examples/minerals/demo.py
uv run pytest
```

## Design documents

- [docs/design/KICKOFF.md](docs/design/KICKOFF.md) — **active execution plan**: milestones,
  canonical example, perf suite, architectural fitness functions
- [docs/design/DESIGN.md](docs/design/DESIGN.md) — architecture proposal + accepted amendments
- [docs/design/ROADMAP.md](docs/design/ROADMAP.md) — ratified roadmap incl. the accepted SDA deltas
- [docs/design/ADR-001-concurrency-contract.md](docs/design/ADR-001-concurrency-contract.md) — **accepted**: owner-thread/loop confinement (Option D + riders)
- [docs/design/CONCURRENCY-OPTIONS.md](docs/design/CONCURRENCY-OPTIONS.md) — the four contract options as developed and judged
- [docs/design/SCALING.md](docs/design/SCALING.md) — how single-writer confinement scales: cores, processes, nodes, external indexers
- [docs/design/NAME.md](docs/design/NAME.md) — the name: origin, metaphor → feature mapping, availability audit
- [docs/design/SDA-LAYERING.md](docs/design/SDA-LAYERING.md) — **accepted**: SDA (semiotic data model) is the first-customer app layer, not core
- [docs/design/STRESS-TEST.md](docs/design/STRESS-TEST.md) — adversarial review of the proposal
- [docs/research/](docs/research/) — per-topic evidence (EclipseStore internals, ZODB prior art,
  CPython mechanics with benchmarks, engine surveys, kickoff plan panel + critiques)
- `resources/store` — cloned EclipseStore Java monorepo (reference)
- `resources/strawberry` — cloned strawberry-graphql (reference)
