"""datacrystal[web] — the FastAPI/Strawberry app wiring, end to end (#92).

Sprint 9 deploy/scale glue (#92 / #49 spike S, build plan #23): the acceptance
tests for :mod:`datacrystal.web._app` — the lifespan, the per-request read /
write / GraphQL-context dependencies, and the doctrine they encode (one store
per worker, reads via snapshots, writes serialized through the owner —
ADR-001). They drive a **real** FastAPI application (and a real Strawberry
``GraphQLRouter``), not the dependency functions in isolation:

* the **lifespan** opens one store on startup and closes it on shutdown — the
  store is reachable through :func:`get_store` only inside the lifespan;
* a **read** route reads a per-request ``store.snapshot()`` (a sync route in a
  threadpool worker, off the owner thread — correct because a snapshot is
  any-thread, ADR-002);
* a **write** route ``await``-s a closure shipped to the owner via
  :func:`submit_write`, which mutates + commits on the owner thread and returns
  the TID once durable;
* a **foreign-thread direct write** still raises ``WrongThreadError`` (unchanged
  — the whole reason the write goes through the owner);
* a **GraphQL** query resolves a 2-level nested graph (``Mineral`` →
  ``type_locality`` → ``Locality``) through the per-request DataLoader on a
  per-request snapshot context (:func:`graphql_context_getter`).

The store is file-backed (the lifespan opens it itself via ``Store.open``), so
these run on a ``tmp_path`` directory rather than the ``store_factory`` backends
— the lifespan *is* the open path under test. ``fastapi``/``pydantic``/
``strawberry`` ship with the ``web`` extra; ``httpx`` (``TestClient``'s
transport) is a dev dependency. All four importorskip so the bare suite stays
green without them (the ``tests/web/`` and ``tests/extras/`` precedent).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated, Any

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")
pytest.importorskip("fastapi", reason="datacrystal[web] extra not installed")
pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")
pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")

import strawberry
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from strawberry.fastapi import GraphQLRouter
from strawberry.tools import create_type
from strawberry.types import Info

import datacrystal as dc
from datacrystal import EntityView, Snapshot, Store, StoreClosedError, WrongThreadError
from datacrystal.web import (
    SNAPSHOT_CONTEXT_KEY as SNAP_KEY,
)
from datacrystal.web import (
    StrawberryReflector,
    Writer,
    create_app,
    get_store,
    graphql_context_getter,
    read_snapshot,
    store_lifespan,
    submit_write,
)

# --- a self-contained slice of the mineral cabinet (reference) ----------------


@dc.entity
class Locality:
    """Where a mineral was first found."""

    qid: Annotated[str, dc.Unique]
    name: str
    country: Annotated[str | None, dc.Index] = None


@dc.entity
class Mineral:
    """A mineral species, optionally pointing at its type locality."""

    qid: Annotated[str, dc.Unique]
    name: str
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None


@pytest.fixture
def seeded_path(tmp_path: Path) -> str:
    """Return the path of a closed store holding one Mineral pointing at one Locality.

    The seeding store is closed so the app's lifespan can re-open it (single writer).
    """
    path = str(tmp_path / "cab.store")
    store = dc.Store.open(path)
    gotthard = Locality(qid="L1", name="St Gotthard", country="CH")
    quartz = Mineral(qid="Q1", name="Quartz", mohs=7.0, type_locality=dc.Lazy.of(gotthard))
    store.store(quartz)
    store.commit()
    store.close()
    return path


# --- the apps under test --------------------------------------------------------


def _make_rest_app(path: str) -> FastAPI:
    """Build a FastAPI app over the #92 wiring: a snapshot GET and a submit POST.

    The write route is async (it runs on the owner loop thread); the read route is
    sync (a threadpool worker, off the owner, correct because a snapshot is any-thread).
    """
    app = create_app(path)

    @app.get("/minerals/{qid}")
    def read_mineral(qid: str, snap: Annotated[Snapshot, Depends(read_snapshot)]) -> dict[str, object]:
        matches = snap.query(Mineral.qid == qid)  # pyrefly: ignore[bad-argument-type]
        if not matches:
            return {"found": False}
        view = matches[0]
        return {"found": True, "qid": view.qid, "name": view.name, "mohs": view.mohs}

    @app.post("/minerals/{qid}")
    async def add_mineral(
        qid: str, name: str, writer: Annotated[Writer, Depends(submit_write)]
    ) -> dict[str, object]:
        def do_write(store: Store) -> int:
            # Plain data (the new TID) crosses back: a live entity would EntityEscapeError.
            store.store(Mineral(qid=qid, name=name))
            return store.commit() or store.last_tid

        tid = await writer(do_write)
        return {"committed_tid": tid}

    @app.post("/minerals/{qid}/direct")
    def add_mineral_direct(qid: str, store: Annotated[Store, Depends(get_store)]) -> dict[str, bool]:
        # A SYNC route runs on a threadpool worker, a foreign thread: a DIRECT live
        # write must raise WrongThreadError (ADR-001), proving submit() is mandatory.
        try:
            store.store(Mineral(qid=qid, name="should-not-land"))
            store.commit()
            return {"raised": False}
        except WrongThreadError:
            return {"raised": True}

    return app


def _add_peek_route(app: FastAPI, seen: list[Snapshot]) -> None:
    """Add GET /peek, which records the snapshot each request was handed."""

    @app.get("/peek")
    def peek(snap: Annotated[Snapshot, Depends(read_snapshot)]) -> dict[str, int]:
        seen.append(snap)
        return {"tid": snap.tid}


def _add_store_route(app: FastAPI, seen: list[Store]) -> None:
    """Add GET /store, which records the store behind get_store and reports its mineral count.

    The route is async so it runs on the owner loop thread, where the store may be read.
    """

    @app.get("/store")
    async def store_route(store: Annotated[Store, Depends(get_store)]) -> dict[str, int]:
        seen.append(store)
        return {"minerals": store.count(Mineral)}


def _make_graphql_app(path: str) -> FastAPI:
    """Build a FastAPI + Strawberry app with a ``mineral(qid)`` query.

    The root reads the per-request snapshot off the context; the reference edge is
    resolved through the per-request DataLoader (graphql_context_getter, #100).
    """
    mineral_gql = StrawberryReflector().reflect(Mineral)

    def mineral_resolver(qid: str, info: Info[Any, Any]) -> EntityView | None:
        snap: Snapshot = info.context[SNAP_KEY]
        matches = snap.query(Mineral.qid == qid)  # pyrefly: ignore[bad-argument-type]
        return matches[0] if matches else None

    mineral_field: Any = strawberry.field(
        resolver=mineral_resolver, graphql_type=mineral_gql | None, name="mineral"
    )
    schema = strawberry.Schema(query=create_type("Query", [mineral_field]))

    app = FastAPI(lifespan=store_lifespan(path))
    app.include_router(
        GraphQLRouter(schema, context_getter=graphql_context_getter), prefix="/graphql"
    )
    return app


def _make_probe_app(path: str, recorded: dict[str, int]) -> FastAPI:
    """Build an app whose POST /probe records the route's and the closure's thread ids."""
    app = create_app(path)

    @app.post("/probe")
    async def probe(writer: Annotated[Writer, Depends(submit_write)]) -> dict[str, bool]:
        route_thread = threading.get_ident()

        def on_owner(store: Store) -> int:
            return threading.get_ident()

        owner_thread = await writer(on_owner)
        recorded["route"] = route_thread
        recorded["owner"] = owner_thread
        return {"same": route_thread == owner_thread}

    return app


# --- the REST app: lifespan + read + write dependencies -----------------------


def test_a_route_needing_the_store_fails_before_the_lifespan_runs(seeded_path: str) -> None:
    """Given an app created over a seeded store path with a route depending on get_store
    When a client calls the route without entering the lifespan
    Then the route raises the missing-store RuntimeError
    """
    app = create_app(seeded_path)
    _add_store_route(app, [])
    client = TestClient(app)

    with pytest.raises(RuntimeError, match=r"no datacrystal store on app\.state"):
        client.get("/store")


def test_lifespan_opens_the_store_on_startup(seeded_path: str) -> None:
    """Given an app created over a seeded store path
    When a client enters the app's lifespan and calls a route depending on get_store
    Then the response is 200
    And the store holds the seeded quartz
    """
    app = create_app(seeded_path)
    _add_store_route(app, [])

    with TestClient(app) as client:
        resp = client.get("/store")

    assert resp.status_code == 200
    assert resp.json() == {"minerals": 1}


def test_lifespan_closes_the_store_on_shutdown(seeded_path: str) -> None:
    """Given an app whose lifespan opened the store
    When the client leaves the lifespan
    Then counting the store's minerals raises StoreClosedError
    """
    seen: list[Store] = []
    app = create_app(seeded_path)
    _add_store_route(app, seen)
    with TestClient(app) as client:
        client.get("/store")

    with pytest.raises(StoreClosedError):
        seen[0].count(Mineral)


def test_snapshot_get_reads_committed_state(seeded_path: str) -> None:
    """Given a committed quartz and a snapshot-backed GET route
    When a client GETs the quartz
    Then the response is 200
    And the body holds the quartz as committed
    """
    with TestClient(_make_rest_app(seeded_path)) as client:
        resp = client.get("/minerals/Q1")

    assert resp.status_code == 200
    assert resp.json() == {"found": True, "qid": "Q1", "name": "Quartz", "mohs": 7.0}


def test_submit_post_commits_and_returns_the_tid(seeded_path: str) -> None:
    """Given a POST route that ships its write through submit_write
    When a client POSTs a new calcite
    Then the response is 200
    And the committed TID is positive
    """
    with TestClient(_make_rest_app(seeded_path)) as client:
        resp = client.post("/minerals/Q2", params={"name": "Calcite"})

    assert resp.status_code == 200
    assert resp.json()["committed_tid"] > 0


def test_posted_mineral_is_read_back_by_a_snapshot_get(seeded_path: str) -> None:
    """Given a calcite POSTed through submit_write
    When a client GETs the calcite
    Then the response is 200
    And the body holds the calcite
    """
    with TestClient(_make_rest_app(seeded_path)) as client:
        client.post("/minerals/Q2", params={"name": "Calcite"})

        got = client.get("/minerals/Q2")

    assert got.status_code == 200
    assert got.json() == {"found": True, "qid": "Q2", "name": "Calcite", "mohs": None}


def test_foreign_thread_direct_write_still_raises_wrong_thread(seeded_path: str) -> None:
    """Given a sync route that writes the live store directly
    When a client POSTs to it
    Then the route reports WrongThreadError was raised
    """
    with TestClient(_make_rest_app(seeded_path)) as client:
        resp = client.post("/minerals/Q3/direct")

    assert resp.status_code == 200
    assert resp.json() == {"raised": True}


def test_rejected_direct_write_does_not_land(seeded_path: str) -> None:
    """Given a direct write from a foreign thread that raised WrongThreadError
    When a client GETs the mineral it tried to write
    Then the mineral is absent
    """
    with TestClient(_make_rest_app(seeded_path)) as client:
        client.post("/minerals/Q3/direct")

        got = client.get("/minerals/Q3")

    assert got.json() == {"found": False}


# --- the GraphQL app: per-request snapshot context + DataLoader ---------------


def test_graphql_resolves_two_level_nested_graph_through_the_loader(seeded_path: str) -> None:
    """Given a committed quartz pointing at St Gotthard and a GraphQL app
    When a client queries the quartz with its type locality
    Then the response is 200
    And the body holds no errors
    And the data holds the quartz with its type locality
    """
    query = '{ mineral(qid: "Q1") { qid name mohs typeLocality { qid name country } } }'

    with TestClient(_make_graphql_app(seeded_path)) as client:
        resp = client.post("/graphql", json={"query": query})

    body = resp.json()
    assert resp.status_code == 200
    assert body.get("errors") is None
    assert body["data"] == {
        "mineral": {
            "qid": "Q1",
            "name": "Quartz",
            "mohs": 7.0,
            "typeLocality": {"qid": "L1", "name": "St Gotthard", "country": "CH"},
        }
    }


# --- the dependencies in isolation (the contracts the routes above rely on) ----


def test_get_store_without_lifespan_fails_loudly() -> None:
    """Given an app wired without store_lifespan and a route depending on get_store
    When a client GETs the route
    Then the response is a 500
    """
    app = FastAPI()

    @app.get("/x")
    def x(store: Annotated[Store, Depends(get_store)]) -> dict[str, bool]:
        return {"ok": store is not None}

    # raise_server_exceptions=False: the RuntimeError surfaces as a 500 response
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/x")

    assert resp.status_code == 500


def test_read_snapshot_is_pooled_per_watermark(seeded_path: str) -> None:
    """Given an app whose /peek route records its snapshot
    When a client GETs /peek twice at one watermark
    Then both requests were handed the same snapshot
    And that snapshot is still open
    """
    seen: list[Snapshot] = []
    app = create_app(seeded_path)
    _add_peek_route(app, seen)

    with TestClient(app) as client:
        client.get("/peek")
        client.get("/peek")

        assert len(seen) == 2
        assert seen[0] is seen[1]
        assert seen[0].count(Mineral) == 1


def test_pooled_snapshot_is_closed_on_shutdown(seeded_path: str) -> None:
    """Given a pooled snapshot handed to a /peek request
    When the client leaves the lifespan
    Then counting the snapshot's minerals raises StoreClosedError
    """
    seen: list[Snapshot] = []
    app = create_app(seeded_path)
    _add_peek_route(app, seen)
    with TestClient(app) as client:
        client.get("/peek")

    with pytest.raises(StoreClosedError):
        seen[0].count(Mineral)


def test_snapshot_pool_refreshes_when_a_commit_advances_the_watermark(seeded_path: str) -> None:
    """Given a /peek read at watermark T and a POST that commits T+1
    When a client GETs /peek again
    Then the second request got a different snapshot
    And the second snapshot's tid is later than the first's
    And the superseded snapshot is closed
    """
    seen: list[Snapshot] = []
    app = _make_rest_app(seeded_path)
    _add_peek_route(app, seen)

    with TestClient(app) as client:
        client.get("/peek")
        client.post("/minerals/NEW", params={"name": "Beryl"})

        client.get("/peek")

        assert len(seen) == 2
        assert seen[0] is not seen[1]
        assert seen[0].tid < seen[1].tid
        with pytest.raises(StoreClosedError):
            seen[0].count(Mineral)


def test_store_lifespan_factory_serves_the_store_to_a_plain_app(seeded_path: str) -> None:
    """Given a plain FastAPI app built with the lifespan store_lifespan returns for a path
    When a client enters it and calls a route depending on get_store
    Then the response is 200
    And the store holds the seeded quartz
    """
    app = FastAPI(lifespan=store_lifespan(seeded_path))
    _add_store_route(app, [])

    with TestClient(app) as client:
        resp = client.get("/store")

    assert resp.status_code == 200
    assert resp.json() == {"minerals": 1}


def test_submit_write_runs_on_the_owner_not_the_caller_thread(seeded_path: str) -> None:
    """Given an async route that submits a closure returning its thread id
    When a client POSTs to the route
    Then the closure ran on the route coroutine's own thread, the owner thread
    """
    recorded: dict[str, int] = {}

    with TestClient(_make_probe_app(seeded_path, recorded)) as client:
        resp = client.post("/probe")

    assert resp.status_code == 200
    assert recorded["route"] == recorded["owner"]
