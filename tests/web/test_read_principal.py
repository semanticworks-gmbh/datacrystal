"""W4-4: the per-request principal fences the ``datacrystal[web]`` READ tier.

The read-side twin of :mod:`tests.web.test_request_principal` (which covers the
write side). A request's principal — resolved by ``get_principal`` and threaded
through ``read_snapshot`` onto a ``Snapshot.for_principal`` handle — sees only
its readable rows through REST *and* GraphQL, a denied reference renders as
wire-null (no error, no existence leak), the anonymous pool base reads no
protected row, and two principals at one watermark SHARE the one pooled core
(the index is built once per commit, never per principal or per request —
ADR-008 R15).

Transport: ``asyncio.run`` + ``httpx.AsyncClient(ASGITransport)`` (the async
read routes run on the loop; a snapshot read is any-thread anyway, ADR-002).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")
pytest.importorskip("fastapi", reason="datacrystal[web] extra not installed")
pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")
pytest.importorskip("httpx", reason="pip install httpx (ASGITransport)")

import httpx
import strawberry
from fastapi import Depends, FastAPI
from strawberry.fastapi import GraphQLRouter
from strawberry.tools import create_type
from strawberry.types import Info

import datacrystal as dc
from datacrystal import EntityView, Snapshot
from datacrystal.web import (
    SNAPSHOT_CONTEXT_KEY as SNAP_KEY,
)
from datacrystal.web import (
    StrawberryReflector,
    entity_model,
    from_pydantic,
    get_principal,
    graphql_context_getter,
    read_snapshot,
    to_pydantic,
)

TEAM = 7
OPERATOR = dc.Principal(uid=1, memberships={dc.WORLD: dc.ADMIN})
BEA = dc.Principal(uid=42, memberships={TEAM: dc.CURATOR})  # owner/seeder
ANNA = dc.Principal(uid=41, memberships={TEAM: dc.VIEWER})  # can read the team row
DENY = dc.Principal(uid=98, memberships={99: dc.CURATOR})  # a foreign team, denied


@dc.entity(protected=True)
class Secret:
    """A protected row fenced to a team."""

    label: Annotated[str, dc.Unique]
    body: str = ""


@dc.entity
class Doc:
    """An unprotected document referencing a protected Secret."""

    slug: Annotated[str, dc.Unique]
    title: str
    about: dc.Lazy[Secret] | None = None


@pytest.fixture
def seeded_store(tmp_path: Path) -> Iterator[dc.Store]:
    """Provide a store opened by the OPERATOR with one TEAM-fenced Secret and a Doc on it.

    BEA (CURATOR in TEAM) owns and fences the Secret to ``read=VIEWER``: any TEAM
    member may read it, a non-member cannot.
    """
    store = dc.Store.open(tmp_path / "cabinet", durability="never", principal=OPERATOR)
    with store.acting_as(BEA):
        secret = Secret(label="s1", body="classified")
        store.store(secret)
        dc.share(secret, TEAM, read=dc.VIEWER, write=dc.CURATOR)
        doc = Doc(slug="d1", title="cover", about=dc.Lazy.of(secret))
        store.store(doc)
        store.commit()
    yield store
    store.close()


def _requests_as(principal: dc.Principal) -> Callable[[], dc.Principal]:
    """Build a get_principal override that always resolves ``principal``."""

    def resolve() -> dc.Principal:
        return principal

    return resolve


def _requests_as_in_turn(*principals: dc.Principal) -> Callable[[], dc.Principal]:
    """Build a get_principal override that resolves ``principals`` one request at a time."""
    remaining = iter(principals)

    def resolve() -> dc.Principal:
        return next(remaining)

    return resolve


def _send(
    app: FastAPI, method: str, path: str, json: dict[str, str] | None = None
) -> httpx.Response:
    """Send one request to ``app`` over an ASGI transport on the calling thread."""

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://web") as client:
            return await client.request(method, path, json=json)

    return asyncio.run(send())


# --- REST read app ------------------------------------------------------------


def _rest_app(store: dc.Store, captured: list[Snapshot] | None = None) -> FastAPI:
    app = FastAPI()
    app.state.dc_store = store  # the get_store state key; _get_pool builds lazily

    @app.get("/secrets")
    async def secrets(snap: Annotated[Snapshot, Depends(read_snapshot)]) -> dict[str, list[str]]:
        # A DISCOVERY surface: query() intersects the handle principal's readable
        # set, so a denied row never appears in the list.
        return {"labels": [v.label for v in snap.query(Secret)]}

    @app.get("/doc/{slug}")
    async def doc(
        slug: str, snap: Annotated[Snapshot, Depends(read_snapshot)]
    ) -> dict[str, object]:
        matches = snap.query(Doc.slug == slug)  # pyrefly: ignore[bad-argument-type]
        if not matches:
            return {"found": False}
        d = matches[0]
        about: dict[str, object] | None = None
        if d.about is not None:
            # Deref the (possibly protected) referent: a denied one comes back a
            # redacted twin, and to_pydantic projects it to wire-null (W4-4).
            (sec,) = snap.get_many([d.about])
            dto = to_pydantic(sec, face="public") if sec is not None else None
            about = dto.model_dump() if dto is not None else None
        return {"found": True, "title": d.title, "about": about}

    @app.get("/whoami")
    async def whoami(snap: Annotated[Snapshot, Depends(read_snapshot)]) -> dict[str, int]:
        if captured is not None:
            captured.append(snap)
        return {"uid": snap.principal.uid}

    return app


# --- GraphQL read app ---------------------------------------------------------


def _graphql_app(store: dc.Store) -> FastAPI:
    doc_gql = StrawberryReflector().reflect(Doc)

    def doc_resolver(slug: str, info: Info[Any, Any]) -> EntityView | None:
        snap: Snapshot = info.context[SNAP_KEY]
        matches = snap.query(Doc.slug == slug)  # pyrefly: ignore[bad-argument-type]
        return matches[0] if matches else None

    doc_field: Any = strawberry.field(
        resolver=doc_resolver, graphql_type=doc_gql | None, name="doc"
    )
    schema = strawberry.Schema(query=create_type("Query", [doc_field]))

    app = FastAPI()
    app.state.dc_store = store
    app.include_router(
        GraphQLRouter(schema, context_getter=graphql_context_getter), prefix="/graphql"
    )
    return app


# --- tests --------------------------------------------------------------------

DOC_QUERY = '{ doc(slug: "d1") { title about { label } } }'


def test_rest_discovery_lists_the_readable_row_for_a_team_member(
    seeded_store: dc.Store,
) -> None:
    """Given a TEAM-fenced Secret and a request resolved as the TEAM viewer Anna
    When the client GETs /secrets
    Then the list holds the Secret's label
    """
    app = _rest_app(seeded_store)
    app.dependency_overrides[get_principal] = _requests_as(ANNA)

    resp = _send(app, "GET", "/secrets")

    assert resp.json() == {"labels": ["s1"]}


def test_rest_discovery_filters_the_row_out_for_a_non_member(seeded_store: dc.Store) -> None:
    """Given a TEAM-fenced Secret and a request resolved as a foreign-team principal
    When the client GETs /secrets
    Then the list is empty, filtered rather than errored
    """
    app = _rest_app(seeded_store)
    app.dependency_overrides[get_principal] = _requests_as(DENY)

    resp = _send(app, "GET", "/secrets")

    assert resp.json() == {"labels": []}


def test_rest_member_reads_the_referent_through_the_edge(seeded_store: dc.Store) -> None:
    """Given a Doc referencing a TEAM-fenced Secret and a request resolved as Anna
    When the client GETs /doc/d1
    Then the Doc is found
    And its about edge carries the Secret's label
    """
    app = _rest_app(seeded_store)
    app.dependency_overrides[get_principal] = _requests_as(ANNA)

    body = _send(app, "GET", "/doc/d1").json()

    assert body["found"] is True
    assert body["about"]["label"] == "s1"


def test_rest_denied_reference_projects_to_null_no_error_no_leak(
    seeded_store: dc.Store,
) -> None:
    """Given a Doc referencing a TEAM-fenced Secret and a request resolved as a non-member
    When the client GETs /doc/d1
    Then the response is 200
    And the Doc resolves with its about edge null
    """
    app = _rest_app(seeded_store)
    app.dependency_overrides[get_principal] = _requests_as(DENY)

    resp = _send(app, "GET", "/doc/d1")

    assert resp.status_code == 200
    assert resp.json() == {"found": True, "title": "cover", "about": None}


def test_anonymous_pool_base_reads_no_protected_row(seeded_store: dc.Store) -> None:
    """Given a TEAM-fenced Secret and a request with no resolved identity
    When the client GETs /secrets
    Then the list is empty
    """
    app = _rest_app(seeded_store)

    resp = _send(app, "GET", "/secrets")

    assert resp.json() == {"labels": []}


def test_anonymous_pool_base_is_pinned_to_uid_zero(seeded_store: dc.Store) -> None:
    """Given a request with no resolved identity
    When the client GETs /whoami
    Then the snapshot's principal has uid 0
    """
    app = _rest_app(seeded_store)

    resp = _send(app, "GET", "/whoami")

    assert resp.json() == {"uid": 0}


def test_graphql_member_sees_the_referent(seeded_store: dc.Store) -> None:
    """Given a Doc referencing a TEAM-fenced Secret and a request resolved as Anna
    When the client queries the Doc and its about edge
    Then the response has no errors
    And the data holds the title and the Secret's label
    """
    app = _graphql_app(seeded_store)
    app.dependency_overrides[get_principal] = _requests_as(ANNA)

    body = _send(app, "POST", "/graphql", json={"query": DOC_QUERY}).json()

    assert body.get("errors") is None
    assert body["data"] == {"doc": {"title": "cover", "about": {"label": "s1"}}}


def test_graphql_denied_reference_is_null(seeded_store: dc.Store) -> None:
    """Given a Doc referencing a TEAM-fenced Secret and a request resolved as a non-member
    When the client queries the Doc and its about edge
    Then the response is 200
    And it has no errors
    And the Doc resolves with a null about edge
    """
    app = _graphql_app(seeded_store)
    app.dependency_overrides[get_principal] = _requests_as(DENY)

    resp = _send(app, "POST", "/graphql", json={"query": DOC_QUERY})

    body = resp.json()
    assert resp.status_code == 200
    assert body.get("errors") is None
    assert body["data"] == {"doc": {"title": "cover", "about": None}}


def test_two_principals_at_one_watermark_get_their_own_handles(seeded_store: dc.Store) -> None:
    """Given requests resolved as Anna and then as a non-member at one watermark
    When the client GETs /whoami twice
    Then each response names its own principal's uid
    And the two requests got distinct snapshot handles
    And the handles are bound to distinct principals
    """
    captured: list[Snapshot] = []
    app = _rest_app(seeded_store, captured)
    app.dependency_overrides[get_principal] = _requests_as_in_turn(ANNA, DENY)

    first = _send(app, "GET", "/whoami")
    second = _send(app, "GET", "/whoami")

    assert first.json() == {"uid": ANNA.uid}
    assert second.json() == {"uid": DENY.uid}
    assert captured[0] is not captured[1]
    assert captured[0].principal.uid != captured[1].principal.uid


def test_two_principals_at_one_watermark_share_the_pooled_core(seeded_store: dc.Store) -> None:
    """Given requests resolved as Anna and then as a non-member at one watermark
    When the client GETs /whoami twice
    Then exactly two handles were served
    And closing one handle closes the pooled core the other shares
    """
    captured: list[Snapshot] = []
    app = _rest_app(seeded_store, captured)
    app.dependency_overrides[get_principal] = _requests_as_in_turn(ANNA, DENY)

    _send(app, "GET", "/whoami")
    _send(app, "GET", "/whoami")

    assert len(captured) == 2
    captured[0].close()
    with pytest.raises(dc.StoreClosedError):
        captured[1].count(Secret)


def test_create_face_has_no_lib_managed_floor_fields() -> None:
    """Given the create face of the protected Secret
    When its fields are listed
    Then it has no _dc_owner field
    And it has no _dc_read_floor field
    """
    create_model = entity_model(Secret, face="create")

    assert "_dc_owner" not in create_model.model_fields
    assert "_dc_read_floor" not in create_model.model_fields


def test_from_pydantic_cannot_spoof_lib_managed_dc_labels() -> None:
    """Given a create body smuggling _dc_owner 999 and an EXECUTIVE _dc_read_floor
    When from_pydantic rebuilds the Secret
    Then the owner is not the smuggled 999
    And the read floor is not the smuggled EXECUTIVE
    """
    create_model = entity_model(Secret, face="create")
    dto = create_model.model_validate(
        {"label": "spoof", "body": "x", "_dc_owner": 999, "_dc_read_floor": dc.EXECUTIVE}
    )

    entity = from_pydantic(dto, Secret)

    assert entity.dc_permissions.owner != 999  # pyrefly: ignore[missing-attribute]
    assert entity.dc_permissions.read_floor != dc.EXECUTIVE  # pyrefly: ignore[missing-attribute]
