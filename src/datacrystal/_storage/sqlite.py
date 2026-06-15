"""SQLite-as-blob-store: the durable v0.x backend (ROADMAP item 2).

datacrystal writes object records as opaque msgpack blobs into a single
SQLite file and rides SQLite's journal for crash atomicity — we add no
custom on-disk format in v0.x, which is exactly why the crash-consistency
story is honest from day one (KICKOFF risk 4). The boot index *is* the
B-tree (ROADMAP punt 14: no boot problem to solve).

Durability is the KICKOFF M2 fsync triad (per-commit / interval / never):
* ``durability="commit"`` — ``synchronous=FULL`` plus ``fullfsync=ON`` on
  macOS (the ~4 ms F_FULLFSYNC floor measured in the feasibility study;
  honesty over speed). Every acked commit survives power loss.
* ``durability="interval"`` (default) — WAL group-commit:
  ``synchronous=NORMAL`` fsyncs at WAL checkpoints, so commits between
  checkpoints may be lost on OS crash/power loss but the file is never
  corrupted. Process crash (kill -9) loses nothing under any policy.
* ``durability="never"`` — ``synchronous=OFF``; benchmarks and scratch
  stores only. OS crash/power loss can corrupt the file.
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
import sys
import zlib
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, cast

from datacrystal._errors import (
    CorruptRecordError,
    DanglingRefError,
    NewerStoreError,
    StoreClosedError,
)
from datacrystal._ids import FORMAT_VERSION
from datacrystal._records import crc as _crc
from datacrystal._storage.protocol import BootInfo, CommitBatch, StoredBlob, StoredRecord

# A plain rowid table (NOT WITHOUT ROWID): a future streamed read uses
# sqlite3.Connection.blobopen, which needs the oid to be a rowid alias
# (ADR-007 §2). Blobs are TYPELESS — no cid column (a blob has no class or
# lineage; this is a deliberate deviation from the schema listed in ADR-007,
# whose `cid` column is meaningless for an opaque value). crc is the torn-blob
# guard, mirroring the objects table.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS types (
    cid    INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,
    fields TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS objects (
    oid     INTEGER PRIMARY KEY,
    cid     INTEGER NOT NULL,
    tid     INTEGER NOT NULL,
    payload BLOB NOT NULL,
    crc     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS objects_by_cid ON objects (cid);
CREATE TABLE IF NOT EXISTS blobs (
    oid  INTEGER PRIMARY KEY,
    tid  INTEGER NOT NULL,
    size INTEGER NOT NULL,
    hash BLOB NOT NULL,
    crc  INTEGER NOT NULL,
    data BLOB NOT NULL
);
"""

_LOAD_CHUNK = 500


def _check_crc(oid: int, payload: bytes, stored_crc: int) -> None:
    if _crc(payload) != stored_crc:
        raise CorruptRecordError(
            f"record oid={oid} failed its checksum — the store file is damaged"
        )


def _load_many(conn: sqlite3.Connection, oids: list[int]) -> dict[int, StoredRecord]:
    out: dict[int, StoredRecord] = {}
    for start in range(0, len(oids), _LOAD_CHUNK):
        chunk = oids[start:start + _LOAD_CHUNK]
        marks = ",".join("?" * len(chunk))
        for oid, cid, tid, payload, stored_crc in conn.execute(
            f"SELECT oid, cid, tid, payload, crc FROM objects WHERE oid IN ({marks})",
            chunk,
        ):
            _check_crc(oid, payload, stored_crc)
            out[oid] = StoredRecord(oid=oid, cid=cid, tid=tid, payload=payload)
    return out


def _scan_type(conn: sqlite3.Connection, cid: int) -> Iterator[StoredRecord]:
    for oid, tid, payload, stored_crc in conn.execute(
        "SELECT oid, tid, payload, crc FROM objects WHERE cid=? ORDER BY oid",
        (cid,),
    ):
        _check_crc(oid, payload, stored_crc)
        yield StoredRecord(oid=oid, cid=cid, tid=tid, payload=payload)


def _load_blob(conn: sqlite3.Connection, oid: int) -> StoredBlob | None:
    row = conn.execute(
        "SELECT tid, size, hash, crc, data FROM blobs WHERE oid=?", (oid,)
    ).fetchone()
    if row is None:
        return None
    tid, size, h, stored_crc, data = cast("tuple[int, int, bytes, int, bytes]", row)
    if _crc(data) != stored_crc:  # torn-blob guard (ADR-007), CRC checked here
        raise CorruptRecordError(
            f"blob oid={oid} failed its checksum — the store file is damaged"
        )
    return StoredBlob(oid=oid, tid=tid, size=size, hash=h, data=data)


def _read_meta_and_types(conn: sqlite3.Connection) -> BootInfo:
    meta = cast("dict[str, str]", dict(conn.execute("SELECT key, value FROM meta")))
    types: list[tuple[int, str, list[str]]] = []
    for row in conn.execute("SELECT cid, name, fields FROM types ORDER BY cid"):
        cid, name, fields = cast("tuple[int, str, str | None]", row)
        types.append((cid, name, fields.split("\x1f") if fields else []))
    return BootInfo(meta=meta, types=types)


def _closed_underneath() -> StoreClosedError:
    return StoreClosedError(
        "this blob stream's store/snapshot read view was closed underneath it — "
        "a blob stream must not outlive the snapshot or store it came from"
    )


class _SqliteBlobReader(io.RawIOBase):
    """A raw reader over a ``sqlite3.Blob`` (ADR-007 §3 streamed read).

    ``readinto``/``seek``/``tell`` delegate to the open blob handle, so wrapped
    in an ``io.BufferedReader`` a ``read(n)``/range read pulls only the spanned
    bytes off disk — peak RSS is the buffer, never the blob size. ``seek`` past
    EOF is allowed (standard file semantics, matching the memory fallback's
    ``BytesIO``): the cursor parks at EOF and reads return ``b""``. Closing also
    runs ``on_close`` (the engine passes its dedicated read view's ``close``, so
    the pinned WAL read transaction is released exactly when the stream is).

    If the connection is closed out from under the stream (a stream that outlived
    its snapshot), every op translates the raw ``sqlite3.ProgrammingError`` to
    :class:`StoreClosedError` — the public surface stays DataCrystalError-only."""

    def __init__(self, blob: sqlite3.Blob, on_close: Callable[[], None] | None) -> None:
        self._blob = blob
        self._on_close = on_close
        self._len = len(blob)
        self._overshoot = 0  # bytes the logical cursor sits PAST EOF (seek-past-end)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:  # type: ignore[override]  # buffer protocol
        if self._overshoot:  # cursor parked past EOF → no bytes (file semantics)
            return 0
        try:
            chunk = self._blob.read(len(b))
        except sqlite3.ProgrammingError as exc:
            raise _closed_underneath() from exc
        n = len(chunk)
        b[:n] = chunk
        return n

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self.tell() + offset
        elif whence == io.SEEK_END:
            target = self._len + offset
        else:
            raise ValueError(f"invalid whence {whence!r}")
        if target < 0:
            raise ValueError("negative seek position")
        try:
            if target > self._len:  # park at EOF, remember the overshoot
                self._blob.seek(self._len)
                self._overshoot = target - self._len
            else:
                self._blob.seek(target)
                self._overshoot = 0
        except sqlite3.ProgrammingError as exc:
            raise _closed_underneath() from exc
        return target

    def tell(self) -> int:
        try:
            return self._blob.tell() + self._overshoot
        except sqlite3.ProgrammingError as exc:
            raise _closed_underneath() from exc

    def close(self) -> None:
        if self.closed:
            return
        try:
            try:
                self._blob.close()
            except sqlite3.ProgrammingError:
                pass  # connection already torn down — nothing left to release here
        finally:
            try:
                if self._on_close is not None:
                    self._on_close()
            finally:
                super().close()


def _open_blob_stream(
    conn: sqlite3.Connection, oid: int, on_close: Callable[[], None] | None
) -> BinaryIO:
    try:
        blob = conn.blobopen("blobs", "data", oid, readonly=True)
    except sqlite3.OperationalError as exc:  # no row with that rowid in `blobs`
        if on_close is not None:
            on_close()
        raise DanglingRefError(
            f"no blob for oid {oid} in the store — deleted (v0.x deletes are "
            "unchecked, ADR-003) or never committed; the blob reference you "
            "followed is stale"
        ) from exc
    except BaseException:  # any other failure: still release the dedicated view
        if on_close is not None:
            on_close()
        raise
    try:
        return io.BufferedReader(_SqliteBlobReader(blob, on_close))
    except BaseException:  # reader construction failed: don't leak the blob/view
        blob.close()
        if on_close is not None:
            on_close()
        raise


class SqliteReadView:
    """A pinned WAL read transaction over its own connection (ADR-002).

    WAL gives every connection snapshot isolation for the lifetime of a read
    transaction, so this view sees exactly one durable commit boundary no
    matter what commit P2 writes concurrently on the backend's connection.
    Close promptly: an open read transaction blocks WAL checkpoint truncation.
    """

    def __init__(self, path: Path) -> None:
        # check_same_thread=False: a snapshot may be handed to a thread pool;
        # CPython's sqlite3 is serialized, and this connection never writes
        # (query_only is enforced below, belt and braces over discipline).
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA query_only=ON")
        self._conn.execute("BEGIN")
        # The read transaction (and with it the snapshot boundary) starts at
        # the first read, not at BEGIN — pin it now.
        self._conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        self._closed = False

    def boot(self) -> BootInfo:
        return _read_meta_and_types(self._conn)

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        return _load_many(self._conn, oids)

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        return _scan_type(self._conn, cid)

    def load_blob(self, oid: int) -> StoredBlob | None:
        return _load_blob(self._conn, oid)

    def open_blob_stream(
        self, oid: int, on_close: Callable[[], None] | None = None
    ) -> BinaryIO:
        return _open_blob_stream(self._conn, oid, on_close)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._conn.execute("COMMIT")  # end the read transaction
        self._conn.close()


class SqliteBackend:
    def __init__(self, path: Path | str, *, durability: str = "interval") -> None:
        if durability not in ("commit", "interval", "never"):
            raise ValueError(
                f"durability must be 'commit', 'interval' or 'never', got {durability!r}"
            )
        self._path = Path(path)
        # check_same_thread=False: commit P2 applies batches from the store's
        # single IO worker thread while the owner thread keeps reading
        # (ADR-001 three-phase commit). CPython's sqlite3 is serialized
        # (threadsafety 3), so interleaved calls are safe; the engine never
        # issues concurrent *writes* (single IO worker + owner confinement).
        self._conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        if durability == "commit":
            self._conn.execute("PRAGMA synchronous=FULL")
            if sys.platform == "darwin":
                self._conn.execute("PRAGMA fullfsync=ON")
        elif durability == "interval":
            self._conn.execute("PRAGMA synchronous=NORMAL")
        else:
            self._conn.execute("PRAGMA synchronous=OFF")

    def boot(self) -> BootInfo:
        conn = self._conn
        # executescript() force-commits any open transaction, so the
        # (idempotent) DDL runs in autocommit mode, outside the version check.
        conn.executescript(_SCHEMA)
        self._drop_types_unique_constraint(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='format_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('format_version', ?)",
                    (str(FORMAT_VERSION),),
                )
                stored_version = FORMAT_VERSION
            else:
                stored_version = int(row[0])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if stored_version > FORMAT_VERSION:
            raise NewerStoreError(
                f"store format v{stored_version} is newer than this library "
                f"supports (v{FORMAT_VERSION}); upgrade datacrystal to open it"
            )
        meta = cast("dict[str, str]", dict(conn.execute("SELECT key, value FROM meta")))
        types: list[tuple[int, str, list[str]]] = []
        for row in conn.execute("SELECT cid, name, fields FROM types ORDER BY cid"):
            cid, name, fields = cast("tuple[int, str, str | None]", row)
            types.append((cid, name, fields.split("\x1f") if fields else []))
        return BootInfo(meta=meta, types=types)

    @staticmethod
    def _drop_types_unique_constraint(conn: sqlite3.Connection) -> None:
        """Stores created before additive schema evolution carry a UNIQUE
        constraint on types.name; the type lineage needs several rows per
        name. One-time, idempotent table rebuild."""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='types'"
        ).fetchone()
        if row is None or "UNIQUE" not in row[0]:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE types RENAME TO types_legacy")
            conn.execute(
                "CREATE TABLE types ("
                "cid INTEGER PRIMARY KEY, name TEXT NOT NULL, fields TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO types SELECT cid, name, fields FROM types_legacy")
            conn.execute("DROP TABLE types_legacy")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        return _load_many(self._conn, oids)

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        return _scan_type(self._conn, cid)

    def load_blob(self, oid: int) -> StoredBlob | None:
        return _load_blob(self._conn, oid)

    def read_view(self) -> SqliteReadView:
        """A snapshot-isolated read view (own connection, pinned WAL read
        transaction). Safe to call from any thread — it never touches the
        backend's shared connection (ADR-002)."""
        return SqliteReadView(self._path)

    def apply(self, batch: CommitBatch) -> None:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            if batch.new_types:
                conn.executemany(
                    "INSERT INTO types (cid, name, fields) VALUES (?, ?, ?)",
                    [(cid, name, "\x1f".join(fields)) for cid, name, fields in batch.new_types],
                )
            conn.executemany(
                "INSERT OR REPLACE INTO objects (oid, cid, tid, payload, crc) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (r.oid, r.cid, r.tid, r.payload, _crc(r.payload))
                    for r in batch.records
                ],
            )
            if batch.blobs:  # out-of-line bytes (ADR-007), same atomic txn
                conn.executemany(
                    "INSERT OR REPLACE INTO blobs (oid, tid, size, hash, crc, data) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (b.oid, b.tid, b.size, b.hash, _crc(b.data), b.data)
                        for b in batch.blobs
                    ],
                )
            for sb in batch.blob_streams:  # streamed fill (ADR-007 §4), same txn
                # Allocate the cell, then fill it in place — the bytes are never
                # whole in RAM. crc is accumulated over the chunks and patched in
                # once the fill is verified complete (all inside this txn → still
                # atomic; a torn fill rolls back with the record).
                conn.execute(
                    "INSERT OR REPLACE INTO blobs (oid, tid, size, hash, crc, data) "
                    "VALUES (?, ?, ?, ?, 0, zeroblob(?))",
                    (sb.oid, sb.tid, sb.size, sb.hash, sb.size),
                )
                written = 0
                running_crc = 0
                hasher = hashlib.sha256()
                with conn.blobopen("blobs", "data", sb.oid) as cell:
                    for chunk in sb.open_chunks():
                        if written + len(chunk) > sb.size:
                            raise ValueError(
                                f"streamed blob oid={sb.oid} produced more than "
                                f"its declared size {sb.size} bytes"
                            )
                        cell.write(chunk)
                        running_crc = zlib.crc32(chunk, running_crc)
                        hasher.update(chunk)
                        written += len(chunk)
                if written != sb.size:
                    raise ValueError(
                        f"streamed blob oid={sb.oid} produced {written} bytes "
                        f"but declared size {sb.size} (a sized streamed write "
                        "must fill the whole cell)"
                    )
                if hasher.digest() != sb.hash:
                    # The fill pass disagreed with the pre-TID hashing pass: a
                    # non-deterministic source (or a file edited mid-commit). The
                    # descriptor hash would lie about the stored bytes — reject,
                    # rolling the whole commit back gaplessly (invariant 5).
                    raise ValueError(
                        f"streamed blob oid={sb.oid} bytes changed between the "
                        "hashing pass and the fill pass (the source is "
                        "non-deterministic) — refusing to store a descriptor "
                        "hash that would not match the bytes"
                    )
                conn.execute(
                    "UPDATE blobs SET crc=? WHERE oid=?",
                    (running_crc & 0xFFFFFFFF, sb.oid),
                )
            if batch.deletes:  # physical removal (ADR-003) — no tombstone rows
                conn.executemany(
                    "DELETE FROM objects WHERE oid=?",
                    [(oid,) for oid in batch.deletes],
                )
            conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                list(batch.meta.items()),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._conn.close()
