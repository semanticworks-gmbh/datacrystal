"""#152 — POST /v1/submit: contribute fan-in to the single writer.

The handler does owner-confined writes (store.submit -> upsert -> commit), so the
test runs the store on the loop thread: ``asyncio.run`` + ``httpx.AsyncClient``
over an ``ASGITransport`` means the handler runs on the same thread that owns the
store, so ``store.submit`` runs inline — exactly as production under uvicorn
(no pytest-asyncio, matching tests/unit/test_async.py). Over both backends.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("httpx", reason="pip install httpx (ASGITransport)")

import asyncio

import httpx
from fastapi import FastAPI

from datacrystal._entity import oid_of
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


def _op(qid: str, **fields: Any) -> dict[str, Any]:
    return {"type": _MINERAL, "key": "qid", "fields": {"qid": qid, **fields}, "base": None}


def test_submit_creates_then_merges_to_same_oid(store_factory, tmp_path) -> None:
    store = store_factory()
    log = DeltaLog(tmp_path / "log")
    store.attach(log)
    try:

        async def go() -> int:
            r1 = await _post(store, log, {"idem": "a", "ops": [_op("Q1", name="Quartz", mohs=7.0)]})
            assert r1.status_code == 200, r1.text
            d1 = r1.json()
            assert d1["applied_tid"] is not None and d1["applied_tid"] > 0
            assert set(d1["keys"]) == {"Q1"}
            oid = d1["keys"]["Q1"]
            assert isinstance(oid, int)
            # same natural key, changed field -> merges into the survivor, SAME OID
            r2 = await _post(store, log, {"idem": "b", "ops": [_op("Q1", name="Quartz", mohs=7.5)]})
            assert r2.status_code == 200, r2.text
            assert r2.json()["keys"]["Q1"] == oid  # no second OID minted
            return oid

        oid = asyncio.run(go())
        # back on the owner thread: the coordinator holds the merged survivor
        m = store.get(Mineral, qid="Q1")
        assert m is not None and m.mohs == 7.5 and m.name == "Quartz"
        assert oid_of(m) == oid
    finally:
        store.close()


def test_submit_batch_is_all_or_nothing(store_factory, tmp_path) -> None:
    store = store_factory()
    log = DeltaLog(tmp_path / "log")
    store.attach(log)
    try:

        async def go() -> None:
            # a batch with one good op and one of an unknown type -> the whole
            # batch is rejected up front; the good op must NOT land
            bad = {"type": "nope.Module:Ghost", "key": "qid", "fields": {"qid": "X"}, "base": None}
            resp = await _post(store, log, {"idem": "c", "ops": [_op("Q2", name="Calcite"), bad]})
            assert resp.status_code == 422, resp.text

        asyncio.run(go())
        # nothing landed: the valid op was not committed (one batch = one commit)
        assert store.last_tid == 0
        assert store.get(Mineral, qid="Q2") is None
    finally:
        store.close()
