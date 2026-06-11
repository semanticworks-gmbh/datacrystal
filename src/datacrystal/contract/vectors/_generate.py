"""Author the COMMIT-DELTA-v1 replay vectors (DRAFT rev 1).

Run once per draft rev (`uv run python src/datacrystal/contract/vectors/_generate.py`)
and commit the outputs. The vectors are BYTE-PINNED: regenerating them is a
draft-rev bump with a spec edit, never a quiet refresh (spec §6).

Deterministic by construction — fixed OIDs/strings, no clock, no randomness.
The payloads are hand-built record encodings (msgpack value lists with
entity refs as ext-type-1 8-byte OIDs), independent of the engine on
purpose: the contract defines what the engine must emit at M3, not the
other way around.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import msgspec

from datacrystal.contract.applier import ReferenceApplier, encode_delta

HERE = Path(__file__).parent
_enc = msgspec.msgpack.Encoder()


def ref(oid: int) -> msgspec.msgpack.Ext:
    return msgspec.msgpack.Ext(1, struct.pack(">q", oid))


# The mineral cabinet, as always. OIDs start at 4096 (the engine's OID_BASE
# partition is an engine detail; the contract only needs ints).
ROOT, TSUMEB, AZURITE = 4096, 4097, 4098

ROOT_CID, LOCALITY_CID, MINERAL_CID, MINERAL_V2_CID = 1, 2, 3, 4

locality_v1 = _enc.encode(["Q571997", "Tsumeb Mine"])
azurite_v1 = _enc.encode(["Q193563", "azurite", "monoclinic", ref(TSUMEB)])
azurite_v2 = _enc.encode(["Q193563", "azurite", "triclinic", ref(TSUMEB)])
root_v1 = _enc.encode([[ref(AZURITE)]])
# tid 3 evolves Mineral additively: + mohs (new lineage row, new cid)
azurite_v3 = _enc.encode(["Q193563", "azurite", "triclinic", ref(TSUMEB), 3.7])

DELTAS = [
    ("001-genesis", {
        "f": "datacrystal-delta", "v": 1, "tid": 1,
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
        "f": "datacrystal-delta", "v": 1, "tid": 2,
        "types": [],
        "ops": [
            {"op": "upsert", "oid": AZURITE, "cid": MINERAL_CID,
             "payload": azurite_v2, "prior": azurite_v1},
        ],
        "root": ROOT,
    }),
    ("003-evolution", {
        "f": "datacrystal-delta", "v": 1, "tid": 3,
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
]


def main() -> None:
    applier = ReferenceApplier()
    digests: dict[str, str] = {}
    for name, delta in DELTAS:
        raw = encode_delta(delta)
        (HERE / f"{name}.bin").write_bytes(raw)
        assert applier.apply(raw) is True
        digests[str(delta["tid"])] = applier.state_digest()
    (HERE / "expected.json").write_text(
        json.dumps({"contract_version": 1, "digests": digests}, indent=2) + "\n"
    )
    print(f"wrote {len(DELTAS)} vectors; final digest {applier.state_digest()}")


if __name__ == "__main__":
    main()
