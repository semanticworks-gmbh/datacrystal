"""The storage protocol: the seam every backend implements.

SQLite is the only durable backend in v0.x (ROADMAP item 2); the in-memory fake is for fast tests.
A later append-only log (ROADMAP punt 14) or Rust wheel slots in behind the same small protocol.
Growing it needs an ADR (ADR-002 added ``read_view()`` for snapshot reads isolated from commit P2).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, Protocol


@dataclass(frozen=True, slots=True)
class StoredRecord:
    oid: int
    cid: int
    tid: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class StoredBlob:
    """One out-of-line blob value (ADR-007): raw bytes addressed by their own
    OID. Blobs are **typeless** — no ``cid`` (a blob has no class or lineage,
    so the descriptor in the referring record carries everything the engine
    needs); ``tid`` is the commit that wrote it, ``size``/``hash`` (sha256)
    mirror the descriptor, ``data`` is the RAW bytes (never msgpack-framed, so
    a future ``blobopen`` can range-read them).
    """

    oid: int
    tid: int
    size: int
    hash: bytes
    data: bytes


@dataclass(frozen=True, slots=True)
class StreamedBlob:
    """A blob written by streaming (ADR-007 §4); the bytes are not resident.

    ``open_chunks()`` yields them on demand and the backend fills a ``zeroblob(size)`` cell in place
    inside ``apply()``.
    ``size`` and ``hash`` are final from the engine's pre-TID hashing pass; the backend recomputes
    only the ``crc``.
    The in-memory fake has no ``blobopen`` and materializes the chunks instead.
    """

    oid: int
    tid: int
    size: int
    hash: bytes
    open_chunks: Callable[[], Iterable[bytes]]


@dataclass(slots=True)
class CommitBatch:
    """Everything one commit persists, applied atomically by the backend.

    This in-process shape foreshadows the public commit-delta contract
    (ROADMAP item 3, drafted at M2) but is NOT yet that contract — do not
    build external consumers against it.
    """

    tid: int
    records: list[StoredRecord] = field(default_factory=list[StoredRecord])
    new_types: list[tuple[int, str, list[str]]] = field(
        default_factory=list[tuple[int, str, list[str]]]
    )  # (cid, typename, fields)
    meta: dict[str, str] = field(default_factory=dict[str, str])
    # OIDs whose rows this commit removes (ADR-003) — applied in the same
    # atomic transaction as the records; never overlaps records' OIDs.
    deletes: list[int] = field(default_factory=list[int])
    # Out-of-line blob values written by this commit (ADR-007), inserted in the
    # SAME atomic transaction as the records → a blob and its referring record
    # both survive a crash or neither does. Blobs are immutable: a changed blob
    # mints a new OID, the old cell is never touched.
    blobs: list[StoredBlob] = field(default_factory=list[StoredBlob])
    # Out-of-line blobs written by STREAMING (ADR-007 §4) — same atomic
    # transaction, but filled chunk-by-chunk from a source rather than handed
    # over whole. Disjoint from ``blobs``: a value is either resident or streamed.
    blob_streams: list[StreamedBlob] = field(default_factory=list[StreamedBlob])


@dataclass(slots=True)
class BootInfo:
    meta: dict[str, str]
    types: list[tuple[int, str, list[str]]]  # (cid, typename, field names)


class StorageReadView(Protocol):
    """A stable, read-only view of the store at one durable commit boundary.

    Created by :meth:`StorageBackend.read_view` — safe to create from ANY
    thread while the owner commits (ADR-001 rider 2: this is what
    ``store.snapshot()`` stands on). ``boot()`` here only *reads* meta and
    type rows (no DDL, no version repair); a view holds resources (e.g. a
    pinned WAL read transaction) until ``close()``.
    """

    def boot(self) -> BootInfo: ...

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]: ...

    def scan_type(self, cid: int) -> Iterator[StoredRecord]: ...

    def load_blob(self, oid: int) -> StoredBlob | None: ...

    def open_blob_stream(
        self, oid: int, on_close: Callable[[], None] | None = None
    ) -> BinaryIO:
        """Open a file-like reader over one blob's raw bytes (ADR-007 §3).

        ``read``/``seek``/``tell`` touch only the spanned bytes, so a range read is RSS-bounded.
        ``on_close`` runs when the stream closes; the engine passes a read view's ``close`` to
        release its pinned transaction.
        Raises :class:`~datacrystal._errors.DanglingRefError` if the blob OID is absent.
        """
        ...

    def close(self) -> None: ...


class StorageBackend(Protocol):
    def boot(self) -> BootInfo: ...

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]: ...

    def scan_type(self, cid: int) -> Iterator[StoredRecord]: ...

    def load_blob(self, oid: int) -> StoredBlob | None: ...

    def apply(self, batch: CommitBatch) -> None: ...

    def read_view(self) -> StorageReadView: ...

    def close(self) -> None: ...
