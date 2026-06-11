"""The root contract: pinned identity, any persistable value as root.

The store holds a strong reference to the root holder, so everything
reachable from ``store.root`` stays live — without the pin, a CLEAN root
graph with no user references was collected and silently rehydrated on the
next access (fresh objects, in-place mutations lost). ``Lazy[T]`` is the
explicit cut point where pinning stops.
"""

from __future__ import annotations

import gc

from tests.conftest import Mineral


def _cabinet(store) -> None:
    store.root = [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"),
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic"),
    ]


def test_root_identity_stable_without_user_refs(store_factory):
    """store.root must return the SAME object every time, even when the
    user holds no strong reference between accesses (the v0.1 GC bug)."""
    store = store_factory()
    _cabinet(store)
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.root is reopened.root
    first = reopened.root[0]
    gc.collect()
    assert reopened.root[0] is first
    reopened.close()


def test_root_pin_released_on_close(store_factory):
    store = store_factory()
    _cabinet(store)
    store.commit()
    registry = store._registry
    assert len(registry) > 0
    store.close()
    gc.collect()
    assert len(registry) == 0


def test_empty_list_root(store_factory):
    store = store_factory()
    assert store.root is None
    store.root = []
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.root == []
    assert reopened.root is not None  # "is None" stays the first-run check
    reopened.close()


def test_empty_dict_root(store_factory):
    store = store_factory()
    store.root = {}
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.root == {}
    reopened.close()


def test_dict_root_roundtrips(store_factory):
    store = store_factory()
    store.root = {"minerals": [Mineral(qid="Q1", name="quartz")], "rev": 1}
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.root["rev"] == 1
    assert reopened.root["minerals"][0].name == "quartz"
    reopened.close()


def test_scalar_root_roundtrips(store_factory):
    store = store_factory()
    store.root = "just a string"
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.root == "just a string"
    reopened.close()


def test_root_reassignment_replaces_value(store_factory):
    store = store_factory()
    store.root = ["old"]
    store.commit()
    store.root = ["new"]
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.root == ["new"]
    reopened.close()


def test_entity_root_is_pinned(store_factory):
    store = store_factory()
    store.root = Mineral(qid="QM", name="azurite")
    store.commit()
    store.close()

    reopened = store_factory()
    root = reopened.root
    assert root.name == "azurite"
    gc.collect()
    assert reopened.root is root
    reopened.close()
