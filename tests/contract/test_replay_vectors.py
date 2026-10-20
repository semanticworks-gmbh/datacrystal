"""COMMIT-DELTA-v1 replay vectors against the reference applier (spec §6).

The vectors are byte-pinned: if these tests fail after touching the
contract, that is the contract telling you a draft-rev bump (and a spec
edit) is due — never quietly regenerate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from datacrystal.contract import (
    CONTRACT_VERSION,
    DeltaFormatError,
    DeltaGapError,
    ReferenceApplier,
    decode_delta,
    encode_delta,
)

VECTORS = Path("src/datacrystal/contract/vectors")

AppliedBefore = Callable[[int], ReferenceApplier]


@pytest.fixture
def vectors() -> list[bytes]:
    """Return the byte-pinned delta vectors in transaction order."""
    return [path.read_bytes() for path in sorted(VECTORS.glob("*.bin"))]


@pytest.fixture
def pinned() -> dict:
    """Return the pinned expected.json: the contract version and the digest after each tid."""
    return json.loads((VECTORS / "expected.json").read_text())


@pytest.fixture
def applier_before(vectors: list[bytes]) -> AppliedBefore:
    """Return a factory for a reference applier that has applied the first n vectors."""

    def build(count: int) -> ReferenceApplier:
        applier = ReferenceApplier()
        for raw in vectors[:count]:
            applier.apply(raw)
        return applier

    return build


@pytest.fixture
def restamped_applier_before(vectors: list[bytes]) -> AppliedBefore:
    """Return a factory for an applier that has applied the first n vectors under other stamps."""

    def build(count: int) -> ReferenceApplier:
        applier = ReferenceApplier()
        for raw in vectors[:count]:
            applier.apply(restamped(raw))
        return applier

    return build


def restamped(raw: bytes) -> bytes:
    """Return the delta with the actor and time stamps replaced and its ops untouched."""
    delta = decode_delta(raw)
    delta["actor"], delta["at"] = 424242, 1
    return encode_delta(delta)


def test_vectors_are_pinned_at_the_current_contract_version(pinned: dict) -> None:
    """Given the expected.json that pins the replay vectors
    When its contract version is read
    Then it is the current contract version
    """
    assert pinned["contract_version"] == CONTRACT_VERSION


@pytest.mark.parametrize("position", [0, 1, 2, 3, 4])
def test_replay_reaches_the_pinned_digest_after_each_vector(
    position: int, vectors: list[bytes], pinned: dict, applier_before: AppliedBefore
) -> None:
    """Given an applier that applied the vectors before one position
    When the vector at that position is applied
    Then it is accepted
    And the state digest equals the one pinned for its tid
    """
    applier = applier_before(position)

    accepted = applier.apply(vectors[position])

    assert accepted is True
    assert applier.state_digest() == pinned["digests"][str(applier.watermark)]


def test_replay_of_all_vectors_ends_in_the_pinned_object_graph(
    applier_before: AppliedBefore, vectors: list[bytes]
) -> None:
    """Given an applier that replayed all vectors, the last of which upserts calcite anonymously
    When its object graph is read
    Then the root is 4096
    And three objects remain, azurite deleted by 004 and calcite added by 005
    And four type rows exist, the evolved Mineral lineage included
    """
    applier = applier_before(len(vectors))

    assert applier.root_oid == 4096
    assert len(applier.objects) == 3
    assert 4098 not in applier.objects
    assert 4099 in applier.objects
    assert len(applier.types) == 4


@pytest.mark.parametrize("position", [0, 1, 2, 3, 4])
def test_apply_twice_is_apply_once(
    position: int, vectors: list[bytes], pinned: dict, applier_before: AppliedBefore
) -> None:
    """Given an applier that applied the vectors before one position and then that vector
    When the same vector is applied a second time
    Then the second apply is skipped
    And the state digest still equals the one pinned for its tid
    """
    applier = applier_before(position)
    applier.apply(vectors[position])

    applied_again = applier.apply(vectors[position])

    assert applied_again is False
    assert applier.state_digest() == pinned["digests"][str(applier.watermark)]


def test_gap_is_refused_with_a_resync_hint(vectors: list[bytes], applier_before: AppliedBefore) -> None:
    """Given an applier at watermark 1
    When tid 3 is applied
    Then it is refused as a gap that asks to resync
    """
    applier = applier_before(1)

    with pytest.raises(DeltaGapError, match="resync"):
        applier.apply(vectors[2])


def test_refused_gap_leaves_the_orderly_path_working(
    vectors: list[bytes], applier_before: AppliedBefore
) -> None:
    """Given an applier at watermark 1 that refused tid 3 as a gap
    When tid 2 and then tid 3 are applied
    Then both are accepted
    """
    applier = applier_before(1)
    with pytest.raises(DeltaGapError, match="resync"):
        applier.apply(vectors[2])

    second_accepted = applier.apply(vectors[1])
    third_accepted = applier.apply(vectors[2])

    assert second_accepted is True
    assert third_accepted is True


def test_newer_contract_version_is_refused(vectors: list[bytes]) -> None:
    """Given a first vector stamped with a newer contract version
    When a fresh applier applies it
    Then it is refused as newer than supported
    """
    bumped = decode_delta(vectors[0])
    bumped["v"] = CONTRACT_VERSION + 1

    with pytest.raises(DeltaFormatError, match="newer"):
        ReferenceApplier().apply(encode_delta(bumped))


def test_pre_v2_contract_version_is_refused_as_predating(vectors: list[bytes]) -> None:
    """Given a first vector stamped with the contract version before the current one
    When a fresh applier applies it
    Then it is refused with the incompatible-version message, not a missing-key one
    """
    downgraded = decode_delta(vectors[0])
    downgraded["v"] = CONTRACT_VERSION - 1

    with pytest.raises(DeltaFormatError, match="predates"):
        ReferenceApplier().apply(encode_delta(downgraded))


@pytest.mark.parametrize("position", [0, 1, 2, 3, 4])
def test_stamps_never_perturb_the_state_digest(
    position: int,
    vectors: list[bytes],
    pinned: dict,
    restamped_applier_before: AppliedBefore,
) -> None:
    """Given an applier that applied the earlier vectors under other actor and time stamps
    When the vector at one position is applied under those stamps too
    Then it is accepted
    And the state digest equals the one pinned for the original stamps
    """
    applier = restamped_applier_before(position)

    accepted = applier.apply(restamped(vectors[position]))

    assert accepted is True
    assert applier.state_digest() == pinned["digests"][str(applier.watermark)]


def test_unknown_op_is_refused(vectors: list[bytes]) -> None:
    """Given a first vector whose first op is the unknown verb merge
    When a fresh applier applies it
    Then it is refused naming the verb
    """
    mutated = decode_delta(vectors[0])
    mutated["ops"][0]["op"] = "merge"

    with pytest.raises(DeltaFormatError, match="merge"):
        ReferenceApplier().apply(encode_delta(mutated))


def test_inconsistent_prior_is_refused(vectors: list[bytes], applier_before: AppliedBefore) -> None:
    """Given an applier at watermark 1 and tid 2 carrying a prior that tid 1 never wrote
    When tid 2 is applied
    Then it is refused naming the prior
    """
    applier = applier_before(1)
    tampered = decode_delta(vectors[1])
    tampered["ops"][0]["prior"] = b"\x90"

    with pytest.raises(DeltaFormatError, match="prior"):
        applier.apply(encode_delta(tampered))


def test_pinned_tombstone_is_a_delete_without_payload(vectors: list[bytes]) -> None:
    """Given the pinned 004 vector that deletes azurite
    When it is decoded
    Then its one op is a delete with no payload
    And the last payload rides the tombstone as its prior
    """
    tombstone = decode_delta(vectors[3])

    (op,) = tombstone["ops"]

    assert op["op"] == "delete"
    assert op["payload"] is None
    assert op["prior"] is not None


def test_pinned_tombstone_leaves_the_root_dangling_by_design(
    applier_before: AppliedBefore, vectors: list[bytes]
) -> None:
    """Given an applier at watermark 3 and the pinned 004 vector that deletes azurite
    While the pinned root payload still references azurite's OID (ADR-003, unchecked delete)
    When the 004 vector is applied
    Then it is accepted without complaint
    And azurite's object is gone
    And the root record still refs it
    """
    applier = applier_before(3)

    accepted = applier.apply(vectors[3])

    assert accepted is True
    assert 4098 not in applier.objects
    assert applier.root_oid == 4096


def test_delete_of_an_unknown_oid_is_refused(applier_before: AppliedBefore) -> None:
    """Given an applier at watermark 1
    When a delta deletes an OID that never existed
    Then it is refused as not existing
    """
    applier = applier_before(1)
    ghost = {
        "f": "datacrystal-delta",
        "v": 2,
        "tid": 2,
        "actor": 2,
        "at": 0,
        "types": [],
        "ops": [{"op": "delete", "oid": 99999, "cid": 2, "payload": None, "prior": None}],
        "root": 4096,
    }

    with pytest.raises(DeltaFormatError, match="does not exist"):
        applier.apply(encode_delta(ghost))
