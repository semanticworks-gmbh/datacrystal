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

from datacrystal.contract import CONTRACT_VERSION, ReferenceApplier, decode_delta
from tests.contract.golden_gen import stream

GOLDENS = Path(__file__).parent / "goldens"

BUMP_HINT = (
    "the emitted delta stream changed for the SAME draft rev. If this is a "
    "deliberate contract revision: bump the draft rev (spec + applier), "
    "regenerate the goldens, and APPEND the new rev to registry.json — "
    "never edit an existing row (COMMIT-DELTA-v1 is byte-pinned)."
)


def test_engine_stream_matches_the_pinned_bytes():
    pinned = sorted(GOLDENS.glob("delta-*.bin"))
    live = stream()
    assert len(live) == len(pinned), BUMP_HINT
    for raw, path in zip(live, pinned):
        assert raw == path.read_bytes(), f"{path.name}: {BUMP_HINT}"


def test_registry_is_append_only_per_draft_rev():
    registry = json.loads((GOLDENS / "registry.json").read_text())
    assert str(CONTRACT_VERSION) in registry, (
        f"draft rev {CONTRACT_VERSION} has no registry row — append it"
    )
    h = hashlib.sha256()
    for raw in stream():
        h.update(raw)
    assert registry[str(CONTRACT_VERSION)] == h.hexdigest(), BUMP_HINT


def test_goldens_replay_through_the_reference_applier():
    """The pinned engine output satisfies the pinned consumer — the two
    byte-pinned worlds (emission, application) agree."""
    applier = ReferenceApplier()
    for path in sorted(GOLDENS.glob("delta-*.bin")):
        assert applier.apply(path.read_bytes()) is True
    assert applier.watermark == 3
    # tid 2 updated azurite: its op carried tid 1's payload as prior —
    # the applier's strict prior verification already proved consistency;
    # spot-check the tombstone-relevant shape survives the pin too
    update = decode_delta((GOLDENS / "delta-002.bin").read_bytes())
    priors = [op["prior"] for op in update["ops"]]
    assert any(p is not None for p in priors), "tid 2 must carry a prior"
    assert any(p is None for p in priors), "tid 2 also created topaz"
