"""The unique secondary-key index (SDA delta 1)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

import datacrystal as dc
from tests.conftest import Locality, Mineral


def _store_with_one_mineral(store: dc.Store, qid: str, name: str) -> None:
    store.root = [Mineral(qid=qid, name=name)]


def test_get_by_unique_key_finds_the_mineral(store: dc.Store) -> None:
    """Given a committed mineral with qid Q43010
    When it is fetched by that unique key
    Then the quartz is returned
    """
    _store_with_one_mineral(store, "Q43010", "quartz")
    store.commit()

    found = store.get(Mineral, qid="Q43010")

    assert found is not None
    assert found.name == "quartz"


def test_get_by_an_unknown_unique_key_returns_none(store: dc.Store) -> None:
    """Given a committed mineral with qid Q43010
    When a mineral is fetched by the unknown key Q-missing
    Then None is returned
    """
    _store_with_one_mineral(store, "Q43010", "quartz")
    store.commit()

    found = store.get(Mineral, qid="Q-missing")

    assert found is None


def test_get_by_a_non_unique_field_raises_query_error(store: dc.Store) -> None:
    """Given a committed mineral named quartz
    When a mineral is fetched by its name, which is not Unique
    Then QueryError is raised
    """
    _store_with_one_mineral(store, "Q1", "quartz")
    store.commit()

    with pytest.raises(dc.QueryError):
        store.get(Mineral, name="quartz")


def test_duplicate_unique_key_is_rejected(store: dc.Store) -> None:
    """Given a committed mineral with qid Q1
    When a second mineral with qid Q1 is committed
    Then the commit raises UniqueViolationError
    """
    _store_with_one_mineral(store, "Q1", "first")
    store.commit()
    store.store(Mineral(qid="Q1", name="dupe"))

    with pytest.raises(dc.UniqueViolationError):
        store.commit()


def test_rejected_duplicate_leaves_the_first_mineral_visible_after_reopen(
    store_factory: Callable[[], dc.Store],
) -> None:
    """Given a committed mineral with qid Q1 and a rejected duplicate commit
    When the store is closed and reopened
    Then the mineral with qid Q1 is still the first one
    """
    store = store_factory()
    _store_with_one_mineral(store, "Q1", "first")
    store.commit()
    store.store(Mineral(qid="Q1", name="dupe"))
    with pytest.raises(dc.UniqueViolationError):
        store.commit()
    store.close()

    reopened = store_factory()

    first = reopened.get(Mineral, qid="Q1")
    assert first is not None
    assert first.name == "first"
    reopened.close()


def test_rejected_duplicate_leaves_the_last_tid_at_one(
    store_factory: Callable[[], dc.Store],
) -> None:
    """Given a committed mineral with qid Q1 and a rejected duplicate commit
    When the store is closed and reopened
    Then its last transaction id is still 1
    """
    store = store_factory()
    _store_with_one_mineral(store, "Q1", "first")
    store.commit()
    store.store(Mineral(qid="Q1", name="dupe"))
    with pytest.raises(dc.UniqueViolationError):
        store.commit()
    store.close()

    reopened = store_factory()

    assert reopened.last_tid == 1
    reopened.close()


def test_duplicates_within_one_commit_are_rejected(store: dc.Store) -> None:
    """Given two new localities with qid QX stored in one commit
    When the commit runs
    Then it raises UniqueViolationError
    """
    store.store(Locality(qid="QX", name="one"))
    store.store(Locality(qid="QX", name="two"))

    with pytest.raises(dc.UniqueViolationError):
        store.commit()


def test_failed_commit_burns_no_tid(store: dc.Store) -> None:
    """Given a committed mineral with qid Q1 and a rejected duplicate commit
    When the duplicate's qid is changed to Q2 and committed
    Then the transaction id is 2, with no gap
    """
    _store_with_one_mineral(store, "Q1", "first")
    assert store.commit() == 1
    dupe = Mineral(qid="Q1", name="dupe")
    store.store(dupe)
    with pytest.raises(dc.UniqueViolationError):
        store.commit()
    dupe.qid = "Q2"

    tid = store.commit()

    assert tid == 2


def test_updating_the_key_removes_the_old_index_entry(store: dc.Store) -> None:
    """Given a committed mineral with qid Q-old
    When its qid is changed to Q-new and committed
    Then a lookup by Q-old finds nothing
    """
    _store_with_one_mineral(store, "Q-old", "m")
    store.commit()
    moved = store.get(Mineral, qid="Q-old")
    assert moved is not None
    moved.qid = "Q-new"
    store.commit()

    found = store.get(Mineral, qid="Q-old")

    assert found is None


def test_updating_the_key_adds_the_new_index_entry(store: dc.Store) -> None:
    """Given a committed mineral with qid Q-old
    When its qid is changed to Q-new and committed
    Then a lookup by Q-new finds the same mineral
    """
    _store_with_one_mineral(store, "Q-old", "m")
    store.commit()
    mineral = store.get(Mineral, qid="Q-old")
    assert mineral is not None
    mineral.qid = "Q-new"
    store.commit()

    found = store.get(Mineral, qid="Q-new")

    assert found is mineral
