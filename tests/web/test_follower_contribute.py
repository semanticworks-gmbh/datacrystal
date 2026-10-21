"""#153 — follower contribute: commit() fans buffered writes into the coordinator.

Two deterministic, in-process layers (the full coordinator+follower HTTP
round-trip — "contribute round-trips to a 2nd follower" — is the #156 conformance
capstone, which stands up a real-threaded server):

* the engine branch — ``commit()`` collects the buffered NEW/DIRTY entities,
  computes the OCC base, and hands them to the contribute hook (here a stub);
* the serializer — ``_contribute`` projects entities to the wire ops and
  translates a coordinator 409 into the typed local exception.

Both layers run over both ``store_factory`` backends (the ``store`` fixture).
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


class _ContributeStub:
    """Stand in for a follower's contribute hook: record the items, answer a fixed TID."""

    def __init__(self, tid: int) -> None:
        self.tid = tid
        self.sent: list[tuple[Any, str | None]] = []

    def __call__(self, items: list[tuple[Any, str | None]]) -> int:
        self.sent.extend(items)
        return self.tid


class _FakeResp:
    def __init__(self, status: int, data: object) -> None:
        self.status_code = status
        self.content = b"{}"
        self._data = data

    def json(self) -> object:
        return self._data


class _FakeClient:
    def __init__(self, resp: _FakeResp) -> None:
        self.resp = resp
        self.posted: tuple[str, dict[str, Any]] | None = None

    def post(self, path: str, json: dict[str, Any]) -> _FakeResp:
        self.posted = (path, json)
        return self.resp


def _committed_mineral(store: dc.Store, **fields: object) -> Mineral:
    """Commit a Mineral built from ``fields`` and return it as read back."""
    store.upsert(Mineral(**fields))  # type: ignore[arg-type]
    store.commit()
    qid = fields["qid"]
    mineral = store.get(Mineral, qid=qid)
    if mineral is None:
        raise LookupError(f"mineral {qid} was not committed")
    return mineral


def test_commit_contributes_new_entity_with_no_base(store: dc.Store) -> None:
    """Given a follower store whose contribute hook answers TID 7
    When a new mineral is upserted and committed
    Then the commit returns TID 7
    And exactly one item was sent
    And the item is the new mineral with base None
    And the new-entity buffer is cleared
    """
    stub = _ContributeStub(tid=7)
    store._contribute_fn = stub
    store.upsert(Mineral(qid="QN", name="New"))

    tid = store.commit()

    assert tid == 7
    assert len(stub.sent) == 1
    obj, base = stub.sent[0]
    assert obj.qid == "QN"
    assert base is None
    assert not store._new


def test_commit_contributes_dirty_entity_with_the_read_base(store: dc.Store) -> None:
    """Given a committed mineral read back and edited after the contribute hook is set
    When the follower commits the edit
    Then the commit returns the hook's TID 9
    And the item is the edited mineral
    And its base is the digest of the payload that was read
    """
    store.upsert(Mineral(qid="QD", name="Old", mohs=5.0))
    store.commit()
    base_expected = store._payload_digest(type_info(Mineral), "qid", "QD")
    stub = _ContributeStub(tid=9)
    store._contribute_fn = stub
    mineral = store.get(Mineral, qid="QD")
    assert mineral is not None
    mineral.mohs = 6.0

    tid = store.commit()

    obj, base = stub.sent[0]
    assert tid == 9
    assert obj.qid == "QD"
    assert base == base_expected


def test_contribute_returns_the_applied_tid(store: dc.Store) -> None:
    """Given a coordinator answering 200 with applied_tid 5
    When a quartz is contributed with base abc
    Then _contribute returns TID 5
    """
    quartz = _committed_mineral(store, qid="QS", name="Quartz", mohs=7.0)
    fake = _FakeClient(_FakeResp(200, {"applied_tid": 5, "keys": {"QS": 1}}))

    tid = _contribute([(quartz, "abc")], url="http://x", api_key=None, client=fake)

    assert tid == 5


def test_contribute_posts_one_serialized_op_to_submit(store: dc.Store) -> None:
    """Given a coordinator answering 200
    When a quartz is contributed with base abc
    Then exactly the /v1/submit path is posted
    And the op names the Mineral type and the qid key
    And the op carries the base abc
    And the op fields hold the quartz's qid and hardness
    """
    quartz = _committed_mineral(store, qid="QS", name="Quartz", mohs=7.0)
    fake = _FakeClient(_FakeResp(200, {"applied_tid": 5, "keys": {"QS": 1}}))

    _contribute([(quartz, "abc")], url="http://x", api_key=None, client=fake)

    assert fake.posted is not None
    path, body = fake.posted
    op = body["ops"][0]
    assert path == "/v1/submit"
    assert op["type"].endswith(":Mineral")
    assert op["key"] == "qid"
    assert op["base"] == "abc"
    assert op["fields"]["qid"] == "QS"
    assert op["fields"]["mohs"] == 7.0


def test_contribute_translates_409_conflict_to_conflict_error(store: dc.Store) -> None:
    """Given a coordinator answering 409 conflict with the message moved
    When a calcite is contributed with a stale base
    Then _contribute raises ConflictError
    And the error message carries moved
    """
    calcite = _committed_mineral(store, qid="QC", name="Calcite")
    conflict = _FakeClient(_FakeResp(409, {"detail": {"error": "conflict", "message": "moved"}}))

    with pytest.raises(ConflictError) as conflict_exc:
        _contribute([(calcite, "stale")], url="http://x", api_key=None, client=conflict)

    assert "moved" in str(conflict_exc.value)


def test_contribute_translates_409_schema_skew_to_schema_skew_error(store: dc.Store) -> None:
    """Given a coordinator answering 409 schema-skew with the message field
    When a calcite is contributed
    Then _contribute raises SchemaSkewError
    And the error message carries field
    """
    calcite = _committed_mineral(store, qid="QC", name="Calcite")
    skew = _FakeClient(_FakeResp(409, {"detail": {"error": "schema-skew", "message": "field"}}))

    with pytest.raises(SchemaSkewError) as skew_exc:
        _contribute([(calcite, None)], url="http://x", api_key=None, client=skew)

    assert "field" in str(skew_exc.value)


def test_commit_refuses_to_contribute_a_delete(store: dc.Store) -> None:
    """Given a committed mineral and a contribute hook on the follower store
    When the mineral is deleted and the delete is committed
    Then the commit raises NotImplementedError
    And nothing was fanned in
    """
    store.upsert(Mineral(qid="QX", name="X"))
    store.commit()
    stub = _ContributeStub(tid=1)
    store._contribute_fn = stub
    store.delete(Mineral, qid="QX")

    with pytest.raises(NotImplementedError):
        store.commit()

    assert not stub.sent


def test_contribute_conflict_carries_envelope_fields(store: dc.Store) -> None:
    """Given a coordinator answering 409 with the full conflict envelope
    When a quartz is contributed with base a-times-64
    Then ConflictError carries the key QE
    And it carries the expected base
    And it carries the actual base
    """
    quartz = _committed_mineral(store, qid="QE", name="Quartz")
    envelope = {
        "error": "conflict",
        "key": "QE",
        "expected_base": "a" * 64,
        "actual_base": "b" * 64,
        "message": "moved",
    }
    fake = _FakeClient(_FakeResp(409, {"detail": envelope}))

    with pytest.raises(ConflictError) as exc:
        _contribute([(quartz, "a" * 64)], url="http://x", api_key=None, client=fake)

    assert exc.value.key == "QE"
    assert exc.value.expected_base == "a" * 64
    assert exc.value.actual_base == "b" * 64


def test_contribute_refuses_entity_in_unsupported_container(store: dc.Store) -> None:
    """Given a mineral holding a committed locality inside its bare list field tags
    When it is contributed
    Then _contribute raises NotImplementedError
    And nothing was posted
    """
    store.store(Locality(qid="LB", name="Tsumeb"))
    store.commit()
    loc = store.get(Locality, qid="LB")
    mineral = Mineral(qid="QB", name="X", tags=[loc])
    fake = _FakeClient(_FakeResp(200, {"applied_tid": 1, "keys": {}}))

    with pytest.raises(NotImplementedError):
        _contribute([(mineral, None)], url="http://x", api_key=None, client=fake)

    assert fake.posted is None


def test_contribute_refuses_blob_field(store: dc.Store) -> None:
    """Given a specimen with a dc.Blob scan
    When it is contributed
    Then _contribute raises NotImplementedError
    And nothing was posted
    """
    spec = Specimen(qid="SP", scan=b"\x89PNG not-utf8 bytes")
    fake = _FakeClient(_FakeResp(200, {"applied_tid": 1, "keys": {}}))

    with pytest.raises(NotImplementedError):
        _contribute([(spec, None)], url="http://x", api_key=None, client=fake)

    assert fake.posted is None


def test_contribute_noop_returns_none_not_typeerror(store: dc.Store) -> None:
    """Given a coordinator answering 200 with applied_tid null, the idempotent re-send
    When a mineral is contributed
    Then _contribute returns None
    """
    mineral = _committed_mineral(store, qid="QZ", name="Z")
    fake = _FakeClient(_FakeResp(200, {"applied_tid": None, "keys": {"QZ": 1}}))

    tid = _contribute([(mineral, "base")], url="http://x", api_key=None, client=fake)

    assert tid is None


def test_contribute_translates_dangling_ref_faithfully(store: dc.Store) -> None:
    """Given a coordinator answering 409 dangling-ref with the message gone
    When a calcite is contributed
    Then _contribute raises DanglingRefError
    And the error message carries gone
    """
    calcite = _committed_mineral(store, qid="QC", name="Calcite")
    fake = _FakeClient(_FakeResp(409, {"detail": {"error": "dangling-ref", "message": "gone"}}))

    with pytest.raises(DanglingRefError) as exc:
        _contribute([(calcite, "x")], url="http://x", api_key=None, client=fake)

    assert "gone" in str(exc.value)


def test_contribute_refuses_new_to_new_reference(store: dc.Store) -> None:
    """Given a new mineral referencing a new, not yet committed locality
    When both are contributed
    Then _contribute raises NotImplementedError
    And nothing crossed the wire
    """
    loc = Locality(qid="LZ", name="Zomba")
    mineral = Mineral(qid="QM", name="X", type_locality=dc.Lazy.of(loc))
    store.store(loc)
    store.store(mineral)
    fake = _FakeClient(_FakeResp(200, {"applied_tid": 1, "keys": {}}))

    with pytest.raises(NotImplementedError):
        _contribute([(mineral, None), (loc, None)], url="http://x", api_key=None, client=fake)

    assert fake.posted is None
