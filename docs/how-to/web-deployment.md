# How-to: serve a store over FastAPI and GraphQL

After this page you can expose a store through REST and GraphQL routes without learning the threading rules.

```bash
pip install 'datacrystal[web]' uvicorn
```

Save it as `app.py`.

```python
from typing import Annotated, Any

import strawberry
from fastapi import Depends, Response
from fastapi.testclient import TestClient
from strawberry.fastapi import GraphQLRouter
from strawberry.tools import create_type
from strawberry.types import Info

import datacrystal as dc
from datacrystal.web import (
    StrawberryReflector, create_app, entity_model, from_pydantic, graphql_context_getter,
    Writer, read_snapshot, submit_write, to_pydantic,
)

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    mohs: float | None = None

MineralCreate = entity_model(Mineral, face="create")
MineralPublic = entity_model(Mineral, face="public")

mineral_gql = StrawberryReflector().reflect(Mineral)

def mineral_resolver(qid: str, info: Info[Any, Any]) -> Any:
    hits = info.context["dc_snapshot"].query(Mineral.qid == qid)
    return hits[0] if hits else None

query_type = create_type("Query", [strawberry.field(
    resolver=mineral_resolver, graphql_type=mineral_gql | None, name="mineral")])

app = create_app("cabinet.store")
app.include_router(
    GraphQLRouter(strawberry.Schema(query=query_type), context_getter=graphql_context_getter),
    prefix="/graphql",
)

@app.get("/minerals/{qid}", response_model=MineralPublic)
def read_one(qid: str, snap: Annotated[dc.Snapshot, Depends(read_snapshot)]):
    hits = snap.query(Mineral.qid == qid)
    return to_pydantic(hits[0], face="public") if hits else Response(status_code=404)

@app.post("/minerals")
async def create(body: MineralCreate, write: Annotated[Writer, Depends(submit_write)]):
    def on_owner(store: dc.Store) -> int:
        store.upsert(from_pydantic(body, Mineral))
        return store.commit()
    return {"committed_tid": await write(on_owner)}

if __name__ == "__main__":
    with TestClient(app) as client:
        print(client.post("/minerals", json={"qid": "Q43010", "name": "quartz", "mohs": 7.0}).json())
        print(client.get("/minerals/Q43010").json())
        print(client.get("/minerals/missing").status_code)
        query = '{ mineral(qid: "Q43010") { name mohs } }'
        print(client.post("/graphql", json={"query": query}).json())
```

Run `python app.py` to see the four calls answered, or serve it with `uvicorn app:app --workers 1`; the `__main__` guard keeps uvicorn's import from running the calls.

- The extra is glue over `store.snapshot()` for reads, `store.submit()` and `aopen()` for writes and a per-request DataLoader; it adds no engine.
- Run `workers=1`.
  A store is single-writer, and `create_app` opens exactly one store per process on the startup thread, which becomes its owner thread ([ADR-001](../design/ADR-001-concurrency-contract.md)).
  `uvicorn --workers 4` is four processes, and the second to open the directory fails with `StoreLockedError`; an app scales within one process ([SCALING.md](../design/SCALING.md)).
- Reads go through `read_snapshot`, never the live graph.
  A sync route runs on a threadpool worker and is still correct, because a snapshot is read-only committed state ([ADR-002](../design/ADR-002-storage-read-views.md)); return `EntityView`s or DTOs, never live entities.
  The snapshot is pooled per commit watermark, so its index is built once and rebuilt only when a commit advances it; building one per request would cost O(store size) each time.
- Writes go through `submit_write`.
  A direct live write from a foreign thread still raises `WrongThreadError`; `submit_write` ships a closure to the owner, which mutates and commits there, and `await write(fn)` resolves once the commit is durable.
  Write routes are `async def` and the closure returns plain data (an OID or a TID), because a live entity in the result raises `EntityEscapeError`.
- `GraphQLRouter(schema, context_getter=graphql_context_getter)` gives each request one pinned snapshot (under `"dc_snapshot"`, the constant `SNAPSHOT_CONTEXT_KEY`) and a fresh `SnapshotLoader` (under `LOADER_CONTEXT_KEY`).
  This is mandatory: a process-lifetime loader would cache and leak resolved entities across requests and snapshot watermarks.
  Sibling reference edges batch into one `Snapshot.get_many`, so there is no N+1.
  For a request you drive by hand, `snapshot_context(snap)` builds the same context.
- `get_store` exposes the process store to a route that needs it, and raises if the app has no lifespan.
- The frameworks load only inside `datacrystal.web`; a bare `import datacrystal` never imports them.

## Reflection

- `reflect(cls)` returns the entity's `TypeInfo` and a tuple of `FieldDescriptor`s in persisted-schema order: `name`, marker-stripped `core_type`, `has_default` and the engine's `FieldSpec`.
  A non-entity raises `NotAnEntityError`.
- `entity_model(cls, face=...)` builds a Pydantic model: `"plain"` (declared fields), `"create"` (input, no `oid`) or `"public"` (output with a required `oid: int`).
  Markers ride along as OpenAPI `json_schema_extra` (`unique` as `candidate_key`, `indexed` as `queryable`, `fulltext` as `searchable`).
  A reference field crosses as its OID, a defaulted field becomes optional, a frozen entity becomes a frozen DTO, and the result is cached per class and face.
- `to_pydantic(source, face=...)` projects a live entity or an `EntityView` into a detached DTO, and `from_pydantic(dto, cls, store=...)` rebuilds a live entity through the public constructor.
- `reflect_strawberry_type(cls)` reflects one root; share a `StrawberryReflector` when reflecting several entities into one schema, so each entity maps to one GraphQL type and cycles terminate.

## Identity for web writes

Every write through `submit_write` runs under the request principal, anonymous by default and never the identity the store was opened with.
Add this to `app.py` before the `__main__` block to wire your auth with a dependency override:

```python
from fastapi import Request
from datacrystal.web import get_principal

def resolve(request: Request) -> dc.Principal | None:
    uid = request.headers.get("x-verified-uid")
    return dc.Principal(uid=int(uid), memberships={5: dc.CURATOR}) if uid else None

app.dependency_overrides[get_principal] = resolve
```

- The `acting_as` wrap sits inside each shipped closure, so concurrent requests never smear identities, and a raw `store.submit()` from your own background code still runs as the ambient principal.
- A write the commit gate denies returns 403 `{"error": "write-denied"}`, commits nothing and burns no TID.
- Build `Principal`s from verified claims, not bare uids; `acting_as(uid)` resolves through the Actor registry, which is for in-store identities.

Reference: [the web reflection API](../reference.md#datacrystalweb-reflection-api), [Concurrency primitives](../reference.md#concurrency-primitives).
