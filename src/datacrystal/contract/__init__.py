"""COMMIT-DELTA-v1 contract package (DRAFT rev 1) — engine-free.

The reference applier and codec for the commit-delta/watermark stream
(ROADMAP item 3; spec: docs/design/COMMIT-DELTA-v1.md). Nothing in here
imports the engine — the fitness suite asserts that mechanically.
"""

from datacrystal.contract.applier import (
    CONTRACT_VERSION,
    DeltaFormatError,
    DeltaGapError,
    ReferenceApplier,
    decode_delta,
    encode_delta,
)

__all__ = [
    "CONTRACT_VERSION",
    "DeltaFormatError",
    "DeltaGapError",
    "ReferenceApplier",
    "decode_delta",
    "encode_delta",
]
