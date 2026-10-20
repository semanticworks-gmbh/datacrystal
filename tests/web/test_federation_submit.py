"""POST /v1/submit — fan-in (#152) + the fail-closed guards (#154 schema-skew /
Unique-key, #155 OCC / idempotency).

The handler does owner-confined writes (store.submit -> upsert -> commit), so the
test runs the store on the loop thread: ``asyncio.run`` + ``httpx.AsyncClient``
over an ``ASGITransport`` means the handler runs on the same thread that owns the
store, so ``store.submit`` runs inline — as production under uvicorn (no
pytest-asyncio, matching tests/unit/test_async.py). Over both ``store_factory`` backends.
"""

from __future__ import annotations

import functools
import threading
from pathlib import Path
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
_STALE_BASE = "0" * 64


async def _post(store: dc.Store, log: DeltaLog, body: dict[str, Any]) -> httpx.Response:
    app = FastAPI()
    app.include_router(federation_router(store, log))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://coord") as client:
        return await client.post("/v1/submit", json=body)


def _post_submit(store: dc.Store, log: DeltaLog, body: dict[str, Any]) -> httpx.Response:
    """POST ``body`` to /v1/submit on the thread that calls it (the store's owner in a test)."""
    return asyncio.run(_post(store, log, body))


def _store_response(captured: dict[str, httpx.Response], post: functools.partial[httpx.Response]) -> None:
    """Thread target: run ``post`` on this thread and keep its response."""
    captured["resp"] = post()


def _op(qid: str, base: str | None = None, **fields: object) -> dict[str, Any]:
    return {"type": _MINERAL, "key": "qid", "fields": {"qid": qid, **fields}, "base": base}


def _digest(store: dc.Store, qid: str) -> str | None:
    """Return the current OCC base for a Mineral, what a follower carries on update."""
    return store._payload_digest(type_info(Mineral), "qid", qid)


@pytest.fixture
def log(store: dc.Store, tmp_path: Path) -> DeltaLog:
    """Create a delta log and attach it to the store."""
    delta_log = DeltaLog(tmp_path / "log")
    store.attach(delta_log)
    return delta_log


def test_submit_creates_a_mineral_and_reports_its_key(store: dc.Store, log: DeltaLog) -> None:
    """Given an empty coordinator
    When a client submits a new quartz without a base
    Then the response is 200
    And it reports a positive applied transaction id
    And its keys name exactly Q1
    """
    resp = _post_submit(store, log, {"ops": [_op("Q1", name="Quartz", mohs=7.0)]})

    created = resp.json()
    assert resp.status_code == 200, resp.text
    assert created["applied_tid"] > 0
    assert set(created["keys"]) == {"Q1"}


def test_submit_update_with_the_current_base_keeps_the_oid(store: dc.Store, log: DeltaLog) -> None:
    """Given a submitted quartz and its current OCC base
    When a client submits an update of Q1 carrying that base
    Then the response is 200
    And no second OID is minted
    And the stored quartz has the new hardness under the same OID
    """
    created = _post_submit(store, log, {"ops": [_op("Q1", name="Quartz", mohs=7.0)]})
    oid = created.json()["keys"]["Q1"]
    base = _digest(store, "Q1")
    assert base is not None

    resp = _post_submit(store, log, {"ops": [_op("Q1", base=base, name="Quartz", mohs=7.5)]})

    quartz = store.get(Mineral, qid="Q1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["keys"]["Q1"] == oid
    assert quartz is not None
    assert quartz.mohs == 7.5
    assert oid_of(quartz) == oid


def test_submit_batch_with_an_unknown_type_is_refused_whole(store: dc.Store, log: DeltaLog) -> None:
    """Given a batch of a valid calcite and an op of an unknown type
    When a client submits it
    Then the response is 422
    And no transaction was committed
    And the calcite is absent
    """
    ghost = {"type": "nope.Module:Ghost", "key": "qid", "fields": {"qid": "X"}, "base": None}

    resp = _post_submit(store, log, {"ops": [_op("Q2", name="Calcite"), ghost]})

    assert resp.status_code == 422, resp.text
    assert store.last_tid == 0
    assert store.get(Mineral, qid="Q2") is None


def test_submit_mid_batch_failure_leaves_nothing_behind(store: dc.Store, log: DeltaLog) -> None:
    """Given a committed quartz and a batch of a new calcite then a stale-base update of Q1
    When a client submits the batch
    Then the response is 409
    And the calcite is absent
    And the watermark did not move
    """
    first = _post_submit(store, log, {"ops": [_op("Q1", name="Quartz", mohs=7.0)]})
    assert first.status_code == 200
    sealed = store.last_tid
    batch = {
        "ops": [
            _op("Q2", name="Calcite", mohs=3.0),
            _op("Q1", base=_STALE_BASE, name="Quartz", mohs=9.9),
        ]
    }

    rejected = _post_submit(store, log, batch)

    assert rejected.status_code == 409
    assert store.get(Mineral, qid="Q2") is None
    assert store.last_tid == sealed


def test_submit_after_a_rejected_batch_does_not_carry_the_leaked_write(
    store: dc.Store, log: DeltaLog
) -> None:
    """Given a batch rejected mid-fan-in after its first op was buffered
    When a client submits a valid topaz
    Then the response is 200
    And the rejected calcite is still absent
    And the topaz is stored
    And the store holds only the quartz and the topaz
    """
    _post_submit(store, log, {"ops": [_op("Q1", name="Quartz", mohs=7.0)]})
    batch = {
        "ops": [
            _op("Q2", name="Calcite", mohs=3.0),
            _op("Q1", base=_STALE_BASE, name="Quartz", mohs=9.9),
        ]
    }
    _post_submit(store, log, batch)

    resp = _post_submit(store, log, {"ops": [_op("Q3", name="Topaz")]})

    assert resp.status_code == 200
    assert store.get(Mineral, qid="Q2") is None
    assert store.get(Mineral, qid="Q3") is not None
    assert {m.qid for m in store.query(Mineral)} == {"Q1", "Q3"}


def test_submit_ops_that_is_not_a_list_is_refused(store: dc.Store, log: DeltaLog) -> None:
    """Given an envelope whose ops is a string
    When a client submits it
    Then the response is 422
    And nothing was written
    """
    resp = _post_submit(store, log, {"ops": "notalist"})

    assert resp.status_code == 422, resp.text
    assert store.last_tid == 0


def test_submit_op_that_is_not_an_object_is_refused(store: dc.Store, log: DeltaLog) -> None:
    """Given an envelope whose only op is the number 5
    When a client submits it
    Then the response is 422
    And nothing was written
    """
    resp = _post_submit(store, log, {"ops": [5]})

    assert resp.status_code == 422, resp.text
    assert store.last_tid == 0


def test_submit_op_missing_key_and_fields_is_refused(store: dc.Store, log: DeltaLog) -> None:
    """Given an envelope whose only op names a type but no key or fields
    When a client submits it
    Then the response is 422
    And nothing was written
    """
    resp = _post_submit(store, log, {"ops": [{"type": _MINERAL}]})

    assert resp.status_code == 422, resp.text
    assert store.last_tid == 0


def test_submit_non_dict_fields_is_422_not_409(store: dc.Store, log: DeltaLog) -> None:
    """Given an op whose fields is the string NOTADICT
    When a client submits it
    Then the response is 422, not the schema-skew 409
    And nothing was written
    """
    non_dict = {"type": _MINERAL, "key": "qid", "fields": "NOTADICT", "base": None}

    resp = _post_submit(store, log, {"ops": [non_dict]})

    assert resp.status_code == 422, resp.text
    assert store.last_tid == 0


def test_submit_non_string_key_is_422(store: dc.Store, log: DeltaLog) -> None:
    """Given an op whose key is the number 5
    When a client submits it
    Then the response is 422
    And nothing was written
    """
    non_str_key = {"type": _MINERAL, "key": 5, "fields": {"qid": "Q"}, "base": None}

    resp = _post_submit(store, log, {"ops": [non_str_key]})

    assert resp.status_code == 422, resp.text
    assert store.last_tid == 0


def test_submit_missing_required_field_is_422_not_500(store: dc.Store, log: DeltaLog) -> None:
    """Given an op that omits the required Mineral.name
    When a client submits it
    Then the response is 422, not a 500 trace
    And nothing was written
    """
    missing = {"type": _MINERAL, "key": "qid", "fields": {"qid": "QV"}, "base": None}

    resp = _post_submit(store, log, {"ops": [missing]})

    assert resp.status_code == 422, resp.text
    assert store.last_tid == 0


def test_submit_wrong_typed_field_is_422_not_500(store: dc.Store, log: DeltaLog) -> None:
    """Given an op whose hardness cannot coerce to a float
    When a client submits it
    Then the response is 422, not a 500 trace
    And nothing was written
    """
    wrong = _op("QW", name="x", mohs="not-a-number")

    resp = _post_submit(store, log, {"ops": [wrong]})

    assert resp.status_code == 422, resp.text
    assert store.last_tid == 0


def test_submit_rejects_schema_skew(store: dc.Store, log: DeltaLog) -> None:
    """Given an op carrying a field the coordinator's Mineral does not have
    When a client submits it
    Then the response is 409
    And the error detail is schema-skew
    And nothing was written
    And the skewed mineral is absent
    """
    resp = _post_submit(store, log, {"ops": [_op("QS", name="x", bogus_field=1)]})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "schema-skew"
    assert store.last_tid == 0
    assert store.get(Mineral, qid="QS") is None


def test_submit_rejects_non_unique_key(store: dc.Store, log: DeltaLog) -> None:
    """Given an op keyed on the name field, which is not a dc.Unique field
    When a client submits it
    Then the response is 422
    And nothing was written
    """
    bad = {"type": _MINERAL, "key": "name", "fields": {"qid": "QN", "name": "x"}, "base": None}

    resp = _post_submit(store, log, {"ops": [bad]})

    assert resp.status_code == 422, resp.text
    assert store.last_tid == 0


def test_submit_occ_rejects_stale_base(store: dc.Store, log: DeltaLog) -> None:
    """Given a committed quartz at hardness 7.0
    When a client submits an update carrying a stale base
    Then the response is 409
    And the error detail is conflict
    And the quartz keeps hardness 7.0
    """
    _post_submit(store, log, {"ops": [_op("Q1", name="Quartz", mohs=7.0)]})

    resp = _post_submit(store, log, {"ops": [_op("Q1", base=_STALE_BASE, name="Quartz", mohs=9.9)]})

    quartz = store.get(Mineral, qid="Q1")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "conflict"
    assert quartz is not None
    assert quartz.mohs == 7.0


def test_submit_reinsert_is_refused_and_commits_nothing(store: dc.Store, log: DeltaLog) -> None:
    """Given a quartz inserted once
    When a client retries the same insert with base None, as after a lost ack
    Then the response is 409
    And the retry committed nothing
    And exactly one quartz remains, under the original OID
    """
    first = _post_submit(store, log, {"ops": [_op("Q1", name="Quartz")]})
    oid = first.json()["keys"]["Q1"]

    retry = _post_submit(store, log, {"ops": [_op("Q1", name="Quartz")]})

    assert retry.status_code == 409, retry.text
    assert store.last_tid == 1
    assert [oid_of(m) for m in store.query(Mineral)] == [oid]


def test_submit_conflict_envelope_carries_bases(store: dc.Store, log: DeltaLog) -> None:
    """Given a committed quartz
    When a client submits an update carrying a stale base
    Then the 409 detail names the error conflict and the key Q1
    And it echoes the base the client carried
    And it reports a different actual base
    """
    _post_submit(store, log, {"ops": [_op("Q1", name="Quartz", mohs=7.0)]})

    resp = _post_submit(store, log, {"ops": [_op("Q1", base=_STALE_BASE, name="Quartz", mohs=9.9)]})

    detail = resp.json()["detail"]
    assert resp.status_code == 409, resp.text
    assert detail["error"] == "conflict"
    assert detail["key"] == "Q1"
    assert detail["expected_base"] == _STALE_BASE
    assert detail["actual_base"] is not None
    assert detail["actual_base"] != _STALE_BASE


def test_submit_off_owner_thread_fails_loud_not_hang(store: dc.Store, log: DeltaLog) -> None:
    """Given a store owned by this thread and an event loop on a worker thread
    When a client submits a quartz through the worker's loop
    Then the call returns instead of hanging
    And the response is 500
    And nothing was written
    """
    captured: dict[str, httpx.Response] = {}
    post = functools.partial(_post_submit, store, log, {"ops": [_op("Q1", name="Quartz")]})
    thread = threading.Thread(target=_store_response, args=(captured, post), daemon=True)

    thread.start()
    thread.join(timeout=15)

    assert not thread.is_alive(), "submit hung: owner-thread guard missing (deadlock)"
    assert captured["resp"].status_code == 500
    assert store.last_tid == 0


def test_submit_idempotent_insert_survives_restart(tmp_path: Path) -> None:
    """Given a quartz inserted into a store that was then closed
    When the store is reopened and the same insert is resubmitted
    Then the response is 409
    And the store did not apply it twice
    And exactly one quartz is stored
    """
    path = tmp_path / "coord"
    with dc.Store.open(path) as first_store:
        first_log = DeltaLog(tmp_path / "log")
        first_store.attach(first_log)
        first = _post_submit(first_store, first_log, {"ops": [_op("Q1", name="Quartz")]})
        assert first.status_code == 200
        assert first_store.last_tid == 1

    with dc.Store.open(path) as reopened:
        reopened_log = DeltaLog(tmp_path / "log")
        reopened.attach(reopened_log)

        resp = _post_submit(reopened, reopened_log, {"ops": [_op("Q1", name="Quartz")]})

        assert resp.status_code == 409, resp.text
        assert reopened.last_tid == 1
        assert len(list(reopened.query(Mineral))) == 1
