"""A snapshot's blob streams are any-thread-safe (ADR-007 §3, ADR-001).

The store is owner-confined, but a snapshot is the cross-thread read handle.
``Snapshot.open_blob`` rides the snapshot's pinned read view, so a worker thread
may open and read a blob that the owning thread committed.
The helpers here only drive a thread; every assert sits in the test body.
The domain is the mineral cabinet: a specimen carries its scanned label.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, BinaryIO

import pytest

import datacrystal as dc

StoreFactory = Callable[[], dc.Store]

AZURITE_SCAN = b"%PDF-1.7 azurite label " * 64
MALACHITE_SCAN = b"%PDF-1.7 malachite label " * 64


@dc.entity
class Specimen:
    """A specimen whose label is scanned and kept as a streamed blob."""

    qid: Annotated[str, dc.Unique]
    scan: Annotated[bytes, dc.Blob] = b""


@pytest.fixture
def worker() -> Iterator[ThreadPoolExecutor]:
    """Yield an executor whose one thread is never the store's owner."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool


@pytest.fixture
def two_workers() -> Iterator[ThreadPoolExecutor]:
    """Yield an executor with two threads, neither the store's owner."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        yield pool


def _commit_specimens(store_factory: StoreFactory) -> tuple[int, int]:
    """Commit an azurite and a malachite specimen, close the store, return their OIDs."""
    store = store_factory()
    azurite = Specimen(qid="Q1", scan=AZURITE_SCAN)
    malachite = Specimen(qid="Q2", scan=MALACHITE_SCAN)
    oids = (store.store(azurite), store.store(malachite))
    store.commit()
    store.close()
    return oids


def _open_and_read(snap: dc.Snapshot, oid: int) -> bytes:
    """Open the scan of ``oid`` on the calling thread and read it whole."""
    with snap.open_blob(oid, "scan") as fh:
        return fh.read()


def _read_open_stream(fh: BinaryIO) -> bytes:
    """Read an already opened stream whole on the calling thread."""
    return fh.read()


def test_open_blob_opened_and_read_on_another_thread_returns_the_bytes(
    store_factory: StoreFactory, worker: ThreadPoolExecutor
) -> None:
    """Given a committed azurite scan and a snapshot taken on the owner thread
    When a worker thread opens the scan through the snapshot and reads it
    Then the worker reads the scan bytes
    """
    azurite_oid, _ = _commit_specimens(store_factory)
    with store_factory() as store, store.snapshot() as snap:
        scan = worker.submit(_open_and_read, snap, azurite_oid).result(timeout=30)
    assert scan == AZURITE_SCAN


def test_open_blob_opened_on_the_owner_can_be_read_on_another_thread(
    store_factory: StoreFactory, worker: ThreadPoolExecutor
) -> None:
    """Given a committed azurite scan and a stream the owner thread opened on a snapshot
    When a worker thread reads that stream
    Then the worker reads the scan bytes
    """
    azurite_oid, _ = _commit_specimens(store_factory)
    with (
        store_factory() as store,
        store.snapshot() as snap,
        snap.open_blob(azurite_oid, "scan") as fh,
    ):
        scan = worker.submit(_read_open_stream, fh).result(timeout=30)
    assert scan == AZURITE_SCAN


def test_two_threads_read_two_scans_through_one_snapshot(
    store_factory: StoreFactory, two_workers: ThreadPoolExecutor
) -> None:
    """Given committed azurite and malachite scans and one snapshot
    When two worker threads each open a different scan through it
    Then each worker reads its own specimen's bytes
    """
    azurite_oid, malachite_oid = _commit_specimens(store_factory)
    with store_factory() as store, store.snapshot() as snap:
        azurite_read = two_workers.submit(_open_and_read, snap, azurite_oid)
        malachite_read = two_workers.submit(_open_and_read, snap, malachite_oid)
        scans = (azurite_read.result(timeout=30), malachite_read.result(timeout=30))
    assert scans == (AZURITE_SCAN, MALACHITE_SCAN)


def test_open_blob_on_another_thread_reads_the_scan_version_the_snapshot_pinned(
    store_factory: StoreFactory, worker: ThreadPoolExecutor
) -> None:
    """Given a snapshot of a committed azurite scan and a later commit that replaces the scan
    When a worker thread opens the scan through the older snapshot
    Then the worker reads the bytes of the scan version the snapshot pinned
    """
    azurite_oid, _ = _commit_specimens(store_factory)
    with store_factory() as store, store.snapshot() as snap:
        store.get_many([azurite_oid])[0].scan = b"replaced"  # pyrefly: ignore[missing-attribute]
        store.commit()
        scan = worker.submit(_open_and_read, snap, azurite_oid).result(timeout=30)
    assert scan == AZURITE_SCAN
