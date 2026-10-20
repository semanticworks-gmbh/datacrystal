"""store.submit() — the sanctioned cross-thread write path (ADR-001).

Foreign threads ship closures; the owner runs them at its next store call
(piggyback) or via run_pending(). Futures resolve with plain data only —
a live entity in the result fails with EntityEscapeError, whose message
embeds the recipe (KICKOFF M2 exit criterion 3).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import NoReturn

import pytest

import datacrystal as dc
from tests.conftest import Mineral

StoreFactory = Callable[[], dc.Store]


@pytest.fixture
def foreign_thread() -> Iterator[ThreadPoolExecutor]:
    """Yield an executor whose one thread is never the store's owner."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool


def _store_quartz(store: dc.Store) -> None:
    store.store(Mineral(qid="Q1", name="quartz", crystal_system="trigonal"))
    store.commit()


def _record(seen: list[int], value: int) -> int:
    seen.append(value)
    return value


def _explode() -> NoReturn:
    raise ValueError("boom")


def _the_entity(store: dc.Store) -> object:
    return store.query(Mineral)[0]


def _entity_nested_in_containers(store: dc.Store) -> list[object]:
    return [1, [store.query(Mineral)[0]]]


def _entity_as_dict_value(store: dc.Store) -> dict[str, object]:
    return {"hit": store.query(Mineral)[0]}


def _loaded_lazy_handle(store: dc.Store) -> object:
    return dc.Lazy.of(store.query(Mineral)[0])


def _plain_fields(store: dc.Store) -> dict[str, str]:
    mineral = store.query(Mineral)[0]
    return {"qid": mineral.qid, "name": mineral.name}


def _ship(foreign_thread: ThreadPoolExecutor, store: dc.Store, fn: Callable[[], object]) -> Future:
    return foreign_thread.submit(store.submit, fn).result()


def test_foreign_submission_stays_queued_until_the_owner_calls_in(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a store write shipped from a foreign thread
    When the owner has not called into the store yet
    Then the submission is not done
    """
    future = _ship(foreign_thread, store, partial(store.store, Mineral(qid="Q1", name="quartz")))
    assert not future.done()


def test_foreign_submission_runs_on_owner_piggyback(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a store write shipped from a foreign thread
    When the owner commits
    Then the submission is done
    And its result is the new OID
    """
    future = _ship(foreign_thread, store, partial(store.store, Mineral(qid="Q1", name="quartz")))
    store.commit()
    assert future.done()
    assert isinstance(future.result(), int)


def test_piggybacked_submission_is_committed_with_the_owner(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a quartz store shipped from a foreign thread
    When the owner commits
    Then the quartz is found by its key
    """
    _ship(foreign_thread, store, partial(store.store, Mineral(qid="Q1", name="quartz")))
    store.commit()
    found = store.get(Mineral, qid="Q1")
    assert found is not None
    assert found.name == "quartz"


def test_run_pending_drains_the_queue_and_reports_the_count(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given three submissions shipped from a foreign thread
    When the owner runs the pending work
    Then three submissions ran
    """
    seen: list[int] = []
    _ship(foreign_thread, store, partial(_record, seen, 0))
    _ship(foreign_thread, store, partial(_record, seen, 1))
    _ship(foreign_thread, store, partial(_record, seen, 2))
    ran = store.run_pending()
    assert ran == 3


def test_run_pending_runs_submissions_in_fifo_order(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given three submissions shipped from a foreign thread in order
    When the owner runs the pending work
    Then they ran in the order shipped
    And each future holds its own result
    """
    seen: list[int] = []
    first = _ship(foreign_thread, store, partial(_record, seen, 0))
    second = _ship(foreign_thread, store, partial(_record, seen, 1))
    third = _ship(foreign_thread, store, partial(_record, seen, 2))
    store.run_pending()
    assert seen == [0, 1, 2]
    assert [first.result(), second.result(), third.result()] == [0, 1, 2]


def test_second_run_pending_finds_nothing(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given submissions already drained by run_pending
    When the owner runs the pending work again
    Then no submission ran
    """
    seen: list[int] = []
    _ship(foreign_thread, store, partial(_record, seen, 0))
    store.run_pending()
    ran_again = store.run_pending()
    assert ran_again == 0


def test_owner_submit_runs_inline(store: dc.Store) -> None:
    """Given the owner thread
    When it submits a closure itself
    Then the future is already resolved with the closure's result
    """
    future = store.submit(partial(sum, [41, 1]))
    assert future.result(timeout=0) == 42


def test_submission_exception_lands_in_the_future(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a foreign submission whose closure raises ValueError
    When the owner runs the pending work
    Then the future re-raises the ValueError
    """
    future = _ship(foreign_thread, store, _explode)
    store.run_pending()
    with pytest.raises(ValueError, match="boom"):
        future.result(timeout=0)


def test_live_entity_result_raises_entity_escape(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a foreign submission returning the live entity itself
    When the owner runs the pending work
    Then the future raises EntityEscapeError naming submit
    """
    _store_quartz(store)
    future = _ship(foreign_thread, store, partial(_the_entity, store))
    store.run_pending()
    with pytest.raises(dc.EntityEscapeError, match="submit"):
        future.result(timeout=0)


def test_entity_nested_in_containers_raises_entity_escape(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a foreign submission returning a live entity nested in lists
    When the owner runs the pending work
    Then the future raises EntityEscapeError naming submit
    """
    _store_quartz(store)
    future = _ship(foreign_thread, store, partial(_entity_nested_in_containers, store))
    store.run_pending()
    with pytest.raises(dc.EntityEscapeError, match="submit"):
        future.result(timeout=0)


def test_entity_as_dict_value_raises_entity_escape(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a foreign submission returning a live entity as a dict value
    When the owner runs the pending work
    Then the future raises EntityEscapeError naming submit
    """
    _store_quartz(store)
    future = _ship(foreign_thread, store, partial(_entity_as_dict_value, store))
    store.run_pending()
    with pytest.raises(dc.EntityEscapeError, match="submit"):
        future.result(timeout=0)


def test_loaded_lazy_handle_raises_entity_escape(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a foreign submission returning a loaded Lazy handle
    When the owner runs the pending work
    Then the future raises EntityEscapeError naming submit
    """
    _store_quartz(store)
    future = _ship(foreign_thread, store, partial(_loaded_lazy_handle, store))
    store.run_pending()
    with pytest.raises(dc.EntityEscapeError, match="submit"):
        future.result(timeout=0)


def test_plain_data_result_passes(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a foreign submission returning a dict of plain strings
    When the owner runs the pending work
    Then the future resolves with that dict
    """
    _store_quartz(store)
    future = _ship(foreign_thread, store, partial(_plain_fields, store))
    store.run_pending()
    assert future.result(timeout=0) == {"qid": "Q1", "name": "quartz"}


def test_direct_foreign_access_still_raises_wrong_thread(
    store: dc.Store, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a store owned by the test thread
    When another thread commits it directly
    Then the commit raises WrongThreadError
    And the message embeds the submit recipe
    """
    trespass = foreign_thread.submit(store.commit)
    with pytest.raises(dc.WrongThreadError, match="submit"):
        trespass.result()


def test_close_fails_pending_submissions(
    store_factory: StoreFactory, foreign_thread: ThreadPoolExecutor
) -> None:
    """Given a submission shipped from a foreign thread and still queued
    When the owner closes the store
    Then the future raises StoreClosedError
    """
    store = store_factory()
    future = _ship(foreign_thread, store, partial(int, 1))
    store.close()
    with pytest.raises(dc.StoreClosedError):
        future.result(timeout=0)
