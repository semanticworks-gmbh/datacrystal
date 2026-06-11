"""In-memory storage fake: protocol-faithful, durable for the process.

Exists so the engine test suite runs parametrized over (sqlite, memory) —
the fake keeps data across Store close/reopen as long as the same backend
instance is reused, which is exactly what reopen-semantics tests need
without touching disk.
"""

from __future__ import annotations

from typing import Iterator

from datacrystal._errors import NewerStoreError
from datacrystal._ids import FORMAT_VERSION
from datacrystal._storage.protocol import BootInfo, CommitBatch, StoredRecord


class MemoryBackend:
    def __init__(self) -> None:
        self._meta: dict[str, str] = {"format_version": str(FORMAT_VERSION)}
        self._types: list[tuple[int, str, list[str]]] = []
        self._objects: dict[int, StoredRecord] = {}

    def boot(self) -> BootInfo:
        stored = int(self._meta["format_version"])
        if stored > FORMAT_VERSION:
            raise NewerStoreError(
                f"store format v{stored} is newer than this library "
                f"supports (v{FORMAT_VERSION}); upgrade datacrystal to open it"
            )
        return BootInfo(meta=dict(self._meta), types=list(self._types))

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        objects = self._objects
        return {oid: objects[oid] for oid in oids if oid in objects}

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        for rec in sorted(
            (r for r in self._objects.values() if r.cid == cid), key=lambda r: r.oid
        ):
            yield rec

    def apply(self, batch: CommitBatch) -> None:
        self._types.extend(batch.new_types)
        for rec in batch.records:
            self._objects[rec.oid] = rec
        self._meta.update(batch.meta)

    def close(self) -> None:
        pass
