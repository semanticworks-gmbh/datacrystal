"""In-memory storage fake: protocol-faithful, durable for the process.

Exists so the engine test suite runs parametrized over (sqlite, memory) —
the fake keeps data across Store close/reopen as long as the same backend
instance is reused, which is exactly what reopen-semantics tests need
without touching disk.

Thread safety mirrors the sqlite backend's serialized connection: commit P2
calls ``apply()`` from the store's IO worker thread while the owner thread
may keep reading (ADR-001), so the fake serializes its calls with a lock.
"""

from __future__ import annotations

import hashlib
import io
import threading
from typing import BinaryIO, Callable, Iterator

from datacrystal._errors import DanglingRefError, NewerStoreError
from datacrystal._ids import FORMAT_VERSION
from datacrystal._storage.protocol import BootInfo, CommitBatch, StoredBlob, StoredRecord


class _MemoryBlobStream(io.BytesIO):
    """``io.BytesIO`` over a copy of one blob's bytes — the memory backend's
    streaming fallback (ADR-007 §3: it has no ``blobopen``, so it cannot be
    RSS-bounded; the bytes are already resident anyway). Closing runs the
    view's ``on_close`` for symmetry with the sqlite reader."""

    def __init__(self, data: bytes, on_close: Callable[[], None] | None) -> None:
        super().__init__(data)
        self._on_close = on_close

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._on_close is not None:
                self._on_close()


class _MemoryReadView:
    """A point-in-time copy of the fake's state (ADR-002 read view).

    The copy is taken under the backend lock, so it is exactly one commit
    boundary — the semantic twin of the sqlite view's pinned WAL read
    transaction (records are frozen dataclasses; sharing them is safe).
    """

    def __init__(self, meta: dict[str, str], types: list[tuple[int, str, list[str]]],
                 objects: dict[int, StoredRecord],
                 blobs: dict[int, StoredBlob]) -> None:
        self._meta = meta
        self._types = types
        self._objects = objects
        self._blobs = blobs

    def boot(self) -> BootInfo:
        return BootInfo(meta=dict(self._meta), types=list(self._types))

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        objects = self._objects
        return {oid: objects[oid] for oid in oids if oid in objects}

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        yield from sorted(
            (r for r in self._objects.values() if r.cid == cid), key=lambda r: r.oid
        )

    def load_blob(self, oid: int) -> StoredBlob | None:
        return self._blobs.get(oid)

    def open_blob_stream(
        self, oid: int, on_close: Callable[[], None] | None = None
    ) -> BinaryIO:
        stored = self._blobs.get(oid)
        if stored is None:
            if on_close is not None:
                on_close()
            raise DanglingRefError(
                f"no blob for oid {oid} in the store — deleted (ADR-003) or "
                "never committed; the blob reference you followed is stale"
            )
        return _MemoryBlobStream(stored.data, on_close)

    def close(self) -> None:
        pass


class MemoryBackend:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._meta: dict[str, str] = {"format_version": str(FORMAT_VERSION)}
        self._types: list[tuple[int, str, list[str]]] = []
        self._objects: dict[int, StoredRecord] = {}
        self._blobs: dict[int, StoredBlob] = {}  # out-of-line bytes (ADR-007)

    def boot(self) -> BootInfo:
        with self._lock:
            stored = int(self._meta["format_version"])
            if stored > FORMAT_VERSION:
                raise NewerStoreError(
                    f"store format v{stored} is newer than this library "
                    f"supports (v{FORMAT_VERSION}); upgrade datacrystal to open it"
                )
            return BootInfo(meta=dict(self._meta), types=list(self._types))

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        with self._lock:
            objects = self._objects
            return {oid: objects[oid] for oid in oids if oid in objects}

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        with self._lock:
            snapshot = sorted(
                (r for r in self._objects.values() if r.cid == cid), key=lambda r: r.oid
            )
        yield from snapshot

    def load_blob(self, oid: int) -> StoredBlob | None:
        with self._lock:
            return self._blobs.get(oid)

    def apply(self, batch: CommitBatch) -> None:
        with self._lock:
            # Materialize streamed blobs FIRST — consuming a source is the only
            # step that can fail (it may raise, or lie about its size), so do it
            # before mutating any state. The list comprehension completes wholly
            # or raises wholly, giving the same all-or-nothing atomicity the
            # sqlite backend gets from BEGIN IMMEDIATE/ROLLBACK (the SIGKILL/fail
            # tests demand both backends behave identically).
            streamed: list[StoredBlob] = []
            for sb in batch.blob_streams:  # no blobopen → materialize the chunks
                data = b"".join(sb.open_chunks())
                if len(data) != sb.size:
                    raise ValueError(
                        f"streamed blob oid={sb.oid} produced {len(data)} bytes "
                        f"but declared size {sb.size}"
                    )
                if hashlib.sha256(data).digest() != sb.hash:
                    # Same guard as the sqlite fill: a source whose two passes
                    # disagree would store a descriptor hash that lies about the
                    # bytes. Reject before any state mutates (atomicity parity).
                    raise ValueError(
                        f"streamed blob oid={sb.oid} bytes changed between the "
                        "hashing pass and the fill pass (the source is "
                        "non-deterministic)"
                    )
                streamed.append(StoredBlob(
                    oid=sb.oid, tid=sb.tid, size=sb.size, hash=sb.hash, data=data
                ))
            self._types.extend(batch.new_types)
            for rec in batch.records:
                self._objects[rec.oid] = rec
            for blob in batch.blobs:
                self._blobs[blob.oid] = blob
            for sb_stored in streamed:
                self._blobs[sb_stored.oid] = sb_stored
            for oid in batch.deletes:
                self._objects.pop(oid, None)
            self._meta.update(batch.meta)

    def read_view(self) -> _MemoryReadView:
        with self._lock:
            return _MemoryReadView(dict(self._meta), list(self._types),
                                   dict(self._objects), dict(self._blobs))

    def close(self) -> None:
        pass
