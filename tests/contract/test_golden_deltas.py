"""Byte-pinned golden deltas with version-bump-or-fail (KICKOFF M3).

The engine's emitted delta stream for the scripted golden session is pinned
byte for byte, and the registry maps draft rev → stream digest append-only
(the fitness-#7 pattern applied to the contract): changing the bytes under
an existing rev fails mechanically, even if someone regenerates the .bin
files. A draft revision is a DELIBERATE act — bump the rev in the spec and
the applier, regenerate, and add the new registry row.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from datacrystal.contract import CONTRACT_VERSION, ReferenceApplier, decode_delta
from tests.contract.golden_gen import stream

GOLDENS = Path(__file__).parent / "goldens"

BUMP_HINT = (
    "the emitted delta stream changed for the SAME draft rev. If this is a "
    "deliberate contract revision: bump the draft rev (spec + applier), "
    "regenerate the goldens, and APPEND the new rev to registry.json — "
    "never edit an existing row (COMMIT-DELTA-v1 is byte-pinned)."
)


def golden_bytes(name: str) -> bytes:
    """Return the pinned bytes of one golden delta file."""
    return (GOLDENS / name).read_bytes()


def stream_digest() -> str:
    """Return the sha256 over the live engine stream, as registry.json pins it per draft rev."""
    digest = hashlib.sha256()
    for raw in stream():
        digest.update(raw)
    return digest.hexdigest()


def test_engine_stream_has_as_many_deltas_as_are_pinned() -> None:
    """Given the scripted golden session and the pinned delta files
    When the engine emits the session's stream
    Then it emits exactly as many deltas as are pinned
    """
    pinned = sorted(GOLDENS.glob("delta-*.bin"))

    live = stream()

    assert len(live) == len(pinned), BUMP_HINT


@pytest.mark.parametrize(
    ("position", "name"),
    [(0, "delta-001.bin"), (1, "delta-002.bin"), (2, "delta-003.bin")],
)
def test_engine_stream_matches_each_pinned_delta_byte_for_byte(position: int, name: str) -> None:
    """Given the scripted golden session and a pinned delta file
    When the engine emits the session's stream
    Then the delta at that position equals the pinned bytes
    """
    live = stream()

    assert live[position] == golden_bytes(name), f"{name}: {BUMP_HINT}"


def test_registry_has_a_row_for_the_current_draft_rev() -> None:
    """Given the append-only registry of stream digests
    When the current draft rev is looked up
    Then the registry has a row for it
    """
    registry = json.loads((GOLDENS / "registry.json").read_text())

    assert str(CONTRACT_VERSION) in registry, (
        f"draft rev {CONTRACT_VERSION} has no registry row — append it"
    )


def test_registry_digest_equals_the_live_stream_digest() -> None:
    """Given the registry row of the current draft rev
    When the engine emits the golden session's stream
    Then the stream digest equals the registry row
    """
    registry = json.loads((GOLDENS / "registry.json").read_text())

    live_digest = stream_digest()

    assert registry[str(CONTRACT_VERSION)] == live_digest, BUMP_HINT


def test_goldens_replay_through_the_reference_applier() -> None:
    """Given the three pinned goldens and a fresh reference applier
    When the applier applies them in order
    Then every delta is accepted
    And the applier's watermark is the last transaction id
    """
    applier = ReferenceApplier()

    first_accepted = applier.apply(golden_bytes("delta-001.bin"))
    second_accepted = applier.apply(golden_bytes("delta-002.bin"))
    third_accepted = applier.apply(golden_bytes("delta-003.bin"))

    assert first_accepted is True
    assert second_accepted is True
    assert third_accepted is True
    assert applier.watermark == 3


def test_golden_update_carries_the_prior_payload_of_the_updated_entity() -> None:
    """Given the pinned golden of transaction 2, which updated azurite
    When the delta is decoded
    Then one op carries the prior payload of transaction 1
    """
    update = decode_delta(golden_bytes("delta-002.bin"))

    assert any(op["prior"] is not None for op in update["ops"]), "tid 2 must carry a prior"


def test_golden_update_carries_no_prior_for_the_created_entity() -> None:
    """Given the pinned golden of transaction 2, which also created topaz
    When the delta is decoded
    Then one op carries no prior payload
    """
    update = decode_delta(golden_bytes("delta-002.bin"))

    assert any(op["prior"] is None for op in update["ops"]), "tid 2 also created topaz"
