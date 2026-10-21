"""The tracer-bullet loop: persist → close → reopen → graph restored."""

from __future__ import annotations

import gc
import weakref
from collections.abc import Callable

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of
from tests.conftest import Locality, Mineral

StoreFactory = Callable[[], dc.Store]


@dc.entity
class Node:
    """An eager graph node whose peer can close a cycle."""

    name: str
    peer: Node | None = None


def _cabinet(store: dc.Store) -> None:
    """Root list of quartz and azurite, azurite lazily pointing at Tsumeb Mine."""
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine", country="Namibia")
    store.root = [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal", mohs=7.0),
        Mineral(
            qid="Q193563",
            name="azurite",
            crystal_system="monoclinic",
            mohs=3.5,
            type_locality=dc.Lazy.of(tsumeb),
        ),
    ]


def _twenty_minerals() -> list[Mineral]:
    return [Mineral(qid=f"Q{i}", name=f"m{i}") for i in range(20)]


def _commit_cabinet(store_factory: StoreFactory) -> None:
    seeding = store_factory()
    _cabinet(seeding)
    seeding.commit()
    seeding.close()


def test_fresh_store_root_is_none(store: dc.Store) -> None:
    """Given a fresh store
    When store.root is read
    Then it is None
    """
    root = store.root

    assert root is None


def test_empty_commit_is_a_no_op(store: dc.Store) -> None:
    """Given a fresh store with nothing stored
    When the store commits
    Then nothing is committed
    """
    tid = store.commit()

    assert tid is None


def test_first_commit_is_tid_one(store_factory: StoreFactory) -> None:
    """Given a cabinet root on a fresh store
    When the store commits
    Then the TID is 1
    """
    store = store_factory()
    _cabinet(store)

    tid = store.commit()

    assert tid == 1
    store.close()


def test_reopen_restores_the_root_graph(store_factory: StoreFactory) -> None:
    """Given a committed cabinet of quartz and azurite
    When the store is reopened
    Then the root holds quartz
    And the root holds azurite
    """
    _commit_cabinet(store_factory)

    reopened = store_factory()
    quartz, azurite = reopened.root

    assert quartz.name == "quartz"
    assert azurite.crystal_system == "monoclinic"
    reopened.close()


def test_reopen_restores_the_last_tid(store_factory: StoreFactory) -> None:
    """Given a cabinet committed as TID 1
    When the store is reopened
    Then last_tid is 1
    """
    _commit_cabinet(store_factory)

    reopened = store_factory()

    assert reopened.last_tid == 1
    reopened.close()


def test_root_and_unique_get_share_one_live_instance(store_factory: StoreFactory) -> None:
    """Given a committed cabinet and a reopened store
    When azurite is read through the root and through its unique qid
    Then both are the identical instance
    """
    _commit_cabinet(store_factory)
    reopened = store_factory()

    via_root = reopened.root[1]
    via_unique = reopened.get(Mineral, qid="Q193563")

    assert via_root is via_unique
    reopened.close()


def test_root_and_query_share_one_live_instance(store_factory: StoreFactory) -> None:
    """Given a committed cabinet and a reopened store
    When azurite is read through the root and through a crystal_system query
    Then both are the identical instance
    """
    _commit_cabinet(store_factory)
    reopened = store_factory()

    via_root = reopened.root[1]
    via_query = reopened.query(dc.fields(Mineral).crystal_system == "monoclinic")[0]

    assert via_root is via_query
    reopened.close()


def test_lazy_handle_is_unloaded_after_reopen(store_factory: StoreFactory) -> None:
    """Given a committed azurite pointing lazily at Tsumeb Mine, reopened
    When azurite's type_locality is read
    Then it is a Lazy
    And it is not loaded
    And peek() is None
    """
    _commit_cabinet(store_factory)
    reopened = store_factory()
    azurite = reopened.get(Mineral, qid="Q193563")
    assert azurite is not None

    ref = azurite.type_locality

    assert isinstance(ref, dc.Lazy)
    assert not ref.loaded
    assert ref.peek() is None
    reopened.close()


def test_lazy_get_loads_the_target(store_factory: StoreFactory) -> None:
    """Given an unloaded Lazy handle to Tsumeb Mine on a reopened azurite
    When get() is called
    Then the locality is named "Tsumeb Mine"
    And the handle is loaded
    """
    _commit_cabinet(store_factory)
    reopened = store_factory()
    azurite = reopened.get(Mineral, qid="Q193563")
    assert azurite is not None
    ref = azurite.type_locality

    locality = ref.get()

    assert locality.name == "Tsumeb Mine"
    assert ref.loaded
    reopened.close()


def test_lazy_get_returns_the_same_instance_each_time(store_factory: StoreFactory) -> None:
    """Given a Lazy handle to Tsumeb Mine on a reopened azurite
    When get() is called twice
    Then both calls return the identical instance
    """
    _commit_cabinet(store_factory)
    reopened = store_factory()
    azurite = reopened.get(Mineral, qid="Q193563")
    assert azurite is not None
    ref = azurite.type_locality

    first = ref.get()
    second = ref.get()

    assert first is second
    reopened.close()


def test_update_commits_as_the_next_tid(store_factory: StoreFactory) -> None:
    """Given a committed cabinet whose azurite then gets mohs 4.0 via the one-shot hook
    When the store commits
    Then the TID is 2
    """
    store = store_factory()
    _cabinet(store)
    store.commit()
    azurite = store.get(Mineral, qid="Q193563")
    assert azurite is not None
    azurite.mohs = 4.0

    tid = store.commit()

    assert tid == 2
    store.close()


def test_update_survives_reopen(store_factory: StoreFactory) -> None:
    """Given a committed cabinet whose azurite got mohs 4.0 in a second commit
    When the store is reopened
    Then azurite has mohs 4.0
    """
    store = store_factory()
    _cabinet(store)
    store.commit()
    azurite = store.get(Mineral, qid="Q193563")
    assert azurite is not None
    azurite.mohs = 4.0
    store.commit()
    store.close()

    reopened = store_factory()
    reopened_azurite = reopened.get(Mineral, qid="Q193563")

    assert reopened_azurite is not None
    assert reopened_azurite.mohs == 4.0
    reopened.close()


def test_commit_without_changes_is_a_no_op(store_factory: StoreFactory) -> None:
    """Given a cabinet committed and left unchanged
    When the store commits again
    Then nothing is committed
    """
    store = store_factory()
    _cabinet(store)
    store.commit()

    tid = store.commit()

    assert tid is None
    store.close()


@pytest.fixture
def committed_cycle(store_factory: StoreFactory) -> None:
    """Commit the eager cycle a <-> b with a as the root."""
    store = store_factory()
    a = Node(name="a")
    b = Node(name="b", peer=a)
    a.peer = b
    store.root = a
    store.commit()
    store.close()


@pytest.mark.usefixtures("committed_cycle")
def test_cyclic_eager_graph_restores_its_root(store_factory: StoreFactory) -> None:
    """Given a committed eager cycle a <-> b with a as root
    When the store is reopened
    Then the root is named "a"
    """
    reopened = store_factory()

    root = reopened.root

    assert root.name == "a"
    reopened.close()


@pytest.mark.usefixtures("committed_cycle")
def test_cyclic_eager_graph_keeps_identity_around_the_cycle(
    store_factory: StoreFactory,
) -> None:
    """Given a committed eager cycle a <-> b with a as root, reopened
    When the root's peer's peer is read
    Then it is the identical root instance
    """
    reopened = store_factory()
    root = reopened.root

    around = root.peer.peer

    assert around is root
    reopened.close()


def test_late_addition_to_a_pending_graph_is_discovered_at_p1(
    store_factory: StoreFactory,
) -> None:
    """The per-pass walk-memo regression: P1 must find an entity added after store().

    Given quartz stored, then given a Lazy.of a new locality "late"
    When the store commits and is reopened
    Then the locality "late" is found by its qid
    """
    store = store_factory()
    quartz = Mineral(qid="Q1", name="quartz")
    store.store(quartz)
    quartz.type_locality = dc.Lazy.of(Locality(qid="Q2", name="late"))
    store.commit()
    store.close()

    reopened = store_factory()
    late = reopened.get(Locality, qid="Q2")

    assert late is not None
    assert late.name == "late"
    reopened.close()


def test_get_many_hydrates_in_the_order_of_the_oids(store_factory: StoreFactory) -> None:
    """Given 20 committed minerals without a crystal system, reopened
    When the query's oids are passed to get_many
    Then the names come back in the query's order
    """
    store = store_factory()
    store.root = _twenty_minerals()
    store.commit()
    store.close()
    reopened = store_factory()
    hits = reopened.query(dc.fields(Mineral).crystal_system == None)  # noqa: E711 - the magic query path compares with ==
    oids = list(map(oid_of, hits))

    again = reopened.get_many(oids)

    assert [m.name for m in again] == [m.name for m in hits]
    reopened.close()


def test_clean_entity_outside_the_root_is_collectable(store_factory: StoreFactory) -> None:
    """The registry must not keep clean entities alive that the pinned root does not reach.

    Given a committed cabinet and an orphan locality outside the root graph
    When the last reference to the orphan is dropped and garbage collected
    Then a weak reference to the orphan is dead
    """
    store = store_factory()
    _cabinet(store)
    orphan = Locality(qid="QORPHAN", name="not reachable from root")
    store.store(orphan)
    store.commit()
    orphan_ref = weakref.ref(orphan)

    del orphan
    gc.collect()

    assert orphan_ref() is None
    store.close()


def test_root_graph_stays_alive_through_garbage_collection(store_factory: StoreFactory) -> None:
    """Given a committed cabinet and an orphan locality outside the root graph
    When the orphan is dropped and garbage collected
    Then a weak reference to the root's quartz is still alive
    """
    store = store_factory()
    _cabinet(store)
    orphan = Locality(qid="QORPHAN", name="not reachable from root")
    store.store(orphan)
    store.commit()
    quartz_ref = weakref.ref(store.root[0])

    del orphan
    gc.collect()

    assert quartz_ref() is not None
    store.close()


def test_uncommitted_changes_are_discarded_on_close(store_factory: StoreFactory) -> None:
    """The explicit contract: close() never commits.

    Given a committed cabinet whose azurite got mohs 9.9 uncommitted
    When the store is closed and reopened
    Then azurite still has mohs 3.5
    """
    store = store_factory()
    _cabinet(store)
    store.commit()
    azurite = store.get(Mineral, qid="Q193563")
    assert azurite is not None
    azurite.mohs = 9.9
    store.close()

    reopened = store_factory()
    reopened_azurite = reopened.get(Mineral, qid="Q193563")

    assert reopened_azurite is not None
    assert reopened_azurite.mohs == 3.5
    reopened.close()
