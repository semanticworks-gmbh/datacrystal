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
  validation) and ``from_pydantic`` rebuilds a ``STATE_NEW`` entity the caller can
  ``store.root``/``upsert`` — the request half of the round-trip.

``fastapi``/``pydantic`` ship with the ``web`` extra; ``httpx`` (FastAPI's
``TestClient`` transport) is a dev dependency. All three importorskip so the bare
suite stays green without them (mirrors ``tests/extras/`` for the fts/arrow extras
and the duckdb-in-arrow-tests precedent).
"""

# create_model DTOs are dynamically built types pyright cannot see the fields of
# (``dto.qid``) nor accept as a function annotation (``body: MineralCreate``), and
# the magic-query ``Mineral.qid == qid`` returns a Condition untypeable by design.
# File-scoped pragmas, exactly like the magic-query tests (tests/unit/test_query.py).
# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false
# pyright: reportInvalidTypeForm=false, reportArgumentType=false

from __future__ import annotations

from typing import Annotated

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")
pytest.importorskip("fastapi", reason="datacrystal[web] extra not installed")
pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import datacrystal as dc
from datacrystal._entity import STATE_NEW, oid_of, state_of
from datacrystal.web import entity_model, from_pydantic, to_pydantic


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: Annotated[str, dc.FullText]
    mohs: float | None = None


MineralPublic = entity_model(Mineral, face="public")
MineralCreate = entity_model(Mineral, face="create")


def _make_app(store: dc.Store) -> FastAPI:
    """A minimal real FastAPI app mounting the #98 REST boundary over ``store``.

    The read route is snapshot-backed (thread-safe), so it is correct off the
    owner thread; the write route reconstructs a STATE_NEW entity via
    ``from_pydantic`` and reports that it landed in the right lifecycle state."""
    app = FastAPI()

    @app.get("/minerals/{qid}", response_model=MineralPublic)
    def read_mineral(qid: str):  # noqa: ANN202 — FastAPI infers from response_model
        # A snapshot read is callable from any thread (ADR-001 / ADR-002), so the
        # threadpool-dispatched sync handler never violates owner confinement.
        with store.snapshot() as snap:
            matches = snap.query(Mineral.qid == qid)
            if not matches:
                raise HTTPException(status_code=404, detail=f"no mineral {qid!r}")
            return to_pydantic(matches[0], face="public")

    @app.post("/minerals")
    def create_mineral(body: MineralCreate):  # noqa: ANN202
        # from_pydantic (no store) is thread-free: it rebuilds a STATE_NEW entity
        # the caller can later store.root/upsert on the owner thread.
        entity = from_pydantic(body, Mineral)
        return {"qid": entity.qid, "name": entity.name,
                "state_new": state_of(entity) == STATE_NEW,
                "has_oid": oid_of(entity) is not None}

    return app


def test_get_returns_snapshot_public_dto(store) -> None:
    # Seed on the owner (test) thread, then drive the app: the GET returns the
    # committed entity as a MineralPublic, oid included.
    mineral = Mineral(qid="Q-QTZ", name="quartz", mohs=7.0)
    store.root = mineral
    store.commit()
    expected_oid = oid_of(mineral)

    client = TestClient(_make_app(store))
    resp = client.get("/minerals/Q-QTZ")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"oid": expected_oid, "qid": "Q-QTZ", "name": "quartz", "mohs": 7.0}


def test_get_missing_is_404(store) -> None:
    client = TestClient(_make_app(store))
    resp = client.get("/minerals/NOPE")
    assert resp.status_code == 404


def test_post_body_parses_and_from_pydantic_yields_state_new(store) -> None:
    # A POST body validates into MineralCreate; from_pydantic rebuilds a STATE_NEW
    # entity inside the real request path (the request half of the round-trip).
    client = TestClient(_make_app(store))
    resp = client.post("/minerals", json={"qid": "Q-NEW", "name": "fresh", "mohs": 5.5})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"qid": "Q-NEW", "name": "fresh", "state_new": True, "has_oid": False}


def test_post_then_owner_persists_and_get_round_trips(store) -> None:
    # Full round-trip: POST a body, reconstruct it on the owner thread exactly as a
    # #92 app would marshal the write, commit, then GET it back as a public DTO.
    client = TestClient(_make_app(store))
    payload = {"qid": "Q-RT", "name": "roundtrip", "mohs": 6.0}
    assert client.post("/minerals", json=payload).status_code == 200

    # The owner thread does the actual write (single-writer; #92 owns marshalling).
    created = MineralCreate.model_validate(payload)
    entity = from_pydantic(created, Mineral)
    store.root = entity
    store.commit()

    resp = client.get("/minerals/Q-RT")
    assert resp.status_code == 200
    assert resp.json()["name"] == "roundtrip"
    assert resp.json()["mohs"] == 6.0


def test_post_rejects_a_malformed_body(store) -> None:
    # FastAPI validates the body against MineralCreate before from_pydantic runs:
    # a missing required field is a 422, never a torn reconstruction.
    client = TestClient(_make_app(store))
    resp = client.post("/minerals", json={"name": "no-qid"})  # qid is required
    assert resp.status_code == 422
