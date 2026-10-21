"""Author the COMMIT-DELTA-v2 replay vectors (`uv run scripts/generate_vectors.py`).

Run once per contract version and commit the outputs.
The vectors are byte-pinned: regenerating them is a contract-version bump with a new spec, never a
quiet refresh (COMMIT-DELTA-v2.md §6).
The payloads are hand-built and deterministic (fixed OIDs, a pinned ``at``, no randomness),
independent of the engine on purpose.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import msgspec

from datacrystal.contract.applier import ReferenceApplier, encode_delta

HERE = Path(__file__).resolve().parent.parent / "src" / "datacrystal" / "contract" / "vectors"
_enc = msgspec.msgpack.Encoder()


def ref(oid: int) -> msgspec.msgpack.Ext:
    """Return an entity reference: ext type 1 carrying the 8-byte OID."""
    return msgspec.msgpack.Ext(1, struct.pack(">q", oid))


# The mineral cabinet, as always. OIDs start at 4096 (the engine's OID_BASE
# partition is an engine detail; the contract only needs ints).
ROOT, TSUMEB, AZURITE, CALCITE = 4096, 4097, 4098, 4099

ROOT_CID, LOCALITY_CID, MINERAL_CID, MINERAL_V2_CID = 1, 2, 3, 4

locality_v1 = _enc.encode(["Q571997", "Tsumeb Mine"])
azurite_v1 = _enc.encode(["Q193563", "azurite", "monoclinic", ref(TSUMEB)])
azurite_v2 = _enc.encode(["Q193563", "azurite", "triclinic", ref(TSUMEB)])
root_v1 = _enc.encode([[ref(AZURITE)]])
# tid 3 evolves Mineral additively: + mohs (new lineage row, new cid)
azurite_v3 = _enc.encode(["Q193563", "azurite", "triclinic", ref(TSUMEB), 3.7])
# tid 5's anonymous upsert (the v1-shape Mineral cid — 4 fields)
calcite_v1 = _enc.encode(["Q23744", "calcite", "trigonal", ref(TSUMEB)])

# Pinned v2 stamps: `at` is a fixed epoch-ns base plus the tid (documenting
# per-delta instants without a real clock); actors tell the walkthrough's
# story — 1 bootstraps, 2 (the curator) works.
AT_NS = 1_700_000_000_000_000_000

DELTAS = [
    ("001-genesis", {
        "f": "datacrystal-delta", "v": 2, "tid": 1,
        "actor": 1, "at": AT_NS + 1,
        "types": [
            [ROOT_CID, "datacrystal._store:_Root", ["value"]],
            [LOCALITY_CID, "minerals:Locality", ["qid", "name"]],
            [MINERAL_CID, "minerals:Mineral",
             ["qid", "name", "crystal_system", "type_locality"]],
        ],
        "ops": [
            {"op": "upsert", "oid": TSUMEB, "cid": LOCALITY_CID,
             "payload": locality_v1, "prior": None},
            {"op": "upsert", "oid": AZURITE, "cid": MINERAL_CID,
             "payload": azurite_v1, "prior": None},
            {"op": "upsert", "oid": ROOT, "cid": ROOT_CID,
             "payload": root_v1, "prior": None},
        ],
        "root": ROOT,
    }),
    ("002-update", {
        "f": "datacrystal-delta", "v": 2, "tid": 2,
        "actor": 2, "at": AT_NS + 2,
        "types": [],
        "ops": [
            {"op": "upsert", "oid": AZURITE, "cid": MINERAL_CID,
             "payload": azurite_v2, "prior": azurite_v1},
        ],
        "root": ROOT,
    }),
    ("003-evolution", {
        "f": "datacrystal-delta", "v": 2, "tid": 3,
        "actor": 2, "at": AT_NS + 3,
        "types": [
            [MINERAL_V2_CID, "minerals:Mineral",
             ["qid", "name", "crystal_system", "type_locality", "mohs"]],
        ],
        "ops": [
            {"op": "upsert", "oid": AZURITE, "cid": MINERAL_V2_CID,
             "payload": azurite_v3, "prior": azurite_v2},
        ],
        "root": ROOT,
    }),
    # 004 was authored at M4 when store.delete() activated the shape reserved in spec §3.1
    # (additive; 001-003 stay byte-identical). The tombstone carries the last payload as prior.
    # The root still references the deleted AZURITE (unchecked delete, ADR-003): a consumer applies
    # it without complaint, and only following the dangle is an error.
    ("004-delete", {
        "f": "datacrystal-delta", "v": 2, "tid": 4,
        "actor": 2, "at": AT_NS + 4,
        "types": [],
        "ops": [
            {"op": "delete", "oid": AZURITE, "cid": MINERAL_V2_CID,
             "payload": None, "prior": azurite_v3},
        ],
        "root": ROOT,
    }),
    # 005: the ANONYMOUS end of always-stamp-both (COMMIT-DELTA-v2 §1),
    # documented in bytes — actor 0 is a stamp like any other, `at` still
    # present. A store opened without a principal emits exactly this shape.
    ("005-anonymous", {
        "f": "datacrystal-delta", "v": 2, "tid": 5,
        "actor": 0, "at": AT_NS + 5,
        "types": [],
        "ops": [
            {"op": "upsert", "oid": CALCITE, "cid": MINERAL_CID,
             "payload": calcite_v1, "prior": None},
        ],
        "root": ROOT,
    }),
]


def main() -> None:
    """Write the vector files and ``expected.json`` next to the contract package."""
    applier = ReferenceApplier()
    digests: dict[str, str] = {}
    for name, delta in DELTAS:
        raw = encode_delta(delta)
        (HERE / f"{name}.bin").write_bytes(raw)
        assert applier.apply(raw) is True
        digests[str(delta["tid"])] = applier.state_digest()
    (HERE / "expected.json").write_text(
        json.dumps({"contract_version": 2, "digests": digests}, indent=2) + "\n"
    )
    print(f"wrote {len(DELTAS)} vectors; final digest {applier.state_digest()}")


if __name__ == "__main__":
    main()
