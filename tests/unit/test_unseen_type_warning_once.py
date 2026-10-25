"""#201 — UnseenTypeWarning fires once per type per Store, snapshots included, then stays quiet.

The Store and every snapshot it opens share one record of the types already warned about,
so a web route that reads an unseen type per request warns once, not once per watermark.
Also pins that a bytearray field hydrates as bytes (#201).
"""

from __future__ import annotations

import threading
import warnings
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, cast

import pytest

import datacrystal as dc
from tests.conftest import Locality, Mineral

StoreFactory = Callable[[], dc.Store]


@dc.entity
class Sample:
    """A field sample whose raw reading is supplied as a bytearray."""

    qid: Annotated[str, dc.Unique]
    raw: bytes = b""


def test_the_first_snapshot_read_of_an_unseen_type_warns(store: dc.Store) -> None:
    """Given a store with no committed minerals
    When a snapshot counts minerals
    Then UnseenTypeWarning says there are no committed records
    """
    with store.snapshot() as snap, pytest.warns(dc.UnseenTypeWarning, match="no committed records"):
        snap.count(Mineral)


def test_a_second_snapshot_of_the_same_store_stays_quiet_on_the_same_type(
    store: dc.Store,
) -> None:
    """Given a store whose first snapshot already warned about minerals
    When a new snapshot of the same store counts minerals
    Then no warning is raised
    """
    with store.snapshot() as first, pytest.warns(dc.UnseenTypeWarning):
        first.count(Mineral)

    with store.snapshot() as second, warnings.catch_warnings():
        warnings.simplefilter("error")
        counted = second.count(Mineral)

    assert counted == 0


def test_a_store_read_after_a_snapshot_warning_stays_quiet(store: dc.Store) -> None:
    """Given a store whose snapshot already warned about minerals
    When the store itself counts minerals
    Then no warning is raised
    """
    with store.snapshot() as snap, pytest.warns(dc.UnseenTypeWarning):
        snap.count(Mineral)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        counted = store.count(Mineral)

    assert counted == 0


def test_a_snapshot_read_after_a_store_warning_stays_quiet(store: dc.Store) -> None:
    """Given a store that already warned about minerals itself
    When a snapshot of that store counts minerals
    Then no warning is raised
    """
    with pytest.warns(dc.UnseenTypeWarning):
        store.count(Mineral)

    with store.snapshot() as snap, warnings.catch_warnings():
        warnings.simplefilter("error")
        counted = snap.count(Mineral)

    assert counted == 0


def test_a_different_unseen_type_still_warns(store: dc.Store) -> None:
    """Given a store that already warned about minerals
    When a snapshot counts localities, which it never saw either
    Then UnseenTypeWarning names Locality
    """
    with store.snapshot() as snap, pytest.warns(dc.UnseenTypeWarning):
        snap.count(Mineral)

    with store.snapshot() as again, pytest.warns(dc.UnseenTypeWarning, match="Locality"):
        again.count(Locality)


def test_a_second_store_over_the_same_files_warns_again_once(
    store_factory: StoreFactory,
) -> None:
    """Given a store that warned about minerals and was closed
    When the files are reopened as a second store that counts minerals twice
    Then the first count warns and the second stays quiet
    """
    first = store_factory()
    with pytest.warns(dc.UnseenTypeWarning):
        first.count(Mineral)
    first.close()
    second = store_factory()

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        second.count(Mineral)
        second.count(Mineral)

    assert [w.category for w in recorded] == [dc.UnseenTypeWarning]
    second.close()


def test_a_type_with_records_does_not_warn_in_any_snapshot(store: dc.Store) -> None:
    """Given a store holding a committed mineral
    When a snapshot counts minerals
    Then no warning is raised
    """
    store.root = [Mineral(qid="Q1", name="Quartz")]
    store.commit()

    with store.snapshot() as snap, warnings.catch_warnings():
        warnings.simplefilter("error")
        counted = snap.count(Mineral)

    assert counted == 1


def _count_minerals_from_sixteen_threads(store: dc.Store) -> list[int]:
    """Concurrency harness: sixteen threads each count minerals in their own snapshot at once."""
    start = threading.Barrier(16)

    def count_in_own_snapshot() -> int:
        start.wait()
        with store.snapshot() as snap:
            return snap.count(Mineral)

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(count_in_own_snapshot) for _ in range(16)]
        return [future.result() for future in futures]


def test_snapshots_read_from_sixteen_threads_warn_exactly_once(store: dc.Store) -> None:
    """Given a store with no minerals and sixteen threads, each opening its own snapshot
    When all of them count minerals at the same moment
    Then exactly one UnseenTypeWarning is raised
    The check-and-add is too short for the GIL to split, so this pins the behaviour, not the lock.
    """
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        counts = _count_minerals_from_sixteen_threads(store)

    assert [w.category for w in recorded] == [dc.UnseenTypeWarning]
    assert counts == [0] * 16


def test_a_bytearray_field_hydrates_as_bytes_after_reopen(store_factory: StoreFactory) -> None:
    """Given a sample whose bytes-typed field was assigned a bytearray and committed
    When the store is reopened and the sample fetched
    Then the field is bytes, not a bytearray
    """
    store = store_factory()
    store.root = [Sample(qid="S1", raw=cast("bytes", bytearray(b"azurite")))]
    store.commit()
    store.close()
    reopened = store_factory()

    fetched = reopened.get(Sample, qid="S1")

    assert fetched is not None
    assert type(fetched.raw) is bytes
    reopened.close()
