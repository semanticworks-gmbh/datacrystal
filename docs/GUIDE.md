# datacrystal documentation

After this page you can run a first store and find the page that teaches the next thing you need.

```python
from typing import Annotated

import datacrystal as dc

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str, dc.Index]

with dc.Store.open("cabinet.store") as store:
    if store.root is None:
        store.root = {"seeded": True}
        store.store(Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic"))
        store.commit()
    print([m.name for m in store.query(Mineral.crystal_system == "monoclinic")])
```

Run it twice: the second run finds the data the first one committed.

The pages follow the [Diátaxis](https://diataxis.fr) split, and a feature that does not exist yet is marked `[planned — …]`.

## Tutorial

[Your first mineral cabinet](tutorial.md) defines a model, stores it, reopens it, queries it and follows a lazy reference.

## How-to guides

- [Query and page results](how-to/querying-and-paging.md): `limit`, `offset`, `order_by`, top-N and `incoming()`.
- [Ingest big data and keep memory bounded](how-to/ingest-and-memory.md): free-floating entities, `query_iter` and parallel parsing.
- [Evolve your schema](how-to/schema-evolution.md): `RenamedFrom`, `Glue`, `migrate` and `verify`.
- [Store binary blobs](how-to/blobs.md): `dc.Blob`, streamed reads and writes.
- [Restrict who can read a record](how-to/permissions.md): `protected=True`, `dc.share`, `dc.Redacted` and root.
- [Serve a store over FastAPI and GraphQL](how-to/web-deployment.md): `datacrystal[web]` routes, reflection and request identity.
- [Run a coordinator and edge followers](how-to/federation.md): `Store.follower`, `sync()` and `committing()`.
- [Search prose fields](how-to/search.md): `datacrystal[fts]` with stemming and BM25.
- [Search by vector and fuse rankings](how-to/vector-search.md): your embeddings, exact top-k and RRF.
- [Run analytics on Arrow mirrors](how-to/analytics.md): `datacrystal[arrow]` and DuckDB.
- [Read from any thread, follow the commit stream and keep an audit log](how-to/snapshots-and-delta-log.md): snapshots, consumers and `DeltaLog`.
- [See datacrystal's operational events in your log](how-to/logging.md): the `datacrystal` logger, its events and a structlog setup.

## Reference

[Reference](reference.md) lists every method, option, guarantee and error, and the type-checker quirks.

## Explanation

[Explanation](explanation.md) covers identity and memory, the query planner, unchecked deletes, permissions, followers and the design philosophy.

## Design documents

[docs/design/](design/) holds the decisions behind the pages above: [VISION.md](design/VISION.md), [DESIGN.md](design/DESIGN.md), [ROADMAP.md](design/ROADMAP.md) (scope authority), [KICKOFF.md](design/KICKOFF.md) (engineering standards), the [ADRs](design/), [SCALING.md](design/SCALING.md) and the [sidecar read-fence pattern](design/SIDECAR-READ-FENCE.md).
