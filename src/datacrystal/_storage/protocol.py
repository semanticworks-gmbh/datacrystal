"""The storage protocol: the seam every backend implements.

SQLite-as-blob-store is the only durable backend in v0.x (ROADMAP item 2);
the in-memory fake exists for fast tests. The custom append-only log
(ROADMAP punt 14) and a future Rust turbo wheel slot in behind this same
protocol — that is the whole point of keeping it this small:

* ``boot()``       — open/create, verify format version, return meta + types
* ``load_many()``  — batch-read records by OID
* ``scan_type()``  — stream all records of one type (index builds)
* ``apply()``      — atomically persist one commit batch
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol


@dataclass(frozen=True, slots=True)
class StoredRecord:
    oid: int
    cid: int
    tid: int
    payload: bytes


@dataclass(slots=True)
class CommitBatch:
    """Everything one commit persists, applied atomically by the backend.

    This in-process shape foreshadows the public commit-delta contract
    (ROADMAP item 3, drafted at M2) but is NOT yet that contract — do not
    build external consumers against it.
    """

    tid: int
    records: list[StoredRecord] = field(default_factory=list)
    new_types: list[tuple[int, str, list[str]]] = field(default_factory=list)  # (cid, typename, fields)
    meta: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class BootInfo:
    meta: dict[str, str]
    types: list[tuple[int, str, list[str]]]  # (cid, typename, field names)


class StorageBackend(Protocol):
    def boot(self) -> BootInfo: ...

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]: ...

    def scan_type(self, cid: int) -> Iterator[StoredRecord]: ...

    def apply(self, batch: CommitBatch) -> None: ...

    def close(self) -> None: ...
