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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._records import crc as _crc
from datacrystal._storage.protocol import CommitBatch
from datacrystal._storage.sqlite import SqliteBackend


@dc.entity
class Mineral:
    """A mineral with an optional out-of-line photo."""

    qid: Annotated[str, dc.Unique]
    name: str
    photo: Annotated[bytes | None, dc.Blob] = None


FIRST_TID = 1
PHOTO = b"\x89PNG fake bytes"


@dataclass
class InTxnRows:
    """How many rows the faulted apply saw live inside its transaction, before the fault."""

    objects_added: int = 0
    blobs_added: int = 0


@dataclass
class StagedBatch:
    """A store with two new minerals staged and ``apply`` faulted before ``COMMIT``."""

    db: Path
    store: dc.Store
    azurite: Mineral
    beryl: Mineral
    seen: InTxnRows


def _count_rows(db: Path, table: str) -> int:
    """Count a table's rows straight from the file, bypassing the engine."""
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _watermark(db: Path) -> int:
    """Read the committed watermark (next_tid - 1) straight from the file."""
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='next_tid'").fetchone()
    finally:
        conn.close()
    next_tid = int(row[0]) if row is not None else FIRST_TID
    return next_tid - 1


def _fault_apply_after_inserts(backend: SqliteBackend, seen: InTxnRows) -> None:
    """Patch ``apply``: the first call runs the real INSERTs in ``BEGIN IMMEDIATE``, then raises.

    The fault comes before ``COMMIT`` (at the deletes/meta step). It mirrors the production
    statements so the fault exercises a genuinely half-written batch; the
    ``except: ROLLBACK; raise`` is the production one. What it saw inside the transaction goes
    to ``seen``.
    """
    real_apply = backend.apply
    faulted = False

    def faulting_apply(batch: CommitBatch) -> None:
        nonlocal faulted
        if faulted:
            real_apply(batch)
            return
        faulted = True
        conn = backend._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            objects_before = conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            blobs_before = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
            if batch.new_types:
                conn.executemany(
                    "INSERT INTO types (cid, name, fields) VALUES (?, ?, ?)",
                    [(cid, name, "\x1f".join(fields)) for cid, name, fields in batch.new_types],
                )
            conn.executemany(
                "INSERT OR REPLACE INTO objects (oid, cid, tid, payload, crc) "
                "VALUES (?, ?, ?, ?, ?)",
                [(r.oid, r.cid, r.tid, r.payload, _crc(r.payload)) for r in batch.records],
            )
            conn.executemany(
                "INSERT OR REPLACE INTO blobs (oid, tid, size, hash, crc, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(b.oid, b.tid, b.size, b.hash, _crc(b.data), b.data) for b in batch.blobs],
            )
            seen.objects_added = (
                conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0] - objects_before
            )
            seen.blobs_added = (
                conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] - blobs_before
            )
            # Fault at the deletes/meta step, before COMMIT: a torn batch.
            raise OSError("injected fault mid-apply (after inserts, before COMMIT)")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    backend.apply = faulting_apply


@pytest.fixture
def baseline_db(tmp_path: Path) -> Path:
    """Return a database file holding one committed mineral at the first TID.

    The baseline commit gives the rolled-back batch a real predecessor watermark to fall back
    to, not just the empty store.
    """
    db = tmp_path / "data.sqlite"
    store = dc.Store._from_backend(SqliteBackend(db, durability="commit"))
    store.store(Mineral(qid="Q1", name="quartz"))
    store.commit()
    store.close()
    return db


@pytest.fixture
def staged_batch(baseline_db: Path) -> Iterator[StagedBatch]:
    """Yield a reopened store with two minerals (one with a photo) staged and apply faulted."""
    backend = SqliteBackend(baseline_db, durability="commit")
    store = dc.Store._from_backend(backend)
    azurite = Mineral(qid="Q2", name="azurite", photo=PHOTO)
    beryl = Mineral(qid="Q3", name="beryl")
    store.store(azurite)
    store.store(beryl)
    seen = InTxnRows()
    _fault_apply_after_inserts(backend, seen)
    yield StagedBatch(baseline_db, store, azurite, beryl, seen)
    store.close()


@pytest.fixture
def rolled_back_db(staged_batch: StagedBatch) -> Path:
    """Return the database file after the staged batch faulted and the store closed."""
    with pytest.raises(OSError, match="injected fault mid-apply"):
        staged_batch.store.commit()
    staged_batch.store.close()
    return staged_batch.db


@pytest.fixture
def reopened_store(rolled_back_db: Path) -> Iterator[dc.Store]:
    """Yield a fresh store over the database whose torn batch was rolled back."""
    store = dc.Store._from_backend(SqliteBackend(rolled_back_db, durability="commit"))
    yield store
    store.close()


# --- the torn commit, seen through the engine ---------------------------------


def test_baseline_commit_takes_the_first_tid(baseline_db: Path) -> None:
    """Given a store with one mineral committed
    When the file is read back
    Then its watermark is the first TID
    """
    assert _watermark(baseline_db) == FIRST_TID


def test_faulted_apply_had_both_records_live_inside_its_transaction(
    staged_batch: StagedBatch,
) -> None:
    """Given two staged minerals and an apply faulted after its inserts
    When the store commits
    Then SQLite held both records inside the transaction before the fault
    """
    with pytest.raises(OSError, match="injected fault mid-apply"):
        staged_batch.store.commit()

    assert staged_batch.seen.objects_added == 2


def test_faulted_apply_had_the_blob_live_inside_its_transaction(
    staged_batch: StagedBatch,
) -> None:
    """Given two staged minerals, one with a photo, and an apply faulted after its inserts
    When the store commits
    Then SQLite held the blob inside the transaction before the fault
    """
    with pytest.raises(OSError, match="injected fault mid-apply"):
        staged_batch.store.commit()

    assert staged_batch.seen.blobs_added == 1


def test_retry_on_the_same_store_takes_the_tid_the_failed_commit_burned(
    staged_batch: StagedBatch,
) -> None:
    """Given two staged minerals and an apply that faulted once before COMMIT
    When the same store commits again
    Then the retry takes the TID right after the baseline one, as both minerals are buffered again
    """
    with pytest.raises(OSError, match="injected fault mid-apply"):
        staged_batch.store.commit()

    retry_tid = staged_batch.store.commit()

    assert retry_tid == FIRST_TID + 1


def test_retry_on_the_same_store_persists_both_minerals(staged_batch: StagedBatch) -> None:
    """Given two staged minerals and an apply that faulted once before COMMIT
    When the same store commits again
    Then the file holds the baseline record and both retried ones
    """
    with pytest.raises(OSError, match="injected fault mid-apply"):
        staged_batch.store.commit()

    staged_batch.store.commit()

    assert _count_rows(staged_batch.db, "objects") == 3


def test_failed_commit_does_not_consume_the_tid(staged_batch: StagedBatch) -> None:
    """Given two staged minerals and an apply faulted before COMMIT
    When the store commits
    Then the store's last TID is still the baseline one
    """
    with pytest.raises(OSError, match="injected fault mid-apply"):
        staged_batch.store.commit()

    assert staged_batch.store.last_tid == FIRST_TID


# --- the SQL-layer truth, read straight from the file --------------------------


def test_rollback_leaves_no_staged_record_in_the_file(rolled_back_db: Path) -> None:
    """Given a batch of two records and a blob torn before COMMIT
    When the file is read back after the store closed
    Then only the baseline record is there, as SQLite rolled the staged ones back
    """
    assert _count_rows(rolled_back_db, "objects") == 1


def test_rollback_leaves_no_staged_blob_in_the_file(rolled_back_db: Path) -> None:
    """Given a batch of two records and a blob torn before COMMIT
    When the file is read back after the store closed
    Then no blob is there
    """
    assert _count_rows(rolled_back_db, "blobs") == 0


def test_rollback_keeps_the_pre_batch_watermark_in_the_file(rolled_back_db: Path) -> None:
    """Given a batch of two records and a blob torn before COMMIT
    When the file is read back after the store closed
    Then the watermark is still the baseline one
    """
    assert _watermark(rolled_back_db) == FIRST_TID


# --- gapless: a fresh open commits the same batch and reuses the burned TID ----


@pytest.mark.parametrize("qid", ["Q2", "Q3"])
def test_rolled_back_mineral_is_absent_through_the_engine(
    reopened_store: dc.Store, qid: str
) -> None:
    """Given a database whose torn batch of Q2 and Q3 was rolled back
    When a fresh store looks a staged mineral up by its qid
    Then there is none
    """
    found = reopened_store.get(Mineral, qid=qid)

    assert found is None


def test_retry_reuses_the_tid_the_torn_batch_burned(reopened_store: dc.Store) -> None:
    """Given a database whose torn batch was rolled back
    When a fresh store stores the same two minerals and commits
    Then the commit takes the TID right after the baseline one (invariant 5)
    """
    reopened_store.store(Mineral(qid="Q2", name="azurite", photo=PHOTO))
    reopened_store.store(Mineral(qid="Q3", name="beryl"))

    retry_tid = reopened_store.commit()

    assert retry_tid == FIRST_TID + 1


def test_retried_batch_keeps_the_azurite(reopened_store: dc.Store) -> None:
    """Given a database whose torn batch was rolled back
    When a fresh store stores the same two minerals and commits
    Then the azurite can be fetched by its qid
    """
    reopened_store.store(Mineral(qid="Q2", name="azurite", photo=PHOTO))
    reopened_store.store(Mineral(qid="Q3", name="beryl"))
    reopened_store.commit()

    survivor = reopened_store.get(Mineral, qid="Q2")

    assert survivor is not None
    assert survivor.name == "azurite"


def test_retried_batch_keeps_the_azurite_photo_bytes(reopened_store: dc.Store) -> None:
    """Given a database whose torn batch was rolled back
    When a fresh store stores the same two minerals and commits
    Then the azurite's photo reads back byte for byte
    """
    reopened_store.store(Mineral(qid="Q2", name="azurite", photo=PHOTO))
    reopened_store.store(Mineral(qid="Q3", name="beryl"))
    reopened_store.commit()

    survivor = reopened_store.get(Mineral, qid="Q2")

    assert survivor is not None
    assert survivor.photo is not None
    assert survivor.photo.bytes() == PHOTO


def test_retried_batch_keeps_the_beryl(reopened_store: dc.Store) -> None:
    """Given a database whose torn batch was rolled back
    When a fresh store stores the same two minerals and commits
    Then the beryl can be fetched by its qid
    """
    reopened_store.store(Mineral(qid="Q2", name="azurite", photo=PHOTO))
    reopened_store.store(Mineral(qid="Q3", name="beryl"))
    reopened_store.commit()

    survivor = reopened_store.get(Mineral, qid="Q3")

    assert survivor is not None
