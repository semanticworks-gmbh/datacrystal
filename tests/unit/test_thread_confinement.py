"""ADR-001 owner confinement: foreign threads fail loudly, mutations never land."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

import datacrystal as dc
from tests.conftest import Mineral

StoreFactory = Callable[[], dc.Store]


@pytest.fixture
def foreign_thread() -> Iterator[ThreadPoolExecutor]:
    """Yield an executor whose one thread is never the store's owner."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool


def test_foreign_thread_store_access_raises_wrong_thread(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a store owned by the test thread
    When another thread reads its root
    Then the read raises WrongThreadError
    And the message carries the snapshot recipe
    """
    read_root = foreign_thread.submit(getattr, store, "root")
    with pytest.raises(dc.WrongThreadError, match="snapshot"):
        read_root.result()


def test_foreign_thread_query_raises_wrong_thread(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a store holding a committed cubic mineral
    When another thread queries it
    Then the query raises WrongThreadError
    """
    store.root = [Mineral(qid="Q1", name="quartz", crystal_system="cubic")]
    store.commit()
    query = foreign_thread.submit(store.query, Mineral)
    with pytest.raises(dc.WrongThreadError):
        query.result()


def test_foreign_thread_write_raises_before_mutating(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a committed quartz named "quartz"
    When another thread renames it
    Then the rename raises WrongThreadError
    And the quartz is still named "quartz"
    """
    quartz = Mineral(qid="Q1", name="quartz")
    store.root = [quartz]
    store.commit()
    rename = foreign_thread.submit(setattr, quartz, "name", "changed")
    with pytest.raises(dc.WrongThreadError):
        rename.result()
    assert quartz.name == "quartz"


def test_foreign_thread_write_buffers_nothing(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a committed quartz and a rename refused on another thread
    When the owner commits
    Then there is nothing to commit
    """
    quartz = Mineral(qid="Q1", name="quartz")
    store.root = [quartz]
    store.commit()
    foreign_thread.submit(setattr, quartz, "name", "changed").exception()
    committed = store.commit()
    assert committed is None


def test_closed_store_raises_store_closed(store_factory: StoreFactory) -> None:
    """Given a store that has been closed
    When its root is read
    Then the read raises StoreClosedError
    """
    store = store_factory()
    store.close()
    with pytest.raises(dc.StoreClosedError):
        _ = store.root
