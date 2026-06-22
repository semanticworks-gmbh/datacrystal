"""POST /v1/submit — fan-in (#152) + the fail-closed guards (#154 schema-skew /
Unique-key, #155 OCC / idempotency).

The handler does owner-confined writes (store.submit -> upsert -> commit), so the
test runs the store on the loop thread: ``asyncio.run`` + ``httpx.AsyncClient``
over an ``ASGITransport`` means the handler runs on the same thread that owns the
store, so ``store.submit`` runs inline — as production under uvicorn (no
pytest-asyncio, matching tests/unit/test_async.py). Over both backends.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("httpx", reason="pip install httpx (ASGITransport)")

import asyncio

import httpx
from fastapi import FastAPI

import datacrystal as dc
from datacrystal._entity import oid_of, type_info
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router
from tests.conftest import Mineral

_MINERAL = f"{Mineral.__module__}:{Mineral.__qualname__}"


async def _post(store, log, body: dict[str, Any]) -> httpx.Response:
    app = FastAPI()
    app.include_router(federation_router(store, log))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://coord") as client:
        return await client.post("/v1/submit", json=body)


def _op(qid: str, base: str | None = None, **fields: Any) -> dict[str, Any]:
    return {"type": _MINERAL, "key": "qid", "fields": {"qid": qid, **fields}, "base": base}


def _digest(store, qid: str) -> str | None:
    """The current OCC base for a Mineral — what a follower would carry on update."""
    return store._payload_digest(type_info(Mineral), "qid", qid)


def test_submit_creates_then_updates_with_base(store_factory, tmp_path) -> None:
    store = store_factory()
    log = DeltaLog(tmp_path / "log")
    store.attach(log)
    try:

        async def go() -> int:
            r1 = await _post(store, log, {"ops": [_op("Q1", name="Quartz", mohs=7.0)]})
            assert r1.status_code == 200, r1.text
            d1 = r1.json()
            assert d1["applied_tid"] > 0 and set(d1["keys"]) == {"Q1"}
            oid = d1["keys"]["Q1"]
            # an UPDATE must carry the current base (OCC) — same key → same OID
            base = _digest(store, "Q1")
            assert base is not None
            r2 = await _post(store, log, {"ops": [_op("Q1", base=base, name="Quartz", mohs=7.5)]})
            assert r2.status_code == 200, r2.text
            assert r2.json()["keys"]["Q1"] == oid  # no second OID minted
            return oid

        oid = asyncio.run(go())
        m = store.get(Mineral, qid="Q1")
        assert m is not None and m.mohs == 7.5 and oid_of(m) == oid
    finally:
        store.close()


def test_submit_batch_is_all_or_nothing(store_factory, tmp_path) -> None:
    store = store_factory()
    log = DeltaLog(tmp_path / "log")
    store.attach(log)
    try:

        async def go() -> None:
            bad = {"type": "nope.Module:Ghost", "key": "qid", "fields": {"qid": "X"}, "base": None}
            resp = await _post(store, log, {"ops": [_op("Q2", name="Calcite"), bad]})
            assert resp.status_code == 422, resp.text

        asyncio.run(go())
        assert store.last_tid == 0 and store.get(Mineral, qid="Q2") is None
    finally:
        store.close()


def test_submit_rejects_malformed_envelope(store_factory, tmp_path) -> None:
    store = store_factory()
    log = DeltaLog(tmp_path / "log")
    store.attach(log)
    try:

        async def go() -> None:
            for body in (
                {"ops": "notalist"},  # ops not a list
                {"ops": [5]},  # an op that is not an object
                {"ops": [{"type": _MINERAL}]},  # an op missing key/fields
            ):
                resp = await _post(store, log, body)
                assert resp.status_code == 422, (body, resp.text)

        asyncio.run(go())
        assert store.last_tid == 0  # fail closed: a malformed envelope writes nothing
    finally:
        store.close()


def test_submit_rejects_schema_skew(store_factory, tmp_path) -> None:
    store = store_factory()
    log = DeltaLog(tmp_path / "log")
    store.attach(log)
    try:

        async def go() -> None:
            # a field the coordinator's Mineral class does not have (cid-lineage skew)
            resp = await _post(store, log, {"ops": [_op("QS", name="x", bogus_field=1)]})
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["error"] == "schema-skew"

        asyncio.run(go())
        # fail closed: never silently dropped + applied — nothing landed
        assert store.last_tid == 0 and store.get(Mineral, qid="QS") is None
    finally:
        store.close()


def test_submit_rejects_non_unique_key(store_factory, tmp_path) -> None:
    store = store_factory()
    log = DeltaLog(tmp_path / "log")
    store.attach(log)
    try:

        async def go() -> None:
            # 'name' is not a dc.Unique field — a natural key must be Unique
            bad = {"type": _MINERAL, "key": "name", "fields": {"qid": "QN", "name": "x"}, "base": None}
            resp = await _post(store, log, {"ops": [bad]})
            assert resp.status_code == 422, resp.text

        asyncio.run(go())
        assert store.last_tid == 0
    finally:
        store.close()


def test_submit_occ_rejects_stale_base(store_factory, tmp_path) -> None:
    store = store_factory()
    log = DeltaLog(tmp_path / "log")
    store.attach(log)
    try:

        async def go() -> None:
            assert (await _post(store, log, {"ops": [_op("Q1", name="Quartz", mohs=7.0)]})).status_code == 200
            # update carrying a STALE/wrong base → conflict, never last-writer-wins
            resp = await _post(store, log, {"ops": [_op("Q1", base="0" * 64, name="Quartz", mohs=9.9)]})
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["error"] == "conflict"

        asyncio.run(go())
        m = store.get(Mineral, qid="Q1")
        assert m is not None and m.mohs == 7.0  # unchanged: the stale update did NOT land
    finally:
        store.close()


def test_submit_reinsert_is_idempotent(store_factory, tmp_path) -> None:
    store = store_factory()
    log = DeltaLog(tmp_path / "log")
    store.attach(log)
    try:

        async def go() -> int:
            r1 = await _post(store, log, {"ops": [_op("Q1", name="Quartz")]})
            assert r1.status_code == 200, r1.text
            oid = r1.json()["keys"]["Q1"]
            # a lost-ack retry of the same insert (base=None on a now-present key)
            # → 409, NOT a duplicate (exactly-once effect)
            r2 = await _post(store, log, {"ops": [_op("Q1", name="Quartz")]})
            assert r2.status_code == 409, r2.text
            return oid

        oid = asyncio.run(go())
        assert store.last_tid == 1  # the retry committed nothing
        rows = list(store.query(Mineral))
        assert len(rows) == 1 and oid_of(rows[0]) == oid  # one Q1, same OID
    finally:
        store.close()


def test_submit_idempotent_insert_survives_restart(tmp_path) -> None:
    path = tmp_path / "coord"
    store = dc.Store.open(path)
    log = DeltaLog(tmp_path / "log")
    store.attach(log)
    try:
        assert asyncio.run(_post(store, log, {"ops": [_op("Q1", name="Quartz")]})).status_code == 200
        assert store.last_tid == 1
    finally:
        store.close()
    # reopen (the unique map rebuilds from records); the same insert must still 409
    store2 = dc.Store.open(path)
    log2 = DeltaLog(tmp_path / "log")
    store2.attach(log2)
    try:
        resp = asyncio.run(_post(store2, log2, {"ops": [_op("Q1", name="Quartz")]}))
        assert resp.status_code == 409, resp.text
        assert store2.last_tid == 1  # no double-apply across a restart
        assert len(list(store2.query(Mineral))) == 1
    finally:
        store2.close()
