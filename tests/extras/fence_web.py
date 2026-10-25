"""The capstone read-fence web tier: a REST and a GraphQL app over one protected record.

Imports the ``datacrystal[web]`` extra at module level, so a test module imports it only after
``pytest.importorskip`` of that extra.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import strawberry
from fastapi import Depends, FastAPI
from strawberry.fastapi import GraphQLRouter
from strawberry.tools import create_type
from strawberry.types import Info

import datacrystal as dc
from datacrystal import Snapshot
from datacrystal.web import SNAPSHOT_CONTEXT_KEY as SNAP_KEY
from datacrystal.web import (
    StrawberryReflector,
    get_principal,
    graphql_context_getter,
    read_snapshot,
    to_pydantic,
)
from tests.extras.fence_world import LABEL, A, Cabinet, Specimen

OPERATOR = dc.Principal(uid=1, memberships={dc.WORLD: dc.ADMIN})


def seed_web_store(directory: Path) -> dc.Store:
    """Open a store in ``directory`` holding the hidden specimen and a cabinet that refers to it.

    No blob value here: the web tier exercises the discovery and deref fence (the blob tier owns
    blobs). A populated dc.Blob field projects to its OID, an orthogonal to_pydantic/Blob
    modeling gap.
    """
    store = dc.Store.open(directory, durability="never", principal=OPERATOR)
    with store.acting_as(A):
        secret = Specimen(label=LABEL, note="the crystal lattice diagram")
        store.store(secret)  # owner-only — B never reads it
        store.store(Cabinet(tag="d1", holds=dc.Lazy.of(secret)))
        store.commit()
    return store


def rest_app(store: dc.Store) -> FastAPI:
    """Build the REST app: a discovery route and a cabinet route that derefs its specimen."""
    app = FastAPI()
    app.state.dc_store = store

    @app.get("/specimens")
    async def specimens(snap: Annotated[Snapshot, Depends(read_snapshot)]) -> dict[str, list[str]]:
        return {"labels": [v.label for v in snap.query(Specimen)]}

    @app.get("/cabinet/{tag}")
    async def cabinet(tag: str, snap: Annotated[Snapshot, Depends(read_snapshot)]) -> dict[str, Any]:
        matches = snap.query(Cabinet.tag == tag)  # pyrefly: ignore[bad-argument-type]
        if not matches:
            return {"found": False}
        holds: Any = None
        if matches[0].holds is not None:
            (secret,) = snap.get_many([matches[0].holds])
            dto = to_pydantic(secret, face="public") if secret is not None else None
            holds = dto.model_dump() if dto is not None else None
        return {"found": True, "holds": holds}

    return app


def graphql_app(store: dc.Store) -> FastAPI:
    """Build the GraphQL app: a ``cabinet(tag:)`` query whose ``holds`` derefs its specimen."""
    reflector = StrawberryReflector()
    cabinet_type = reflector.reflect(Cabinet)

    def cabinet_resolver(tag: str, info: Info[dict[str, Snapshot], None]) -> object:
        snap: Snapshot = info.context[SNAP_KEY]
        matches = snap.query(Cabinet.tag == tag)  # pyrefly: ignore[bad-argument-type]
        return matches[0] if matches else None

    cabinet_field: Any = strawberry.field(
        resolver=cabinet_resolver, graphql_type=cabinet_type | None, name="cabinet"
    )
    schema = strawberry.Schema(query=create_type("Query", [cabinet_field]))
    app = FastAPI()
    app.state.dc_store = store
    app.include_router(
        GraphQLRouter(schema, context_getter=graphql_context_getter), prefix="/graphql"
    )
    return app


def read_as(app: FastAPI, principal: dc.Principal) -> FastAPI:
    """Make every request to the app read as the principal, and return the app."""

    def current_principal() -> dc.Principal:
        return principal

    app.dependency_overrides[get_principal] = current_principal
    return app


def dc_keys_on_wire(payload: object) -> list[str]:
    """Return every lib-managed ``_dc_*`` key anywhere in a decoded JSON payload."""
    if isinstance(payload, dict):
        own = [str(key) for key in payload if str(key).startswith("_dc")]
        return own + [key for value in payload.values() for key in dc_keys_on_wire(value)]
    if isinstance(payload, list):
        return [key for value in payload for key in dc_keys_on_wire(value)]
    return []
