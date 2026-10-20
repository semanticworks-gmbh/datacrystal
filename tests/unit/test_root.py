"""The root contract: pinned identity, any persistable value as root.

The store holds a strong reference to the root holder, so everything
reachable from ``store.root`` stays live — without the pin, a CLEAN root
graph with no user references was collected and silently rehydrated on the
next access (fresh objects, in-place mutations lost). ``Lazy[T]`` is the
explicit cut point where pinning stops.
"""

from __future__ import annotations

import gc
import weakref
from collections.abc import Callable

import datacrystal as dc
from tests.conftest import Mineral

StoreFactory = Callable[[], dc.Store]


def _cabinet() -> list[Mineral]:
    return [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"),
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic"),
    ]


def _commit_cabinet_root(store_factory: StoreFactory) -> None:
    seeding = store_factory()
    seeding.root = _cabinet()
    seeding.commit()
    seeding.close()


def test_root_returns_the_same_object_on_every_access(store_factory: StoreFactory) -> None:
    """The v0.1 GC bug: the user held no strong reference between accesses.

    Given a committed cabinet root and a reopened store
    When store.root is read twice without keeping a reference
    Then both reads are the identical object
    """
    _commit_cabinet_root(store_factory)
    reopened = store_factory()

    first_read = reopened.root
    second_read = reopened.root

    assert first_read is second_read
    reopened.close()


def test_root_member_survives_garbage_collection(store_factory: StoreFactory) -> None:
    """Given a reopened store whose first root mineral was read
    When garbage collection runs
    Then the root's first mineral is still the identical object
    """
    _commit_cabinet_root(store_factory)
    reopened = store_factory()
    first = reopened.root[0]

    gc.collect()

    assert reopened.root[0] is first
    reopened.close()


def test_root_pin_is_released_on_close(store_factory: StoreFactory) -> None:
    """Given a committed cabinet root and a weak reference to its first mineral
    When the store is closed and garbage collected
    Then the mineral is gone
    """
    store = store_factory()
    store.root = _cabinet()
    store.commit()
    quartz = weakref.ref(store.root[0])

    store.close()
    gc.collect()

    assert quartz() is None


def test_fresh_store_has_no_root(store_factory: StoreFactory) -> None:
    """Given a store that was never given a root
    When store.root is read
    Then it is None
    """
    store = store_factory()

    root = store.root

    assert root is None
    store.close()


def test_empty_list_root_reopens_as_an_empty_list(store_factory: StoreFactory) -> None:
    """Given an empty list committed as the root
    When the store is reopened
    Then the root equals []
    """
    store = store_factory()
    store.root = []
    store.commit()
    store.close()

    reopened = store_factory()

    assert reopened.root == []
    reopened.close()


def test_empty_list_root_is_not_none(store_factory: StoreFactory) -> None:
    """The first-run check stays `root is None`, so an empty root must not read as None.

    Given an empty list committed as the root
    When the store is reopened
    Then the root is not None
    """
    store = store_factory()
    store.root = []
    store.commit()
    store.close()

    reopened = store_factory()

    assert reopened.root is not None
    reopened.close()


def test_empty_dict_root_reopens_as_an_empty_dict(store_factory: StoreFactory) -> None:
    """Given an empty dict committed as the root
    When the store is reopened
    Then the root equals {}
    """
    store = store_factory()
    store.root = {}
    store.commit()
    store.close()

    reopened = store_factory()

    assert reopened.root == {}
    reopened.close()


def test_dict_root_keeps_its_scalar_value(store_factory: StoreFactory) -> None:
    """Given a dict root with "rev": 1 and a mineral list, committed
    When the store is reopened
    Then root["rev"] is 1
    """
    store = store_factory()
    store.root = {"minerals": [Mineral(qid="Q1", name="quartz")], "rev": 1}
    store.commit()
    store.close()

    reopened = store_factory()

    assert reopened.root["rev"] == 1
    reopened.close()


def test_dict_root_keeps_its_mineral_list(store_factory: StoreFactory) -> None:
    """Given a dict root with "rev": 1 and a list holding quartz, committed
    When the store is reopened
    Then the first mineral under "minerals" is named "quartz"
    """
    store = store_factory()
    store.root = {"minerals": [Mineral(qid="Q1", name="quartz")], "rev": 1}
    store.commit()
    store.close()

    reopened = store_factory()

    assert reopened.root["minerals"][0].name == "quartz"
    reopened.close()


def test_scalar_root_reopens_as_the_same_string(store_factory: StoreFactory) -> None:
    """Given the string "just a string" committed as the root
    When the store is reopened
    Then the root equals "just a string"
    """
    store = store_factory()
    store.root = "just a string"
    store.commit()
    store.close()

    reopened = store_factory()

    assert reopened.root == "just a string"
    reopened.close()


def test_reassigned_root_replaces_the_old_value(store_factory: StoreFactory) -> None:
    """Given the root ["old"] committed and then reassigned to ["new"] and committed
    When the store is reopened
    Then the root equals ["new"]
    """
    store = store_factory()
    store.root = ["old"]
    store.commit()
    store.root = ["new"]
    store.commit()
    store.close()

    reopened = store_factory()

    assert reopened.root == ["new"]
    reopened.close()


def test_entity_root_reopens_with_its_fields(store_factory: StoreFactory) -> None:
    """Given an azurite entity committed as the root
    When the store is reopened
    Then the root is named "azurite"
    """
    store = store_factory()
    store.root = Mineral(qid="QM", name="azurite")
    store.commit()
    store.close()

    reopened = store_factory()

    assert reopened.root.name == "azurite"
    reopened.close()


def test_entity_root_survives_garbage_collection(store_factory: StoreFactory) -> None:
    """Given a reopened store whose entity root was read
    When garbage collection runs
    Then store.root is still the identical object
    """
    store = store_factory()
    store.root = Mineral(qid="QM", name="azurite")
    store.commit()
    store.close()
    reopened = store_factory()
    root = reopened.root

    gc.collect()

    assert reopened.root is root
    reopened.close()
