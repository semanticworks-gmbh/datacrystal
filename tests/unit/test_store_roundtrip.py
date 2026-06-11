"""The tracer-bullet loop: persist → close → reopen → graph restored."""

from __future__ import annotations

import gc

import datacrystal as dc
from datacrystal._entity import oid_of
from tests.conftest import Locality, Mineral


@dc.entity
class Node:
    name: str
    peer: "Node | None" = None


def _cabinet(store: dc.Store) -> None:
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine", country="Namibia")
    store.root = [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal", mohs=7.0),
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic",
                mohs=3.5, type_locality=dc.Lazy.of(tsumeb)),
    ]


def test_root_is_none_initially(store):
    assert store.root is None
    assert store.commit() is None  # empty commit is a no-op


def test_commit_reopen_restores_graph(store_factory):
    store = store_factory()
    _cabinet(store)
    tid = store.commit()
    assert tid == 1
    store.close()

    reopened = store_factory()
    quartz, azurite = reopened.root
    assert quartz.name == "quartz"
    assert azurite.crystal_system == "monoclinic"
    assert reopened.last_tid == 1
    reopened.close()


def test_identity_one_live_instance_per_oid(store_factory):
    store = store_factory()
    _cabinet(store)
    store.commit()
    store.close()

    reopened = store_factory()
    via_root = reopened.root[1]
    via_unique = reopened.get(Mineral, qid="Q193563")
    via_query = reopened.query(Mineral.crystal_system == "monoclinic")[0]
    assert via_root is via_unique is via_query
    reopened.close()


def test_lazy_defers_until_get(store_factory):
    store = store_factory()
    _cabinet(store)
    store.commit()
    store.close()

    reopened = store_factory()
    azurite = reopened.get(Mineral, qid="Q193563")
    ref = azurite.type_locality
    assert isinstance(ref, dc.Lazy)
    assert not ref.loaded and ref.peek() is None
    assert ref.get().name == "Tsumeb Mine"
    assert ref.loaded and ref.get() is ref.get()
    reopened.close()


def test_dirty_tracking_update_persists(store_factory):
    store = store_factory()
    _cabinet(store)
    store.commit()
    azurite = store.get(Mineral, qid="Q193563")
    azurite.mohs = 4.0  # one-shot hook buffers it
    assert store.commit() == 2
    store.close()

    reopened = store_factory()
    assert reopened.get(Mineral, qid="Q193563").mohs == 4.0
    reopened.close()


def test_no_change_no_commit(store_factory):
    store = store_factory()
    _cabinet(store)
    store.commit()
    assert store.commit() is None
    store.close()


def test_cyclic_eager_graph_roundtrips(store_factory):
    store = store_factory()
    a = Node(name="a")
    b = Node(name="b", peer=a)
    a.peer = b
    store.root = a
    store.commit()
    store.close()

    reopened = store_factory()
    a2 = reopened.root
    assert a2.name == "a"
    assert a2.peer.peer is a2  # the cycle survives, identity intact
    reopened.close()


def test_late_addition_to_pending_graph(store_factory):
    """An entity added to an already-registered object before commit must be
    discovered at P1 (the per-pass walk-memo regression test)."""
    store = store_factory()
    quartz = Mineral(qid="Q1", name="quartz")
    store.store(quartz)
    quartz.type_locality = dc.Lazy.of(Locality(qid="Q2", name="late"))
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.get(Locality, qid="Q2").name == "late"
    reopened.close()


def test_get_many_hydrates_in_order(store_factory):
    store = store_factory()
    minerals = [Mineral(qid=f"Q{i}", name=f"m{i}") for i in range(20)]
    store.root = minerals
    store.commit()
    store.close()

    reopened = store_factory()
    hits = reopened.query(Mineral.crystal_system == None)  # noqa: E711
    oids = [oid_of(m) for m in hits]
    again = reopened.get_many(oids)
    assert [m.name for m in again] == [m.name for m in hits]
    reopened.close()


def test_clean_entities_are_collectable(store_factory):
    """The registry must not keep clean, unreferenced entities alive."""
    store = store_factory()
    _cabinet(store)
    store.commit()
    registry = store._registry
    store.close()

    reopened = store_factory()
    _ = reopened.root  # hydrate, then drop every strong reference
    registry = reopened._registry
    assert len(registry) > 0
    del _
    gc.collect()
    assert len(registry) == 0
    reopened.close()


def test_uncommitted_changes_are_discarded_on_close(store_factory):
    store = store_factory()
    _cabinet(store)
    store.commit()
    azurite = store.get(Mineral, qid="Q193563")
    azurite.mohs = 9.9
    store.close()  # explicit contract: close() never commits

    reopened = store_factory()
    assert reopened.get(Mineral, qid="Q193563").mohs == 3.5
    reopened.close()
