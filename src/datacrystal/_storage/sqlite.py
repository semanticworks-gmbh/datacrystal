"""SQLite-as-blob-store: the durable v0.x backend (ROADMAP item 2).

datacrystal writes object records as opaque msgpack blobs into a single
SQLite file and rides SQLite's journal for crash atomicity — we add no
custom on-disk format in v0.x, which is exactly why the crash-consistency
story is honest from day one (KICKOFF risk 4). The boot index *is* the
B-tree (ROADMAP punt 14: no boot problem to solve).

Durability:
* ``durability="full"`` (default) — ``synchronous=FULL`` plus
  ``fullfsync=ON`` on macOS (the ~4 ms F_FULLFSYNC floor measured in the
  feasibility study; honesty over speed).
* ``durability="relaxed"`` — ``synchronous=NORMAL`` under WAL: commits may
  be lost on OS crash/power loss, never corrupted.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Iterator

from datacrystal._errors import CorruptRecordError, NewerStoreError
from datacrystal._ids import FORMAT_VERSION
from datacrystal._records import crc as _crc
from datacrystal._storage.protocol import BootInfo, CommitBatch, StoredRecord

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
"""

_LOAD_CHUNK = 500


class SqliteBackend:
    def __init__(self, path: Path | str, *, durability: str = "full") -> None:
        if durability not in ("full", "relaxed"):
            raise ValueError(f"durability must be 'full' or 'relaxed', got {durability!r}")
        self._path = Path(path)
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        if durability == "full":
            self._conn.execute("PRAGMA synchronous=FULL")
            if sys.platform == "darwin":
                self._conn.execute("PRAGMA fullfsync=ON")
        else:
            self._conn.execute("PRAGMA synchronous=NORMAL")

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
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        types = [
            (cid, name, fields.split("\x1f") if fields else [])
            for cid, name, fields in conn.execute(
                "SELECT cid, name, fields FROM types ORDER BY cid"
            )
        ]
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
        out: dict[int, StoredRecord] = {}
        conn = self._conn
        for start in range(0, len(oids), _LOAD_CHUNK):
            chunk = oids[start:start + _LOAD_CHUNK]
            marks = ",".join("?" * len(chunk))
            for oid, cid, tid, payload, stored_crc in conn.execute(
                f"SELECT oid, cid, tid, payload, crc FROM objects WHERE oid IN ({marks})",
                chunk,
            ):
                self._check_crc(oid, payload, stored_crc)
                out[oid] = StoredRecord(oid=oid, cid=cid, tid=tid, payload=payload)
        return out

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        for oid, tid, payload, stored_crc in self._conn.execute(
            "SELECT oid, tid, payload, crc FROM objects WHERE cid=? ORDER BY oid",
            (cid,),
        ):
            self._check_crc(oid, payload, stored_crc)
            yield StoredRecord(oid=oid, cid=cid, tid=tid, payload=payload)

    @staticmethod
    def _check_crc(oid: int, payload: bytes, stored_crc: int) -> None:
        if _crc(payload) != stored_crc:
            raise CorruptRecordError(
                f"record oid={oid} failed its checksum — the store file is damaged"
            )

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
