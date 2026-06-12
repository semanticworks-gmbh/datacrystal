"""Partitioned 64-bit id space (ROADMAP item 1) and the format version.

Three id families share one signed-64-bit space, partitioned so any id is
self-classifying:

* **CID** — class/type ids, small integers starting at ``CID_BASE``. Assigned
  per store (the same class may have different CIDs in different stores).
* **OID** — object ids, starting at ``OID_BASE`` (above 2**32 so they can
  never collide with CIDs, and EclipseStore-style obviously "not a count").
* **TID** — transaction ids / commit watermarks, a separate sequence starting
  at 1. **Sequence-derived, never wall-clock** (recorded decision in
  KICKOFF.md: deterministic stores, replayable commit streams).

Allocation state is plain in-memory counters; the high-water marks are
persisted in the store's meta table at every commit and restored at boot.
"""

from __future__ import annotations

FORMAT_VERSION = 2
"""Store format version. Bumped on any incompatible layout change; opening a
store with a higher version raises ``NewerStoreError`` (fitness function #18).

v2 (2026-06-12): temporal extension codes 2–4 in record payloads (naive
datetime / date / time as ISO-text exts — they round-tripped as bare strings
before). A v1-era store stays stamped v1 until its first commit under this
library: every commit batch re-stamps ``format_version`` (a v1 reader must
refuse only once v2 payload bytes may actually exist).
"""

CID_BASE = 1
OID_BASE = 1 << 40  # 1_099_511_627_776 — far above any CID, below 2**63
TID_BASE = 1


def is_oid(value: int) -> bool:
    return value >= OID_BASE


def is_cid(value: int) -> bool:
    return CID_BASE <= value < OID_BASE


class IdAllocator:
    """Hands out fresh OIDs/CIDs/TIDs; restored from persisted high-water marks."""

    __slots__ = ("_next_oid", "_next_cid", "_next_tid")

    def __init__(self, next_oid: int = OID_BASE, next_cid: int = CID_BASE,
                 next_tid: int = TID_BASE) -> None:
        self._next_oid = next_oid
        self._next_cid = next_cid
        self._next_tid = next_tid

    def next_oid(self) -> int:
        oid = self._next_oid
        self._next_oid = oid + 1
        return oid

    def next_cid(self) -> int:
        cid = self._next_cid
        self._next_cid = cid + 1
        return cid

    def next_tid(self) -> int:
        tid = self._next_tid
        self._next_tid = tid + 1
        return tid

    @property
    def oid_watermark(self) -> int:
        return self._next_oid

    @property
    def cid_watermark(self) -> int:
        return self._next_cid

    @property
    def tid_watermark(self) -> int:
        return self._next_tid
