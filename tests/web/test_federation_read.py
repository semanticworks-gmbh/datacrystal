"""#149 — the coordinator federation read surface (FEDERATION-WIRE-v1).

Real ASGI over BOTH ``store_factory`` backends: ``/v1/head`` reports the
watermark; ``/v1/deltas`` serves the exact COMMIT-DELTA-v1 frames in strict TID
order (each decode-round-trips); the auth seam rejects an unauthed request.
"""

from __future__ import annotations

import inspect
import struct
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

import datacrystal as dc
from datacrystal.contract.applier import decode_delta
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router
from tests.conftest import Mineral


def _parse_frames(blob: bytes) -> list[dict[str, Any]]:
    """Unpack the length-prefixed ``>Q`` frames back into delta dicts."""
    deltas: list[dict[str, Any]] = []
    offset = 0
    while offset < len(blob):
        (size,) = struct.unpack_from(">Q", blob, offset)
        offset += 8
        deltas.append(decode_delta(blob[offset : offset + size]))
        offset += size
    return deltas


def _app_serving(router: APIRouter) -> FastAPI:
    """Build an app that mounts the federation router."""
    app = FastAPI()
    app.include_router(router)
    return app


def _endpoints_by_path(router: APIRouter) -> dict[str, Any]:
    """Map each route path of the router to its endpoint function."""
    return {route.path: route.endpoint for route in router.routes}  # type: ignore[attr-defined]


def _require_token(x_api_key: str | None = Header(default=None)) -> None:
    """Reject a request whose ``x-api-key`` header is not the shared secret."""
    if x_api_key != "secret":
        raise HTTPException(status_code=401, detail="unauthorized")


@pytest.fixture
def log(tmp_path: Path) -> DeltaLog:
    """Create a fresh delta log in ``tmp_path``."""
    return DeltaLog(tmp_path / "deltalog")


@pytest.fixture
def tids(store: dc.Store, log: DeltaLog) -> list[int]:
    """Attach the log to the store, commit three minerals and return the TIDs."""
    store.attach(log)
    committed: list[int] = []
    for qid, name in (("Q1", "Quartz"), ("Q2", "Calcite"), ("Q3", "Gold")):
        store.store(Mineral(qid=qid, name=name))
        tid = store.commit()
        if tid is None:
            raise RuntimeError(f"commit of {qid} produced no transaction")
        committed.append(tid)
    return committed


@pytest.fixture
def stamped_log(store: dc.Store, log: DeltaLog) -> DeltaLog:
    """Attach the log, commit one mineral as uid 2 and one outside any scope."""
    store.attach(log)
    with store.acting_as(dc.Principal(uid=2)):
        store.store(Mineral(qid="Q7", name="Azurite"))
        store.commit()
    store.store(Mineral(qid="Q8", name="Topaz"))
    store.commit()
    return log


def test_head_reports_the_watermark_and_the_wire_format(
    store: dc.Store, log: DeltaLog, tids: list[int]
) -> None:
    """Given a coordinator that committed three minerals
    When a client GETs /v1/head
    Then the body names the last transaction id, the delta format and version 2
    """
    with TestClient(_app_serving(federation_router(store, log))) as client:
        head = client.get("/v1/head").json()

    assert head == {"tid": tids[-1], "format": "datacrystal-delta", "version": 2}


def test_head_tid_is_the_stores_last_tid(
    store: dc.Store, log: DeltaLog, tids: list[int]
) -> None:
    """Given a coordinator that committed three minerals
    When a client GETs /v1/head
    Then the reported tid is the store's last_tid
    """
    with TestClient(_app_serving(federation_router(store, log))) as client:
        head = client.get("/v1/head").json()

    assert head["tid"] == store.last_tid


def test_deltas_serve_every_frame_as_octets(
    store: dc.Store, log: DeltaLog, tids: list[int]
) -> None:
    """Given a coordinator that committed three minerals
    When a client GETs /v1/deltas after 0
    Then the response is 200 octet-stream
    And the frames carry exactly the three transaction ids in strict order
    And every frame is a well-formed delta of the format and version 2
    """
    with TestClient(_app_serving(federation_router(store, log))) as client:
        resp = client.get("/v1/deltas", params={"after": 0})

    deltas = _parse_frames(resp.content)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert [d["tid"] for d in deltas] == tids
    assert all(d["f"] == "datacrystal-delta" and d["v"] == 2 for d in deltas)


def test_deltas_past_the_watermark_are_empty(
    store: dc.Store, log: DeltaLog, tids: list[int]
) -> None:
    """Given a coordinator whose last transaction is the third
    When a client GETs /v1/deltas after the last transaction id
    Then no frame is served
    """
    with TestClient(_app_serving(federation_router(store, log))) as client:
        resp = client.get("/v1/deltas", params={"after": tids[-1]})

    assert _parse_frames(resp.content) == []


def test_deltas_resume_mid_stream_serves_only_the_tail(
    store: dc.Store, log: DeltaLog, tids: list[int]
) -> None:
    """Given a coordinator that committed three minerals
    When a client GETs /v1/deltas after the first transaction id
    Then only the second and third frames are served
    """
    with TestClient(_app_serving(federation_router(store, log))) as client:
        resp = client.get("/v1/deltas", params={"after": tids[0]})

    assert [d["tid"] for d in _parse_frames(resp.content)] == tids[1:]


def test_actor_stamps_cross_the_wire_inside_unchanged_frames(
    store: dc.Store, log: DeltaLog, stamped_log: DeltaLog
) -> None:
    """Given a coordinator that committed once as uid 2 and once outside any scope
    When a client GETs /v1/deltas after 0
    Then the frames are stamped actor 2 and then anonymous 0
    And every frame carries an integer timestamp
    """
    with TestClient(_app_serving(federation_router(store, log))) as client:
        resp = client.get("/v1/deltas", params={"after": 0})

    deltas = _parse_frames(resp.content)
    assert [d["actor"] for d in deltas] == [2, 0]
    assert all(isinstance(d["at"], int) for d in deltas)


def test_follower_bootstraps_from_the_stamped_stream(
    store: dc.Store, log: DeltaLog, stamped_log: DeltaLog
) -> None:
    """Given a coordinator serving stamped v2 deltas
    When a follower bootstraps from it
    Then the follower finds both committed minerals
    """
    app = _app_serving(federation_router(store, log))
    with TestClient(app) as client, dc.Store.follower("http://coord", client=client) as follower:
        assert {m.qid for m in follower.query(Mineral)} == {"Q7", "Q8"}


def test_read_routes_are_async(store: dc.Store, log: DeltaLog, tids: list[int]) -> None:
    """Given the federation router of a coordinator
    When its route endpoints are inspected
    Then /v1/head is a coroutine function
    And /v1/deltas is a coroutine function
    """
    endpoints = _endpoints_by_path(federation_router(store, log))

    assert inspect.iscoroutinefunction(endpoints["/v1/head"])
    assert inspect.iscoroutinefunction(endpoints["/v1/deltas"])


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/v1/head"), ("GET", "/v1/deltas"), ("POST", "/v1/submit")],
)
def test_auth_seam_rejects_an_unauthenticated_request(
    store: dc.Store, log: DeltaLog, tids: list[int], method: str, path: str
) -> None:
    """Given a router guarded by an api-key dependency
    When a client calls a route without the key
    Then the response is 401
    """
    router = federation_router(store, log, dependencies=[Depends(_require_token)])

    with TestClient(_app_serving(router)) as client:
        resp = client.request(method, path, params={"after": 0}, json={"ops": []})

    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/v1/head", "/v1/deltas"])
def test_auth_seam_accepts_a_request_with_the_key(
    store: dc.Store, log: DeltaLog, tids: list[int], path: str
) -> None:
    """Given a router guarded by an api-key dependency
    When a client GETs a read route with the key
    Then the response is 200
    """
    router = federation_router(store, log, dependencies=[Depends(_require_token)])

    with TestClient(_app_serving(router)) as client:
        resp = client.get(path, params={"after": 0}, headers={"x-api-key": "secret"})

    assert resp.status_code == 200
