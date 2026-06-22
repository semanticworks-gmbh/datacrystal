"""#153 — follower contribute: commit() fans buffered writes into the coordinator.

Two deterministic, in-process layers (the full coordinator+follower HTTP
round-trip — "contribute round-trips to a 2nd follower" — is the #156 conformance
capstone, which stands up a real-threaded server):

* the engine branch — ``commit()`` collects the buffered NEW/DIRTY entities,
  computes the OCC base, and hands them to the contribute hook (here a stub);
* the serializer — ``_contribute`` projects entities to the wire ops and
  translates a coordinator 409 into the typed local exception.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pydantic", reason="contribute serializes via to_pydantic")

from datacrystal import ConflictError, SchemaSkewError
from datacrystal._entity import type_info
from datacrystal._follower import _contribute
from tests.conftest import Mineral


def test_commit_contributes_new_entity(store_factory) -> None:
    store = store_factory()
    sent: list[tuple[Any, str | None]] = []

    def stub(items: list[tuple[Any, str | None]]) -> int:
        sent.extend(items)
        return 7

    try:
        store._contribute_fn = stub  # a follower's commit() fans in via this hook
        store.upsert(Mineral(qid="QN", name="New"))
        tid = store.commit()
        assert tid == 7
        assert len(sent) == 1
        obj, base = sent[0]
        assert obj.qid == "QN" and base is None  # NEW → base None
        assert not store._new  # buffers cleared on success
    finally:
        store.close()


def test_commit_contributes_dirty_entity_with_read_base(store_factory) -> None:
    store = store_factory()
    sent: list[tuple[Any, str | None]] = []

    def stub(items: list[tuple[Any, str | None]]) -> int:
        sent.extend(items)
        return 9

    try:
        store.upsert(Mineral(qid="QD", name="Old", mohs=5.0))
        store.commit()  # a normal local commit (no contribute hook yet)
        base_expected = store._payload_digest(type_info(Mineral), "qid", "QD")
        store._contribute_fn = stub
        m = store.get(Mineral, qid="QD")
        assert m is not None
        m.mohs = 6.0  # an edit → DIRTY
        assert store.commit() == 9
        obj, base = sent[0]
        assert obj.qid == "QD" and base == base_expected  # DIRTY → the read hash
    finally:
        store.close()


class _FakeResp:
    def __init__(self, status: int, data: Any) -> None:
        self.status_code = status
        self.content = b"{}"
        self._data = data

    def json(self) -> Any:
        return self._data


class _FakeClient:
    def __init__(self, resp: _FakeResp) -> None:
        self.resp = resp
        self.posted: tuple[str, Any] | None = None

    def post(self, path: str, json: Any) -> _FakeResp:
        self.posted = (path, json)
        return self.resp


def test_contribute_serializes_ops_and_returns_tid(store) -> None:
    store.upsert(Mineral(qid="QS", name="Quartz", mohs=7.0))
    store.commit()
    m = store.get(Mineral, qid="QS")
    fake = _FakeClient(_FakeResp(200, {"applied_tid": 5, "keys": {"QS": 1}}))
    tid = _contribute([(m, "abc")], url="http://x", api_key=None, client=fake)
    assert tid == 5
    assert fake.posted is not None
    path, body = fake.posted
    assert path == "/v1/submit"
    op = body["ops"][0]
    assert op["type"].endswith(":Mineral") and op["key"] == "qid" and op["base"] == "abc"
    assert op["fields"]["qid"] == "QS" and op["fields"]["mohs"] == 7.0


def test_contribute_translates_409_to_typed_exceptions(store) -> None:
    store.upsert(Mineral(qid="QC", name="Calcite"))
    store.commit()
    m = store.get(Mineral, qid="QC")
    conflict = _FakeClient(_FakeResp(409, {"detail": {"error": "conflict", "message": "moved"}}))
    with pytest.raises(ConflictError) as conflict_exc:
        _contribute([(m, "stale")], url="http://x", api_key=None, client=conflict)
    assert "moved" in str(conflict_exc.value)  # the 409 body's message rides through
    skew = _FakeClient(_FakeResp(409, {"detail": {"error": "schema-skew", "message": "field"}}))
    with pytest.raises(SchemaSkewError) as skew_exc:
        _contribute([(m, None)], url="http://x", api_key=None, client=skew)
    assert "field" in str(skew_exc.value)
