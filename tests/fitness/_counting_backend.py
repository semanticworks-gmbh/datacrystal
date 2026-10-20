"""The counting storage wrapper of the KICKOFF fitness gates (#19): count ``load_many`` traffic."""

from __future__ import annotations

from collections.abc import Iterator

from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import (
    BootInfo,
    CommitBatch,
    StorageReadView,
    StoredBlob,
    StoredRecord,
)


class CountingBackend:
    """Count the ``load_many`` calls and records of a backend.

    ``scan_type`` and so index builds are the documented one-time cost and deliberately
    uncounted (the #19 precedent).
    """

    def __init__(self, inner: MemoryBackend) -> None:
        self._inner = inner
        self.load_calls = 0
        self.records_loaded = 0

    def reset(self) -> None:
        """Zero both counters."""
        self.load_calls = 0
        self.records_loaded = 0

    def boot(self) -> BootInfo:
        """Delegate to the inner backend."""
        return self._inner.boot()

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        """Load the records and count the call and the records."""
        out = self._inner.load_many(oids)
        self.load_calls += 1
        self.records_loaded += len(out)
        return out

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        """Delegate to the inner backend, uncounted."""
        return self._inner.scan_type(cid)

    def load_blob(self, oid: int) -> StoredBlob | None:
        """Delegate to the inner backend."""
        return self._inner.load_blob(oid)

    def apply(self, batch: CommitBatch) -> None:
        """Delegate to the inner backend."""
        self._inner.apply(batch)

    def read_view(self) -> StorageReadView:
        """Delegate to the inner backend."""
        return self._inner.read_view()

    def close(self) -> None:
        """Delegate to the inner backend."""
        self._inner.close()
