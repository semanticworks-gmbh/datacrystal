"""#213 — a discarded new entity is untracked again, so its next store() allocates a fresh OID.

It used to keep its OID while the allocator rewound, and the next new entity took the same OID.
Re-storing the discarded entity then returned the other entity's OID and wrote nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import pytest

import datacrystal as dc
from tests.conftest import Locality, Mineral

StoreFactory = Callable[[], dc.Store]


def _seeded(store_factory: StoreFactory) -> dc.Store:
    """Open a store holding one committed mineral, so OID allocation is past the base."""
    store = store_factory()
    store.root = [Mineral(qid="Q0", name="seed")]
    store.commit()
    return store


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
    quartz_back = reopened.get(Mineral, qid="Q1")
    calcite_back = reopened.get(Mineral, qid="Q2")
    assert committed_tid is not None
    assert quartz_back is not None
    assert quartz_back.name == "quartz"
    assert calcite_back is not None
    assert calcite_back.name == "calcite"
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


def test_discard_detaches_a_child_reached_from_a_new_entity(
    store_factory: StoreFactory,
) -> None:
    """Given a new mineral whose locality child was stored with it
    When the store discards, then another mineral is stored and committed
    Then storing the discarded mineral again writes both it and its child
    """
    store = _seeded(store_factory)
    locality = Locality(qid="L1", name="Mount Vesuvius")
    quartz = Mineral(qid="Q1", name="quartz", type_locality=dc.Lazy.of(locality))
    store.store(quartz)
    store.discard()
    calcite = Mineral(qid="Q2", name="calcite")
    store.store(calcite)
    store.commit()

    store.store(quartz)
    store.commit()
    store.close()

    reopened = store_factory()
    found = reopened.get(Locality, qid="L1")
    quartz_back = reopened.get(Mineral, qid="Q1")
    assert found is not None
    assert found.name == "Mount Vesuvius"
    assert quartz_back is not None
    assert quartz_back.name == "quartz"
    reopened.close()


def test_edit_of_a_discarded_entity_not_stored_again_commits_nothing(
    store_factory: StoreFactory,
) -> None:
    """Given a new mineral discarded, with another mineral stored and committed
    When the discarded mineral is edited and the store commits, then reopens
    Then the commit wrote nothing and the mineral is not in the store
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
    quartz_back = reopened.get(Mineral, qid="Q1")
    assert quartz_back is not None
    assert quartz_back.name == "rock crystal"
    reopened.close()


def test_discard_keeps_the_oid_of_a_committed_entity_that_was_edited(
    store_factory: StoreFactory,
) -> None:
    """Given a committed mineral edited but not committed
    When the store discards
    Then its OID still reads the committed mineral
    """
    store = _seeded(store_factory)
    seed = store.get(Mineral, qid="Q0")
    assert seed is not None
    seed_oid = store.store(seed)
    seed.name = "edited"

    store.discard()

    by_oid = store.get_many([seed_oid])[0]
    assert by_oid.name == "seed"
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
    rejected_back = reopened.get(Mineral, qid="Q1")
    calcite_back = reopened.get(Mineral, qid="Q2")
    assert rejected_back is not None
    assert rejected_back.name == "duplicate"
    assert calcite_back is not None
    assert calcite_back.name == "calcite"
    reopened.close()


def test_discard_detaches_a_new_entity_that_was_stored_then_deleted(
    store_factory: StoreFactory,
) -> None:
    """Given a new locality stored then deleted, and the store discarded
    When another locality is stored and a mineral referring to the first is stored, then committed
    Then the first locality was detached and the mineral's reference reads back as the first one
    """
    store = _seeded(store_factory)
    first = Locality(qid="L1", name="Mount Vesuvius")
    store.store(first)
    store.delete(first)
    store.discard()
    second = Locality(qid="L2", name="Elba")
    store.store(second)

    store.store(Mineral(qid="Q3", name="quartz", type_locality=dc.Lazy.of(first)))
    store.commit()
    store.close()

    reopened = store_factory()
    quartz_back = reopened.get(Mineral, qid="Q3")
    assert quartz_back is not None
    assert quartz_back.type_locality is not None
    assert quartz_back.type_locality.get().qid == "L1"
    reopened.close()


@dc.entity(protected=True)
class Doc:
    """A protected document."""

    name: Annotated[str, dc.Unique]


@dc.entity(protected=True)
class Folder:
    """A protected folder holding one document by a Lazy handle."""

    name: Annotated[str, dc.Unique]
    doc: dc.Lazy[Doc] | None = None


TEAM = 2
ANNA = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
BOB = dc.Principal(uid=3, memberships={1: dc.STAFF})


def test_discard_restores_the_labels_a_protected_entity_inherited_from_its_container(
    store_factory: StoreFactory,
) -> None:
    """Given a protected doc stored inside a folder shared with a team, then discarded
    When another principal stores the doc on its own
    Then the doc carries no group and the store accepts the commit
    """
    store = store_factory()
    doc = Doc(name="d1")
    with store.acting_as(ANNA):
        folder = Folder(name="f1", doc=dc.Lazy.of(doc))
        dc.share(folder, TEAM, read=dc.VIEWER, write=dc.AGENT)
        store.store(folder)
    store.discard()

    with store.acting_as(BOB):
        store.store(doc)
        committed_tid = store.commit()

    permissions = doc.dc_permissions  # pyrefly: ignore[missing-attribute] - added by @dc.entity(protected=True)
    assert committed_tid is not None
    assert permissions.groups == ()
    assert permissions.owner == BOB.uid
    store.close()


def test_a_p2_failure_followed_by_discard_leaves_the_new_entity_storable_again(
    store_factory: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a commit of a new mineral whose backend write failed, then the store discarded
    When another mineral is committed and the first is stored and committed again
    Then both minerals read back with their names
    """
    store = _seeded(store_factory)
    quartz = Mineral(qid="Q1", name="quartz")
    store.store(quartz)
    monkeypatch.setattr(store._backend, "apply", _failing_apply)
    with pytest.raises(OSError, match="disk full"):
        store.commit()
    monkeypatch.undo()
    store.discard()
    calcite = Mineral(qid="Q2", name="calcite")
    store.store(calcite)
    store.commit()

    store.store(quartz)
    store.commit()
    store.close()

    reopened = store_factory()
    quartz_back = reopened.get(Mineral, qid="Q1")
    calcite_back = reopened.get(Mineral, qid="Q2")
    assert quartz_back is not None
    assert quartz_back.name == "quartz"
    assert calcite_back is not None
    assert calcite_back.name == "calcite"
    reopened.close()


def _failing_apply(batch: object) -> None:
    raise OSError("disk full")
