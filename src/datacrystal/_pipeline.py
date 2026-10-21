"""Commit-delta emission and consumer attachment (the watermark pipeline).

The engine side of [COMMIT-DELTA-v2](../../docs/design/COMMIT-DELTA-v2.md); the consumer side
lives in ``datacrystal/contract/``.
With no consumer attached, commits pay nothing: no prior-payload reads, no delta encoding.
Priors are read before the TID is allocated, so a failed read consumes no TID (invariant 5).
A consumer that raises is detached with a ``ConsumerDetachedWarning`` (invariant 11).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from datacrystal._storage.protocol import StoredRecord
from datacrystal.contract.applier import CONTRACT_VERSION, FORMAT_MARKER


@runtime_checkable
class DeltaConsumer(Protocol):
    """What ``store.attach()`` accepts — the spec §4 consumer surface.

    ``watermark`` is the highest TID fully applied (0 for a fresh consumer).
    ``apply()`` receives one decoded delta map; file and queue transports deliver msgpack bytes
    that ``contract.decode_delta`` turns into the same shape.
    """

    @property
    def watermark(self) -> int: ...

    def apply(self, delta: dict[str, Any]) -> Any: ...


def build_delta(tid: int, records: list[StoredRecord],
                new_types: list[tuple[int, str, list[str]]],
                root_oid: int | None,
                priors: dict[int, bytes],
                deletes: Sequence[tuple[int, int, bytes]] = (), *,
                actor: int, at: int) -> dict[str, Any]:
    """Assemble one COMMIT-DELTA-v2 map (spec §2/§3) from P1's capture.

    Upserts come first in capture order (``priors`` maps OID to previous payload; absent means
    created), then delete tombstones as ``(oid, cid, last payload)``; an OID is never in both.
    ``actor`` (uid, 0 = anonymous) and ``at`` (ns from the store clock) are the required v2 audit
    stamps (ADR-008 batch 1), keyword-only and defaultless so no call site can omit them.
    """
    ops: list[dict[str, Any]] = [
        {
            "op": "upsert",
            "oid": rec.oid,
            "cid": rec.cid,
            "payload": rec.payload,
            "prior": priors.get(rec.oid),
        }
        for rec in records
    ]
    ops.extend(
        {"op": "delete", "oid": oid, "cid": cid, "payload": None, "prior": prior}
        for oid, cid, prior in deletes
    )
    return {
        "f": FORMAT_MARKER,
        "v": CONTRACT_VERSION,
        "tid": tid,
        "actor": actor,
        "at": at,
        "ops": ops,
        "types": [[cid, typename, list(fields)] for cid, typename, fields in new_types],
        "root": root_oid,
    }
