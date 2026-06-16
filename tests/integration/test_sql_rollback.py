"""SQL-layer ROLLBACK of a partially-executed multi-statement batch (#115).

``tests/unit/test_three_phase.py`` proves the *engine* compensates a failed
P2 (re-buffers the captured entities, reuses the burned TID — invariant 5),
but it injects the fault in a backend *wrapper*, so SQLite's own connection
never runs a single statement of the batch. This test closes that gap: it
faults ``SqliteBackend.apply`` *after* the ``objects``/``blobs`` inserts but
*before* ``COMMIT`` (at the deletes/meta step), then reopens the file and
proves the whole batch rolled back at the SQL layer — none of its rows
landed and the watermark is the pre-batch one. Atomicity is then *proven by
SQLite*, not merely asserted by construction (``sqlite.py:389-472`` wraps the
batch in one ``BEGIN IMMEDIATE … COMMIT`` with ``except: ROLLBACK; raise``).

sqlite-only by design: the memory backend has no SQL transaction to roll back.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._ids import TID_BASE
from datacrystal._records import crc as _crc
from datacrystal._state import STATE_NEW
from datacrystal._storage.protocol import CommitBatch
from datacrystal._storage.sqlite import SqliteBackend


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    photo: Annotated[bytes | None, dc.Blob] = None


def _row_counts(db: Path) -> tuple[int, int, int]:
    """(objects, blobs, watermark) read straight from the file, bypassing
    the engine — the on-disk truth after a reopen."""
    conn = sqlite3.connect(db)
    try:
        objects = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        blobs = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
        row = conn.execute(
            "SELECT value FROM meta WHERE key='next_tid'"
        ).fetchone()
        next_tid = int(row[0]) if row is not None else TID_BASE
    finally:
        conn.close()
    return objects, blobs, next_tid - 1  # watermark = next_tid - 1


def _fault_apply_after_inserts(backend: SqliteBackend):
    """Patch ``apply`` to run the real ``objects``/``blobs`` INSERTs inside the
    ``BEGIN IMMEDIATE`` txn, then raise *before* ``COMMIT`` (at the deletes/meta
    step). Mirrors the production statements so the fault exercises a genuinely
    half-written batch; the ``except: ROLLBACK; raise`` is the production one."""

    def faulting_apply(batch: CommitBatch) -> None:
        conn = backend._conn  # pyright: ignore[reportPrivateUsage]
        conn.execute("BEGIN IMMEDIATE")
        try:
            objects_before = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            blobs_before = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
            if batch.new_types:
                conn.executemany(
                    "INSERT INTO types (cid, name, fields) VALUES (?, ?, ?)",
                    [
                        (cid, name, "\x1f".join(fields))
                        for cid, name, fields in batch.new_types
                    ],
                )
            conn.executemany(
                "INSERT OR REPLACE INTO objects (oid, cid, tid, payload, crc) "
                "VALUES (?, ?, ?, ?, ?)",
                [(r.oid, r.cid, r.tid, r.payload, _crc(r.payload)) for r in batch.records],
            )
            assert batch.blobs, "the staged batch must carry an out-of-line blob"
            conn.executemany(
                "INSERT OR REPLACE INTO blobs (oid, tid, size, hash, crc, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (b.oid, b.tid, b.size, b.hash, _crc(b.data), b.data)
                    for b in batch.blobs
                ],
            )
            # The rows are now live *inside* the txn — prove SQLite holds both
            # the records and the blob before we tear the batch in half.
            assert (
                conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
                == objects_before + len(batch.records)
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
                == blobs_before + len(batch.blobs)
            )
            # Fault at the deletes/meta step, before COMMIT: a torn batch.
            raise OSError("injected fault mid-apply (after inserts, before COMMIT)")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    backend.apply = faulting_apply  # type: ignore[method-assign]


def test_sql_rollback_undoes_a_torn_batch_and_keeps_the_tid_gapless(tmp_path):
    db = tmp_path / "data.sqlite"

    # A baseline commit so the rolled-back batch has a real predecessor
    # watermark to fall back to (not just the empty store).
    backend = SqliteBackend(db, durability="commit")
    store = dc.Store._from_backend(backend)
    store.store(Mineral(qid="Q1", name="quartz"))
    baseline_tid = store.commit()
    assert baseline_tid == TID_BASE
    store.close()

    base_objects, base_blobs, base_watermark = _row_counts(db)
    assert base_objects == 1 and base_blobs == 0
    assert base_watermark == baseline_tid

    # Reopen; stage a multi-statement batch (two records + a blob), then fault
    # apply after the inserts but before COMMIT.
    backend = SqliteBackend(db, durability="commit")
    store = dc.Store._from_backend(backend)
    azurite = Mineral(qid="Q2", name="azurite", photo=b"\x89PNG fake bytes")
    beryl = Mineral(qid="Q3", name="beryl")
    store.store(azurite)
    store.store(beryl)
    _fault_apply_after_inserts(backend)
    with pytest.raises(OSError, match="injected fault mid-apply"):
        store.commit()

    # Engine compensation (mirrors test_three_phase): never durable, so the
    # captured NEW entities are re-buffered and the TID was *not* consumed.
    assert object.__getattribute__(azurite, "__dc_state__") == STATE_NEW
    assert object.__getattribute__(beryl, "__dc_state__") == STATE_NEW
    assert store.last_tid == baseline_tid
    store.close()

    # SQL-layer truth, read straight from the file: the torn batch left NO
    # rows behind and the watermark is still the pre-batch one — SQLite rolled
    # the half-written txn back, not the engine.
    objects_now, blobs_now, watermark_now = _row_counts(db)
    assert objects_now == base_objects  # the two staged records did not land
    assert blobs_now == base_blobs  # nor the staged blob
    assert watermark_now == base_watermark

    # Gapless: a fresh open commits the same batch successfully and reuses the
    # burned TID (invariant 5 — replay determinism is a public contract).
    backend = SqliteBackend(db, durability="commit")
    store = dc.Store._from_backend(backend)
    assert store.get(Mineral, qid="Q2") is None  # confirms the rollback via the engine
    assert store.get(Mineral, qid="Q3") is None
    store.store(Mineral(qid="Q2", name="azurite", photo=b"\x89PNG fake bytes"))
    store.store(Mineral(qid="Q3", name="beryl"))
    retry_tid = store.commit()
    assert retry_tid == baseline_tid + 1  # the very TID the torn batch burned

    survivor = store.get(Mineral, qid="Q2")
    assert survivor is not None and survivor.name == "azurite"
    handle = store.get(Mineral, qid="Q2").photo  # type: ignore[union-attr]
    assert handle is not None and handle.bytes() == b"\x89PNG fake bytes"
    assert store.get(Mineral, qid="Q3") is not None
    store.close()
