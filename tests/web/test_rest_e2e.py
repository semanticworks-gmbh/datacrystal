"""datacrystal[web] — the REST round-trip over a *real* FastAPI app (#98 e2e).

Sprint 9 REST boundary (#98 / #49 spike S4, build plan #23): the acceptance test
that ties #96 (``entity_model``), #97 (``to_pydantic``) and #98 (``from_pydantic``)
together through an actual FastAPI application — not the framework wiring of #92
(that lands later), just the minimal mount that proves the boundary works:

* ``GET /minerals/{qid}`` declares ``response_model=MineralPublic`` and returns a
  **snapshot** DTO (``to_pydantic(view, face="public")``) — a snapshot read is
  thread-safe (ADR-001 / ADR-002), so the route is correct even though FastAPI
  runs a sync handler in a threadpool worker, off the store's owner thread;
* ``POST /minerals`` parses its body into ``MineralCreate`` (FastAPI does the
  validation) and ``from_pydantic`` rebuilds a fresh entity the caller can
  ``store.root``/``upsert`` — the request half of the round-trip.

``fastapi``/``pydantic`` ship with the ``web`` extra; ``httpx`` (FastAPI's
``TestClient`` transport) is a dev dependency. All three importorskip so the bare
suite stays green without them (mirrors ``tests/extras/`` for the fts/arrow extras
and the duckdb-in-arrow-tests precedent).
"""

from __future__ import annotations

import warnings
from typing import Annotated

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")
pytest.importorskip("fastapi", reason="datacrystal[web] extra not installed")
pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

import datacrystal as dc
from datacrystal.web import entity_model, from_pydantic, to_pydantic


@dc.entity
class Mineral:
    """A mineral species."""

    qid: Annotated[str, dc.Unique]
    name: Annotated[str, dc.FullText]
    mohs: float | None = None


MineralPublic = entity_model(Mineral, face="public")
MineralCreate = entity_model(Mineral, face="create")


def _make_app(store: dc.Store) -> FastAPI:
    """Build a minimal real FastAPI app mounting the #98 REST boundary over ``store``.

    The read route is snapshot-backed (thread-safe), so it is correct off the
    owner thread; the write route reconstructs a fresh entity via
    ``from_pydantic`` and echoes what it rebuilt.
    """
    app = FastAPI()

    @app.get("/minerals/{qid}", response_model=MineralPublic)
    def read_mineral(qid: str) -> BaseModel:
        # A snapshot read is callable from any thread (ADR-001 / ADR-002), so the
        # threadpool-dispatched sync handler never violates owner confinement.
        with store.snapshot() as snap:
            matches = snap.query(Mineral.qid == qid)  # pyrefly: ignore[bad-argument-type]
            if not matches:
                raise HTTPException(status_code=404, detail=f"no mineral {qid!r}")
            return to_pydantic(matches[0], face="public")

    @app.post("/minerals")
    def create_mineral(body: MineralCreate) -> dict[str, object]:
        # from_pydantic (no store) is thread-free: it rebuilds a fresh entity
        # the caller can later store.root/upsert on the owner thread.
        entity = from_pydantic(body, Mineral)
        return {"qid": entity.qid, "name": entity.name}

    return app


def test_get_returns_snapshot_public_dto(store: dc.Store) -> None:
    """Given a committed quartz
    When a client GETs /minerals/Q-QTZ
    Then the response is 200
    And the body is the public DTO with the engine-assigned oid
    """
    mineral = Mineral(qid="Q-QTZ", name="quartz", mohs=7.0)
    store.root = mineral
    store.commit()
    oid = store.store(mineral)
    client = TestClient(_make_app(store))

    resp = client.get("/minerals/Q-QTZ")

    assert resp.status_code == 200
    assert resp.json() == {"oid": oid, "qid": "Q-QTZ", "name": "quartz", "mohs": 7.0}


def test_get_missing_is_404(store: dc.Store) -> None:
    """Given an empty store
    When a client GETs a mineral that does not exist
    Then the response is 404
    """
    client = TestClient(_make_app(store))

    resp = client.get("/minerals/NOPE")

    assert resp.status_code == 404


def test_route_reading_an_unseen_type_warns_once_not_per_request(store: dc.Store) -> None:
    """Given an empty store behind a snapshot-backed route
    When a client GETs a missing mineral three times
    Then UnseenTypeWarning was raised exactly once
    """
    client = TestClient(_make_app(store))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client.get("/minerals/NOPE")
        client.get("/minerals/NOPE")
        client.get("/minerals/NOPE")

    assert [w.category for w in caught].count(dc.UnseenTypeWarning) == 1


def test_post_body_parses_and_from_pydantic_rebuilds_the_entity(store: dc.Store) -> None:
    """Given a valid create body
    When a client POSTs it to /minerals
    Then the response is 200
    And the route rebuilt the entity from the body
    """
    client = TestClient(_make_app(store))

    resp = client.post("/minerals", json={"qid": "Q-NEW", "name": "fresh", "mohs": 5.5})

    assert resp.status_code == 200
    assert resp.json() == {"qid": "Q-NEW", "name": "fresh"}


def test_post_then_owner_persists_and_get_round_trips(store: dc.Store) -> None:
    """Given a body POSTed and then rebuilt and committed on the owner thread
    When a client GETs the mineral
    Then the response is 200
    And the public DTO carries the name
    And it carries the hardness
    """
    client = TestClient(_make_app(store))
    payload = {"qid": "Q-RT", "name": "roundtrip", "mohs": 6.0}
    client.post("/minerals", json=payload)
    store.root = from_pydantic(MineralCreate.model_validate(payload), Mineral)
    store.commit()

    resp = client.get("/minerals/Q-RT")

    assert resp.status_code == 200
    assert resp.json()["name"] == "roundtrip"
    assert resp.json()["mohs"] == 6.0


def test_post_rejects_a_malformed_body(store: dc.Store) -> None:
    """Given a create body missing the required qid
    When a client POSTs it to /minerals
    Then the response is 422
    """
    client = TestClient(_make_app(store))

    resp = client.post("/minerals", json={"name": "no-qid"})

    assert resp.status_code == 422
