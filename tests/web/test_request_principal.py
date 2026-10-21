"""W2-8: the per-request principal seam (epic #168, ADR-008).

``get_principal`` resolves the request identity; ``submit_write`` wraps every
shipped closure in ``acting_as(principal)`` INSIDE the closure body (queued
work runs ambient — the W1 rule — so an outside wrap would never arrive).
Web writes stamp the request principal or anonymous, NEVER the operator's
store-opening identity. Denial maps to 403. Federation ``/v1/submit``
refuses protected-class ops pre-flight (R16 interim) and keeps stamping
anonymous.

Transport: ``asyncio.run`` + ``httpx.AsyncClient(ASGITransport)`` so handlers
run on the store's owner thread (the sync-TestClient deadlock lesson).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any

import pytest

pytest.importorskip("httpx", reason="pip install httpx (ASGITransport)")

import httpx
from fastapi import Depends, FastAPI

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router, get_principal, submit_write
from datacrystal.web._app import _OwnerWriter
from tests.conftest import Mineral

_MINERAL = f"{Mineral.__module__}:{Mineral.__qualname__}"

TEAM = 2
OPERATOR = dc.Principal(uid=1, memberships={dc.WORLD: dc.ADMIN})
ANNA = dc.Principal(uid=41, memberships={TEAM: dc.STAFF})
BEA = dc.Principal(uid=42, memberships={TEAM: dc.CURATOR})


@dc.entity(protected=True)
class CuratedEntry:
    """A protected entry fenced to a team."""

    label: Annotated[str, dc.Unique]
    note: str = ""


class _StampSink:
    """A delta consumer recording the actor stamp of every commit."""

    def __init__(self, at: int) -> None:
        self.watermark = at
        self.stamps: list[int] = []

    def apply(self, delta: dict[str, Any]) -> None:
        self.stamps.append(delta["actor"])
        self.watermark = delta["tid"]


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
    app: FastAPI,
    method: str,
    path: str,
    params: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    """Send one request to ``app`` over an ASGI transport on the calling thread."""

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://web") as client:
            return await client.request(method, path, params=params, json=json)

    return asyncio.run(send())


def _write_app(store: dc.Store) -> FastAPI:
    app = FastAPI()

    async def create_mineral(
        qid: str, write: Annotated[_OwnerWriter, Depends(submit_write)]
    ) -> dict[str, str]:
        def fn(s: dc.Store) -> dict[str, str]:
            s.upsert(Mineral(qid=qid, name=qid))
            s.commit()
            return {"qid": qid}

        return await write(fn)

    async def edit_entry(
        label: str, note: str, write: Annotated[_OwnerWriter, Depends(submit_write)]
    ) -> dict[str, str]:
        def fn(s: dc.Store) -> dict[str, str]:
            rec = s.get(CuratedEntry, label=label)
            if rec is None:
                raise LookupError(f"no entry {label}")
            rec.note = note
            s.commit()
            return {"label": label}

        return await write(fn)

    app.state.dc_store = store  # the get_store state key
    app.add_api_route("/minerals", create_mineral, methods=["POST"])
    app.add_api_route("/entries", edit_entry, methods=["POST"])
    return app


def _federation_app(store: dc.Store, log: DeltaLog) -> FastAPI:
    app = FastAPI()
    app.include_router(federation_router(store, log))
    return app


def _commit_from_a_foreign_thread(store: dc.Store) -> None:
    """Thread target: queue a raw ``store.submit`` closure that stores and commits a mineral."""

    def fn() -> None:
        store.store(Mineral(qid="Q-thread", name="t"))
        store.commit()

    store.submit(fn)


@pytest.fixture
def operator_store() -> Iterator[dc.Store]:
    """Provide a store opened by the OPERATOR: web writes must not inherit that identity."""
    store = dc.Store._from_backend(MemoryBackend(), principal=OPERATOR)
    yield store
    store.close()


@pytest.fixture
def stamps(operator_store: dc.Store) -> _StampSink:
    """Attach and return a sink recording the actor stamp of each commit on the operator store."""
    sink = _StampSink(operator_store.last_tid)
    operator_store.attach(sink)
    return sink


@pytest.fixture
def fenced_ledger(operator_store: dc.Store) -> Iterator[dc.Store]:
    """Provide the operator store holding a ledger BEA fenced to CURATOR writes."""
    with operator_store.acting_as(BEA):
        entry = CuratedEntry(label="ledger")
        operator_store.store(entry)
        dc.share(entry, TEAM, read=dc.VIEWER, write=dc.CURATOR)
        operator_store.commit()
    yield operator_store
    operator_store.discard()


@pytest.fixture
def log(store: dc.Store, tmp_path: Path) -> DeltaLog:
    """Create a delta log and attach it to the store."""
    delta_log = DeltaLog(tmp_path / "log")
    store.attach(delta_log)
    return delta_log


def test_write_without_a_resolved_principal_stamps_anonymous(
    operator_store: dc.Store, stamps: _StampSink
) -> None:
    """Given a store opened by the operator and a request with no resolved principal
    When the client POSTs a mineral
    Then the response is 200
    And the commit is stamped anonymous 0, never the operator
    """
    app = _write_app(operator_store)

    resp = _send(app, "POST", "/minerals", params={"qid": "Q-anon"})

    assert resp.status_code == 200
    assert stamps.stamps == [0]


def test_write_stamps_the_request_principal(operator_store: dc.Store, stamps: _StampSink) -> None:
    """Given a store opened by the operator and a request resolved as Anna
    When the client POSTs a mineral
    Then the response is 200
    And the commit is stamped with Anna's uid, never the operator's
    """
    app = _write_app(operator_store)
    app.dependency_overrides[get_principal] = _requests_as(ANNA)

    resp = _send(app, "POST", "/minerals", params={"qid": "Q-anna"})

    assert resp.status_code == 200
    assert stamps.stamps == [ANNA.uid]


def test_interleaved_requests_and_foreign_thread_keep_their_own_identity(
    operator_store: dc.Store, stamps: _StampSink
) -> None:
    """Given requests resolved as Anna then Bea and a foreign thread queueing raw work between
    When the client POSTs a mineral, the thread queues its commit, and the client POSTs again
    Then both responses are 200
    And the stamps are Anna's, the operator's for the foreign closure, and Bea's
    """
    app = _write_app(operator_store)
    app.dependency_overrides[get_principal] = _requests_as_in_turn(ANNA, BEA)

    first = _send(app, "POST", "/minerals", params={"qid": "Q-a"})
    foreign = threading.Thread(target=_commit_from_a_foreign_thread, args=(operator_store,))
    foreign.start()
    foreign.join(timeout=10)
    second = _send(app, "POST", "/minerals", params={"qid": "Q-b"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert sorted(stamps.stamps) == [OPERATOR.uid, ANNA.uid, BEA.uid]


def test_denied_write_maps_to_403_and_burns_no_tid(fenced_ledger: dc.Store) -> None:
    """Given a ledger fenced to CURATOR writes and a request resolved as STAFF Anna
    When the client POSTs an edit of the ledger
    Then the response is 403
    And the error detail is write-denied
    And the store's last tid is unchanged, gapless
    """
    tid_before = fenced_ledger.last_tid
    app = _write_app(fenced_ledger)
    app.dependency_overrides[get_principal] = _requests_as(ANNA)

    resp = _send(app, "POST", "/entries", params={"label": "ledger", "note": "tamper"})

    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "write-denied"
    assert fenced_ledger.last_tid == tid_before


def test_federation_refuses_protected_class_ops_preflight(store: dc.Store, log: DeltaLog) -> None:
    """Given a committed protected entry and a federation submit op on that class
    When a client POSTs it to /v1/submit
    Then the response is 403
    And the error detail is write-denied
    And the store's last tid is unchanged
    """
    with store.acting_as(BEA):
        store.store(CuratedEntry(label="synced?"))
        store.commit()
    tid_before = store.last_tid
    op = {
        "type": f"{CuratedEntry.__module__}:{CuratedEntry.__qualname__}",
        "key": "label",
        "fields": {"label": "synced?", "note": "from-follower"},
        "base": None,
    }

    resp = _send(_federation_app(store, log), "POST", "/v1/submit", json={"ops": [op]})

    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "write-denied"
    assert store.last_tid == tid_before


def test_refused_federation_op_leaves_nothing_in_the_commit_buffer(
    store: dc.Store, log: DeltaLog
) -> None:
    """Given a protected-class federation op refused pre-flight
    When Bea then commits
    Then there is nothing to commit
    """
    op = {
        "type": f"{CuratedEntry.__module__}:{CuratedEntry.__qualname__}",
        "key": "label",
        "fields": {"label": "synced?", "note": "from-follower"},
        "base": None,
    }
    _send(_federation_app(store, log), "POST", "/v1/submit", json={"ops": [op]})

    with store.acting_as(BEA):
        tid = store.commit()

    assert tid is None


def test_unprotected_federation_op_is_accepted(store: dc.Store, log: DeltaLog) -> None:
    """Given a federation submit op on an unprotected class
    When a client POSTs it to /v1/submit
    Then the response is 200
    """
    op = {
        "type": _MINERAL,
        "key": "qid",
        "fields": {"qid": "Q-fed", "name": "fed"},
        "base": None,
    }

    resp = _send(_federation_app(store, log), "POST", "/v1/submit", json={"ops": [op]})

    assert resp.status_code == 200


def test_unprotected_federation_op_stamps_anonymous(store: dc.Store, log: DeltaLog) -> None:
    """Given a federation submit op on an unprotected class and a stamp sink
    When a client POSTs it to /v1/submit
    Then the commit is stamped anonymous 0
    """
    sink = _StampSink(store.last_tid)
    store.attach(sink)
    op = {
        "type": _MINERAL,
        "key": "qid",
        "fields": {"qid": "Q-fed", "name": "fed"},
        "base": None,
    }

    _send(_federation_app(store, log), "POST", "/v1/submit", json={"ops": [op]})

    assert sink.stamps == [0]


def test_federation_dc_field_never_lands_as_a_label_write(store: dc.Store, log: DeltaLog) -> None:
    """Given a federation op on an unprotected class smuggling a _dc_owner field
    When a client POSTs it to /v1/submit
    Then the response is 409
    And the error detail is schema-skew, never a silent label write
    """
    op = {
        "type": _MINERAL,
        "key": "qid",
        "fields": {"qid": "Q-smuggle", "_dc_owner": 999},
        "base": None,
    }

    resp = _send(_federation_app(store, log), "POST", "/v1/submit", json={"ops": [op]})

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "schema-skew"
