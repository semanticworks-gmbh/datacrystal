"""#153 — follower contribute: commit() fans buffered writes into the coordinator.

A real follower opened over a fake coordinator (``tests/web/_fake_coordinator.py``) that serves
the seed deltas and records every ``POST /v1/submit``, so the whole path is the public one:

* the engine branch — ``commit()`` collects the buffered new and dirty entities,
  computes the OCC base and sends them;
* the serializer — the wire ops the follower builds, and the typed local exception it raises
  for a coordinator 409 or a v0 cut.

The follower is placed in memory and on sqlite.
The full coordinator+follower HTTP round-trip is the #156 conformance capstone.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import pytest

pytest.importorskip("pydantic", reason="contribute serializes via to_pydantic")

import datacrystal as dc
from datacrystal import ConflictError, DanglingRefError, SchemaSkewError
from datacrystal.deltalog import DeltaLog
from tests.conftest import Locality, Mineral
from tests.web._fake_coordinator import FakeCoordinator, occ_base_of


@dc.entity
class Specimen:
    """A mineral-cabinet specimen with an out-of-line scanned label (dc.Blob).

    Used only by the blob-rejection contribute test; its blob field has no
    create-face wire shape, so a follower must refuse to contribute it (v0).
    """

    qid: Annotated[str, dc.Unique]
    scan: Annotated[bytes, dc.Blob] = b""


@pytest.fixture(params=["memory", "sqlite"])
def follower_path(request: pytest.FixtureRequest, tmp_path: Path) -> Path | None:
    """Place the follower in memory, or sqlite-backed at a path: both must contribute alike."""
    return None if request.param == "memory" else tmp_path / "follower"


@pytest.fixture
def coordinator(tmp_path: Path) -> Iterator[tuple[dc.Store, DeltaLog]]:
    """Provide a coordinator with a delta log, closed at teardown."""
    coord = dc.Store.open(tmp_path / "coord", durability="never")
    log = DeltaLog(tmp_path / "coordlog")
    coord.attach(log)
    yield coord, log
    coord.close()


def _conflict(message: str = "moved") -> dict[str, object]:
    """Build the 409 body of a coordinator that refuses a base."""
    return {"detail": {"error": "conflict", "message": message}}


def test_commit_contributes_new_entity_with_no_base(follower_path: Path | None) -> None:
    """Given a follower whose coordinator answers applied TID 7
    When a new mineral is upserted and committed
    Then the commit returns TID 7
    And exactly one op was sent
    And the op is the new mineral with base None
    And a second commit sends nothing more
    """
    coordinator = FakeCoordinator(submit_body={"applied_tid": 7, "keys": {"QN": 1}})
    with dc.open_follower("http://coord", client=coordinator, path=follower_path) as follower:
        follower.upsert(Mineral(qid="QN", name="New"))

        tid = follower.commit()

        assert tid == 7
        assert len(coordinator.posts) == 1
        op = coordinator.posts[0]["ops"][0]
        assert op["fields"]["qid"] == "QN"
        assert op["base"] is None
        assert follower.commit() is None
        assert len(coordinator.posts) == 1


def test_commit_contributes_dirty_entity_with_the_read_base(
    coordinator: tuple[dc.Store, DeltaLog], follower_path: Path | None
) -> None:
    """Given a follower that bootstrapped a committed mineral, read back and edited
    When the follower commits the edit and the coordinator answers applied TID 9
    Then the commit returns TID 9
    And the op is the edited mineral
    And its base is the digest of the payload the coordinator served
    """
    coord, log = coordinator
    oid = coord.store(Mineral(qid="QD", name="Old", mohs=5.0))
    coord.commit()
    seeded = list(log.replay(after_tid=0))
    served = FakeCoordinator(seeded, submit_body={"applied_tid": 9, "keys": {"QD": 1}})
    base_expected = occ_base_of(seeded, oid)
    with dc.open_follower("http://coord", client=served, path=follower_path) as follower:
        mineral = follower.get(Mineral, qid="QD")
        assert mineral is not None
        mineral.mohs = 6.0

        tid = follower.commit()

        op = served.posts[0]["ops"][0]
        assert tid == 9
        assert op["fields"]["qid"] == "QD"
        assert op["base"] == base_expected


def test_contribute_returns_the_applied_tid(follower_path: Path | None) -> None:
    """Given a coordinator answering 200 with applied_tid 5
    When a quartz is upserted and committed
    Then the commit returns TID 5
    """
    coordinator = FakeCoordinator(submit_body={"applied_tid": 5, "keys": {"QS": 1}})
    with dc.open_follower("http://coord", client=coordinator, path=follower_path) as follower:
        follower.upsert(Mineral(qid="QS", name="Quartz", mohs=7.0))

        tid = follower.commit()

        assert tid == 5


def test_contribute_posts_one_serialized_op_to_submit(follower_path: Path | None) -> None:
    """Given a coordinator answering 200
    When a quartz is upserted and committed
    Then one body is posted
    And the op names the Mineral type and the qid key
    And the op fields hold the quartz's qid and hardness
    """
    coordinator = FakeCoordinator(submit_body={"applied_tid": 5, "keys": {"QS": 1}})
    with dc.open_follower("http://coord", client=coordinator, path=follower_path) as follower:
        follower.upsert(Mineral(qid="QS", name="Quartz", mohs=7.0))

        follower.commit()

        assert len(coordinator.posts) == 1
        op = coordinator.posts[0]["ops"][0]
        assert op["type"].endswith(":Mineral")
        assert op["key"] == "qid"
        assert op["fields"]["qid"] == "QS"
        assert op["fields"]["mohs"] == 7.0


def test_contribute_translates_409_conflict_to_conflict_error(follower_path: Path | None) -> None:
    """Given a coordinator answering 409 conflict with the message moved
    When a calcite is upserted and committed
    Then the commit raises ConflictError
    And the error message carries moved
    """
    coordinator = FakeCoordinator(submit_status=409, submit_body=_conflict("moved"))
    with dc.open_follower("http://coord", client=coordinator, path=follower_path) as follower:
        follower.upsert(Mineral(qid="QC", name="Calcite"))

        with pytest.raises(ConflictError) as conflict_exc:
            follower.commit()

        assert "moved" in str(conflict_exc.value)


def test_contribute_translates_409_schema_skew_to_schema_skew_error(
    follower_path: Path | None,
) -> None:
    """Given a coordinator answering 409 schema-skew with the message field
    When a calcite is upserted and committed
    Then the commit raises SchemaSkewError
    And the error message carries field
    """
    skew = {"detail": {"error": "schema-skew", "message": "field"}}
    coordinator = FakeCoordinator(submit_status=409, submit_body=skew)
    with dc.open_follower("http://coord", client=coordinator, path=follower_path) as follower:
        follower.upsert(Mineral(qid="QC", name="Calcite"))

        with pytest.raises(SchemaSkewError) as skew_exc:
            follower.commit()

        assert "field" in str(skew_exc.value)


def test_commit_refuses_to_contribute_a_delete(
    coordinator: tuple[dc.Store, DeltaLog], follower_path: Path | None
) -> None:
    """Given a follower that bootstrapped a committed mineral
    When the mineral is deleted and the delete is committed
    Then the commit raises NotImplementedError
    And nothing was fanned in
    """
    coord, log = coordinator
    coord.upsert(Mineral(qid="QX", name="X"))
    coord.commit()
    served = FakeCoordinator(log.replay(after_tid=0))
    with dc.open_follower("http://coord", client=served, path=follower_path) as follower:
        follower.delete(Mineral, qid="QX")

        with pytest.raises(NotImplementedError):
            follower.commit()

        assert served.posts == []


def test_contribute_conflict_carries_envelope_fields(follower_path: Path | None) -> None:
    """Given a coordinator answering 409 with the full conflict envelope
    When a quartz is upserted and committed
    Then ConflictError carries the key QE
    And it carries the expected base
    And it carries the actual base
    """
    envelope = {
        "error": "conflict",
        "key": "QE",
        "expected_base": "a" * 64,
        "actual_base": "b" * 64,
        "message": "moved",
    }
    coordinator = FakeCoordinator(submit_status=409, submit_body={"detail": envelope})
    with dc.open_follower("http://coord", client=coordinator, path=follower_path) as follower:
        follower.upsert(Mineral(qid="QE", name="Quartz"))

        with pytest.raises(ConflictError) as exc:
            follower.commit()

        assert exc.value.key == "QE"
        assert exc.value.expected_base == "a" * 64
        assert exc.value.actual_base == "b" * 64


def test_contribute_refuses_entity_in_unsupported_container(
    coordinator: tuple[dc.Store, DeltaLog], follower_path: Path | None
) -> None:
    """Given a follower holding a committed locality and a mineral with it inside its tags list
    When the mineral is upserted and committed
    Then the commit raises NotImplementedError
    And nothing was posted
    """
    coord, log = coordinator
    coord.store(Locality(qid="LB", name="Tsumeb"))
    coord.commit()
    served = FakeCoordinator(log.replay(after_tid=0))
    with dc.open_follower("http://coord", client=served, path=follower_path) as follower:
        loc = follower.get(Locality, qid="LB")
        follower.upsert(Mineral(qid="QB", name="X", tags=[loc]))

        with pytest.raises(NotImplementedError):
            follower.commit()

        assert served.posts == []


def test_contribute_refuses_blob_field(follower_path: Path | None) -> None:
    """Given a specimen with a dc.Blob scan
    When it is upserted and committed
    Then the commit raises NotImplementedError
    And nothing was posted
    """
    served = FakeCoordinator()
    with dc.open_follower("http://coord", client=served, path=follower_path) as follower:
        follower.upsert(Specimen(qid="SP", scan=b"\x89PNG not-utf8 bytes"))

        with pytest.raises(NotImplementedError):
            follower.commit()

        assert served.posts == []


def test_contribute_noop_returns_none_not_typeerror(follower_path: Path | None) -> None:
    """Given a coordinator answering 200 with applied_tid null, the idempotent re-send
    When a mineral is upserted and committed
    Then the commit returns None
    """
    coordinator = FakeCoordinator(submit_body={"applied_tid": None, "keys": {"QZ": 1}})
    with dc.open_follower("http://coord", client=coordinator, path=follower_path) as follower:
        follower.upsert(Mineral(qid="QZ", name="Z"))

        tid = follower.commit()

        assert tid is None


def test_contribute_translates_dangling_ref_faithfully(follower_path: Path | None) -> None:
    """Given a coordinator answering 409 dangling-ref with the message gone
    When a calcite is upserted and committed
    Then the commit raises DanglingRefError
    And the error message carries gone
    """
    gone = {"detail": {"error": "dangling-ref", "message": "gone"}}
    coordinator = FakeCoordinator(submit_status=409, submit_body=gone)
    with dc.open_follower("http://coord", client=coordinator, path=follower_path) as follower:
        follower.upsert(Mineral(qid="QC", name="Calcite"))

        with pytest.raises(DanglingRefError) as exc:
            follower.commit()

        assert "gone" in str(exc.value)


def test_contribute_refuses_new_to_new_reference(follower_path: Path | None) -> None:
    """Given a new mineral referencing a new, not yet committed locality
    When both are stored and committed
    Then the commit raises NotImplementedError
    And nothing crossed the wire
    """
    served = FakeCoordinator()
    with dc.open_follower("http://coord", client=served, path=follower_path) as follower:
        loc = Locality(qid="LZ", name="Zomba")
        follower.store(loc)
        follower.store(Mineral(qid="QM", name="X", type_locality=dc.Lazy.of(loc)))

        with pytest.raises(NotImplementedError):
            follower.commit()

        assert served.posts == []
