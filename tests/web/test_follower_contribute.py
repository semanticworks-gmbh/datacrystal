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

from typing import Annotated, Any

import pytest

pytest.importorskip("pydantic", reason="contribute serializes via to_pydantic")

import datacrystal as dc
from datacrystal import ConflictError, DanglingRefError, SchemaSkewError
from datacrystal._entity import type_info
from datacrystal._follower import _contribute
from tests.conftest import Locality, Mineral


@dc.entity
class Specimen:
    """A mineral-cabinet specimen with an out-of-line scanned label (dc.Blob).

    Used only by the blob-rejection contribute test; its blob field has no
    create-face wire shape, so a follower must refuse to contribute it (v0).
    """

    qid: Annotated[str, dc.Unique]
    scan: Annotated[bytes, dc.Blob] = b""


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


def test_commit_refuses_to_contribute_a_delete(store_factory) -> None:
    store = store_factory()
    sent: list[tuple[Any, str | None]] = []

    def stub(items: list[tuple[Any, str | None]]) -> int:
        sent.extend(items)
        return 1

    try:
        store.upsert(Mineral(qid="QX", name="X"))
        store.commit()  # local commit (no contribute hook yet)
        store._contribute_fn = stub
        store.delete(Mineral, qid="QX")  # a buffered delete
        with pytest.raises(NotImplementedError):
            store.commit()
        assert not sent  # fail loud: the delete was NOT silently fanned in (or dropped)
    finally:
        store.close()


def test_contribute_conflict_carries_envelope_fields(store) -> None:
    """The follower-side ``ConflictError`` carries the LOCKED conflict envelope
    (key/expected_base/actual_base) the coordinator sent, so a programmatic caller
    can read ``exc.actual_base`` to drive its re-read (#155 re-verification fix —
    the decode previously dropped the fields, keeping only the message).
    """
    store.upsert(Mineral(qid="QE", name="Quartz"))
    store.commit()
    m = store.get(Mineral, qid="QE")
    envelope = {
        "error": "conflict",
        "key": "QE",
        "expected_base": "a" * 64,
        "actual_base": "b" * 64,
        "message": "moved",
    }
    fake = _FakeClient(_FakeResp(409, {"detail": envelope}))
    with pytest.raises(ConflictError) as exc:
        _contribute([(m, "a" * 64)], url="http://x", api_key=None, client=fake)
    assert exc.value.key == "QE"
    assert exc.value.expected_base == "a" * 64
    assert exc.value.actual_base == "b" * 64


def test_contribute_refuses_entity_in_unsupported_container(store) -> None:
    """An @entity nested in a bare ``list`` field would land as a bare int on the
    coordinator (the OID-int boundary cannot rebind it) — fail loud before the
    wire, never silently corrupt (#153 peer-review fix, FEDERATION-WIRE-v1 §5).
    """
    store.store(Locality(qid="LB", name="Tsumeb"))
    store.commit()
    loc = store.get(Locality, qid="LB")
    # a committed Locality placed inside Mineral.tags (a bare ``list`` field)
    mineral = Mineral(qid="QB", name="X", tags=[loc])
    fake = _FakeClient(_FakeResp(200, {"applied_tid": 1, "keys": {}}))
    with pytest.raises(NotImplementedError):
        _contribute([(mineral, None)], url="http://x", api_key=None, client=fake)
    assert fake.posted is None  # the unfederatable container ref never crossed


def test_contribute_refuses_blob_field(store) -> None:
    """A ``dc.Blob`` field has no create-face wire shape — a follower must refuse
    to contribute a blob-bearing entity, loudly, never silently inline its bytes
    (#153 peer-review fix, FEDERATION-WIRE-v1 §5).
    """
    spec = Specimen(qid="SP", scan=b"\x89PNG not-utf8 bytes")
    fake = _FakeClient(_FakeResp(200, {"applied_tid": 1, "keys": {}}))
    with pytest.raises(NotImplementedError):
        _contribute([(spec, None)], url="http://x", api_key=None, client=fake)
    assert fake.posted is None


def test_contribute_noop_returns_none_not_typeerror(store) -> None:
    """A coordinator ``applied_tid: null`` (the documented no-op / idempotent
    re-send response) returns ``None`` cleanly, never ``int(None)`` → TypeError
    (#155 peer-review fix).
    """
    store.upsert(Mineral(qid="QZ", name="Z"))
    store.commit()
    m = store.get(Mineral, qid="QZ")
    fake = _FakeClient(_FakeResp(200, {"applied_tid": None, "keys": {"QZ": 1}}))
    assert _contribute([(m, "base")], url="http://x", api_key=None, client=fake) is None


def test_contribute_translates_dangling_ref_faithfully(store) -> None:
    """A 409 ``dangling-ref`` maps to ``DanglingRefError``, not folded into
    ``ConflictError`` ("re-read and retry" is wrong for a dangle) (#153/#155 fix).
    """
    store.upsert(Mineral(qid="QC", name="Calcite"))
    store.commit()
    m = store.get(Mineral, qid="QC")
    fake = _FakeClient(_FakeResp(409, {"detail": {"error": "dangling-ref", "message": "gone"}}))
    with pytest.raises(DanglingRefError) as exc:
        _contribute([(m, "x")], url="http://x", api_key=None, client=fake)
    assert "gone" in str(exc.value)


def test_contribute_refuses_new_to_new_reference(store) -> None:
    loc = Locality(qid="LZ", name="Zomba")
    mineral = Mineral(qid="QM", name="X", type_locality=dc.Lazy.of(loc))
    store.store(loc)
    store.store(mineral)  # both NEW; mineral references the not-yet-committed loc
    fake = _FakeClient(_FakeResp(200, {"applied_tid": 1, "keys": {}}))
    with pytest.raises(NotImplementedError):
        _contribute([(mineral, None), (loc, None)], url="http://x", api_key=None, client=fake)
    assert fake.posted is None  # the unsafe follower-local OID never crossed the wire
