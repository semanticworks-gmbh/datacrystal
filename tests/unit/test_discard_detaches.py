"""#213 — a discarded new entity is untracked again, so its next store() allocates a fresh OID.

It used to keep its OID while the allocator rewound, and the next new entity took the same OID.
Re-storing the discarded entity then returned the other entity's OID and wrote nothing.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of
from tests.conftest import Locality, Mineral

StoreFactory = Callable[[], dc.Store]


def _seeded(store_factory: StoreFactory) -> dc.Store:
    """Open a store holding one committed mineral, so OID allocation is past the base."""
    store = store_factory()
    store.root = [Mineral(qid="Q0", name="seed")]
    store.commit()
    return store


def _name_of(store: dc.Store, qid: str) -> str | None:
    """Return the name of the stored mineral with ``qid``, or None when there is none."""
    found = store.get(Mineral, qid=qid)
    return found.name if found is not None else None


def test_discarded_new_entity_is_stored_again_with_its_own_oid(
    store_factory: StoreFactory,
) -> None:
    """Given a new mineral discarded and another mineral stored and committed
    When the discarded mineral is stored again and committed, and the store is reopened
    Then both minerals read back with their names
    """
    store = _seeded(store_factory)
    quartz = Mineral(qid="Q1", name="quartz")
    store.store(quartz)
    store.discard()
    calcite = Mineral(qid="Q2", name="calcite")
    store.store(calcite)
    store.commit()

    store.store(quartz)
    committed_tid = store.commit()
    store.close()

    reopened = store_factory()
    assert committed_tid is not None
    assert _name_of(reopened, "Q1") == "quartz"
    assert _name_of(reopened, "Q2") == "calcite"
    reopened.close()


def test_discarded_new_entity_gets_an_oid_different_from_the_next_new_entity(
    store_factory: StoreFactory,
) -> None:
    """Given a new mineral discarded and another mineral stored and committed
    When the discarded mineral is stored again
    Then its OID differs from the other mineral's OID
    """
    store = _seeded(store_factory)
    quartz = Mineral(qid="Q1", name="quartz")
    store.store(quartz)
    store.discard()
    calcite = Mineral(qid="Q2", name="calcite")
    calcite_oid = store.store(calcite)
    store.commit()

    quartz_oid = store.store(quartz)

    assert quartz_oid != calcite_oid
    store.close()


def test_discard_leaves_a_new_entity_without_an_oid(store_factory: StoreFactory) -> None:
    """Given a new mineral stored, so it holds an OID
    When the store discards
    Then the mineral has no OID
    """
    store = _seeded(store_factory)
    quartz = Mineral(qid="Q1", name="quartz")
    store.store(quartz)

    store.discard()

    assert oid_of(quartz) is None
    store.close()


def test_discard_detaches_a_child_reached_from_a_new_entity(
    store_factory: StoreFactory,
) -> None:
    """Given a new mineral whose locality child was stored with it
    When the store discards, then another mineral is stored and committed
    Then storing the discarded mineral again writes both it and its child under distinct OIDs
    """
    store = _seeded(store_factory)
    locality = Locality(qid="L1", name="Mount Vesuvius")
    quartz = Mineral(qid="Q1", name="quartz", type_locality=dc.Lazy.of(locality))
    store.store(quartz)
    store.discard()
    store.store(Mineral(qid="Q2", name="calcite"))
    store.commit()

    store.store(quartz)
    store.commit()
    store.close()

    reopened = store_factory()
    found = reopened.get(Locality, qid="L1")
    assert found is not None
    assert found.name == "Mount Vesuvius"
    assert _name_of(reopened, "Q1") == "quartz"
    reopened.close()


def test_edit_of_a_discarded_entity_not_stored_again_commits_nothing(
    store_factory: StoreFactory,
) -> None:
    """Given a new mineral discarded, with another mineral stored and committed
    When the discarded mineral is edited and the store commits, then reopens
    Then the commit wrote nothing and the edited mineral is not in the store
    """
    store = _seeded(store_factory)
    quartz = Mineral(qid="Q1", name="quartz")
    store.store(quartz)
    store.discard()
    store.store(Mineral(qid="Q2", name="calcite"))
    store.commit()

    quartz.name = "rock crystal"
    committed_tid = store.commit()
    store.close()

    reopened = store_factory()
    assert committed_tid is None
    assert reopened.get(Mineral, qid="Q1") is None
    reopened.close()


def test_edit_then_store_of_a_discarded_entity_writes_the_edit(
    store_factory: StoreFactory,
) -> None:
    """Given a new mineral discarded and then edited like any untracked entity
    When it is stored and committed, and the store is reopened
    Then the stored record carries the edit
    """
    store = _seeded(store_factory)
    quartz = Mineral(qid="Q1", name="quartz")
    store.store(quartz)
    store.discard()
    quartz.name = "rock crystal"

    store.store(quartz)
    store.commit()
    store.close()

    reopened = store_factory()
    assert _name_of(reopened, "Q1") == "rock crystal"
    reopened.close()


def test_discard_keeps_the_oid_of_a_committed_entity_that_was_edited(
    store_factory: StoreFactory,
) -> None:
    """Given a committed mineral edited but not committed
    When the store discards
    Then the mineral keeps its OID
    """
    store = _seeded(store_factory)
    seed = store.get(Mineral, qid="Q0")
    assert seed is not None
    seed_oid = oid_of(seed)
    seed.name = "edited"

    store.discard()

    assert oid_of(seed) == seed_oid
    assert _name_of(store, "Q0") == "seed"
    store.close()


def test_rejected_commit_keeps_new_entities_buffered_with_their_oids(
    store_factory: StoreFactory,
) -> None:
    """Given a new mineral whose qid duplicates a committed one, so the commit is rejected
    When a second new mineral is stored
    Then the two new minerals hold distinct OIDs
    """
    store = _seeded(store_factory)
    duplicate = Mineral(qid="Q0", name="duplicate")
    duplicate_oid = store.store(duplicate)
    with pytest.raises(dc.UniqueViolationError):
        store.commit()

    calcite_oid = store.store(Mineral(qid="Q2", name="calcite"))

    assert calcite_oid != duplicate_oid
    store.close()


def test_discard_after_a_rejected_commit_lets_the_rejected_entity_be_stored_again(
    store_factory: StoreFactory,
) -> None:
    """Given a new mineral whose commit was rejected as a duplicate qid, then discarded
    When another mineral is committed and the fixed rejected one is stored and committed
    Then both minerals read back with their names
    """
    store = _seeded(store_factory)
    rejected = Mineral(qid="Q0", name="duplicate")
    store.store(rejected)
    with pytest.raises(dc.UniqueViolationError):
        store.commit()
    store.discard()
    store.store(Mineral(qid="Q2", name="calcite"))
    store.commit()

    rejected.qid = "Q1"
    store.store(rejected)
    store.commit()
    store.close()

    reopened = store_factory()
    assert _name_of(reopened, "Q1") == "duplicate"
    assert _name_of(reopened, "Q2") == "calcite"
    reopened.close()
