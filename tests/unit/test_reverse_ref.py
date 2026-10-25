"""Reverse-reference index + incoming() (#20 / ROADMAP item 8, sub-story A).

incoming(x) = every committed entity that references x — direct (eager) refs and
Lazy refs, in scalar fields and inside list/dict containers. Built lazily from a
store scan, then maintained incrementally at commit. Validated against a
brute-force oracle. (Delete-fold and Snapshot.incoming() parity are the next
sub-stories.) Parametrized over both backends.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import field
from typing import Annotated, Any

import pytest

import datacrystal as dc
from tests.conftest import Locality, Mineral

StoreFactory = Callable[[], dc.Store]


@dc.entity
class Collector:
    """Refers to minerals lazily and to a home locality eagerly."""

    name: Annotated[str, dc.Unique]
    favorites: list[dc.Lazy[Mineral]] = field(default_factory=list)  # lazy adjacency
    home: Locality | None = None  # eager scalar ref


def _localities(count: int) -> list[Locality]:
    return [Locality(qid=f"L{i}", name=f"loc{i}") for i in range(count)]


def _minerals_at(localities: list[Locality], count: int) -> list[Mineral]:
    """`count` minerals M0.., mineral i pointing lazily at locality i modulo the locality count."""
    return [
        Mineral(
            qid=f"M{i}",
            name=f"m{i}",
            type_locality=dc.Lazy.of(localities[i % len(localities)]),
        )
        for i in range(count)
    ]


def _store_all(store: dc.Store, entities: list[Any]) -> None:
    list(map(store.store, entities))


@pytest.fixture
def tsumeb_with_two_referrers(store: dc.Store) -> Locality:
    """Commit Tsumeb with azurite and malachite pointing at it, and an unrelated quartz."""
    tsumeb = Locality(qid="LT", name="Tsumeb")
    azurite = Mineral(qid="QA", name="azurite", type_locality=dc.Lazy.of(tsumeb))
    malachite = Mineral(qid="QM", name="malachite", type_locality=dc.Lazy.of(tsumeb))
    quartz = Mineral(qid="QQ", name="quartz")
    _store_all(store, [tsumeb, azurite, malachite, quartz])
    store.commit()
    return tsumeb


def test_incoming_finds_lazy_scalar_referrers(
    store: dc.Store, tsumeb_with_two_referrers: Locality
) -> None:
    """Given azurite and malachite lazily pointing at Tsumeb, committed
    When incoming(Tsumeb) is asked
    Then it names QA and QM
    """
    referrers = store.incoming(tsumeb_with_two_referrers)

    assert sorted(m.qid for m in referrers) == ["QA", "QM"]


def test_incoming_of_an_unreferenced_entity_is_empty(
    store: dc.Store, tsumeb_with_two_referrers: Locality
) -> None:
    """Given a committed quartz that references nothing and that nothing references
    When incoming(quartz) is asked
    Then it is empty
    """
    quartz = store.get(Mineral, qid="QQ")

    referrers = store.incoming(quartz)

    assert referrers == []


def test_incoming_through_an_eager_ref(store: dc.Store) -> None:
    """Given a collector whose eager home is a locality, committed
    When incoming(home) is asked
    Then it names the collector
    """
    home = Locality(qid="LH", name="home")
    _store_all(store, [home, Collector(name="collector", home=home)])
    store.commit()

    referrers = store.incoming(home)

    assert [x.name for x in referrers] == ["collector"]


@pytest.fixture
def collector_of_a_and_b(store: dc.Store) -> tuple[Mineral, Mineral]:
    """Commit minerals a and b and a collector whose lazy favorites list holds both."""
    a = Mineral(qid="A", name="a")
    b = Mineral(qid="B", name="b")
    collector = Collector(name="col", favorites=[dc.Lazy.of(a), dc.Lazy.of(b)])
    _store_all(store, [a, b, collector])
    store.commit()
    return a, b


def test_incoming_through_a_lazy_list_finds_the_first_element(
    store: dc.Store, collector_of_a_and_b: tuple[Mineral, Mineral]
) -> None:
    """Given a collector whose lazy favorites list holds a and b, committed
    When incoming(a) is asked
    Then it names the collector
    """
    a, _ = collector_of_a_and_b

    referrers = store.incoming(a)

    assert [x.name for x in referrers] == ["col"]


def test_incoming_through_a_lazy_list_finds_the_second_element(
    store: dc.Store, collector_of_a_and_b: tuple[Mineral, Mineral]
) -> None:
    """Given a collector whose lazy favorites list holds a and b, committed
    When incoming(b) is asked
    Then it names the collector
    """
    _, b = collector_of_a_and_b

    referrers = store.incoming(b)

    assert [x.name for x in referrers] == ["col"]


@pytest.fixture
def ten_minerals_over_three_localities(store: dc.Store) -> list[Locality]:
    """Commit L0..L2 and M0..M9, mineral i pointing at locality i modulo 3."""
    localities = _localities(3)
    _store_all(store, [*localities, *_minerals_at(localities, 10)])
    store.commit()
    return localities


def test_incoming_matches_the_brute_force_oracle_for_the_first_locality(
    store: dc.Store, ten_minerals_over_three_localities: list[Locality]
) -> None:
    """Given ten minerals over three localities, committed
    When incoming(L0) is asked
    Then it names M0, M3, M6 and M9
    """
    referrers = store.incoming(ten_minerals_over_three_localities[0])

    assert sorted(m.qid for m in referrers) == ["M0", "M3", "M6", "M9"]


def test_incoming_matches_the_brute_force_oracle_for_the_second_locality(
    store: dc.Store, ten_minerals_over_three_localities: list[Locality]
) -> None:
    """Given ten minerals over three localities, committed
    When incoming(L1) is asked
    Then it names M1, M4 and M7
    """
    referrers = store.incoming(ten_minerals_over_three_localities[1])

    assert sorted(m.qid for m in referrers) == ["M1", "M4", "M7"]


def test_incoming_matches_the_brute_force_oracle_for_the_third_locality(
    store: dc.Store, ten_minerals_over_three_localities: list[Locality]
) -> None:
    """Given ten minerals over three localities, committed
    When incoming(L2) is asked
    Then it names M2, M5 and M8
    """
    referrers = store.incoming(ten_minerals_over_three_localities[2])

    assert sorted(m.qid for m in referrers) == ["M2", "M5", "M8"]


@pytest.fixture
def indexed_loc_with_m1(store: dc.Store) -> tuple[Locality, Mineral]:
    """Commit a locality with referrer M1 and build the reverse index by asking incoming once."""
    loc = Locality(qid="L", name="loc")
    m1 = Mineral(qid="M1", name="m1", type_locality=dc.Lazy.of(loc))
    _store_all(store, [loc, m1])
    store.commit()
    store.incoming(loc)
    return loc, m1


def test_incoming_builds_its_index_on_first_use(store: dc.Store) -> None:
    """Given a locality with referrer M1, committed
    When incoming(locality) is asked for the first time
    Then it names M1
    """
    loc = Locality(qid="L", name="loc")
    _store_all(store, [loc, Mineral(qid="M1", name="m1", type_locality=dc.Lazy.of(loc))])
    store.commit()

    referrers = store.incoming(loc)

    assert sorted(m.qid for m in referrers) == ["M1"]


def test_incoming_folds_a_new_referrer_in_incrementally(
    store: dc.Store, indexed_loc_with_m1: tuple[Locality, Mineral]
) -> None:
    """Given a built reverse index with referrer M1
    When M2 pointing at the locality is committed
    Then incoming(locality) names M1 and M2
    """
    loc, _ = indexed_loc_with_m1
    store.store(Mineral(qid="M2", name="m2", type_locality=dc.Lazy.of(loc)))
    store.commit()

    referrers = store.incoming(loc)

    assert sorted(m.qid for m in referrers) == ["M1", "M2"]


def test_incoming_drops_a_re_pointed_referrer(
    store: dc.Store, indexed_loc_with_m1: tuple[Locality, Mineral]
) -> None:
    """Given a built reverse index with referrers M1 and M2
    When M1's type_locality is set to None and committed
    Then incoming(locality) names M2 only
    """
    loc, m1 = indexed_loc_with_m1
    store.store(Mineral(qid="M2", name="m2", type_locality=dc.Lazy.of(loc)))
    store.commit()
    m1.type_locality = None
    store.commit()

    referrers = store.incoming(loc)

    assert sorted(m.qid for m in referrers) == ["M2"]


def test_incoming_is_built_from_a_scan_after_reopen(store_factory: StoreFactory) -> None:
    """A fresh store builds the reverse index from a cold scan on the first incoming().

    Given a locality with referrer M committed, and the store reopened
    When incoming(locality) is asked
    Then it names M
    """
    store = store_factory()
    loc = Locality(qid="L", name="loc")
    _store_all(store, [loc, Mineral(qid="M", name="m", type_locality=dc.Lazy.of(loc))])
    store.commit()
    store.close()
    reopened = store_factory()
    reopened_loc = reopened.get(Locality, qid="L")

    referrers = reopened.incoming(reopened_loc)

    assert [x.qid for x in referrers] == ["M"]
    reopened.close()


def test_incoming_rejects_a_non_entity(store: dc.Store) -> None:
    """Given a store
    When incoming is asked about a plain dict
    Then NotAnEntityError is raised
    """
    with pytest.raises(dc.NotAnEntityError):
        store.incoming({"not": "an entity"})


def test_incoming_of_an_unstored_entity_is_empty(store: dc.Store) -> None:
    """Given a locality that was never stored
    When incoming(locality) is asked
    Then it is empty
    """
    referrers = store.incoming(Locality(qid="ghost", name="never stored"))

    assert referrers == []


# --- #20-B: delete-fold + rebuild equivalence -------------------------------


def test_incoming_drops_a_deleted_referrer(store: dc.Store) -> None:
    """The delete-fold removes the deleted mineral as a referrer.

    Given a built reverse index with referrers M1 and M2 of one locality
    When M1 is deleted and the store commits
    Then incoming(locality) names M2 only
    """
    loc = Locality(qid="L", name="loc")
    m1 = Mineral(qid="M1", name="m1", type_locality=dc.Lazy.of(loc))
    m2 = Mineral(qid="M2", name="m2", type_locality=dc.Lazy.of(loc))
    _store_all(store, [loc, m1, m2])
    store.commit()
    store.incoming(loc)
    store.delete(m1)
    store.commit()

    referrers = store.incoming(loc)

    assert sorted(m.qid for m in referrers) == ["M2"]


def test_incoming_on_a_deleted_target_names_the_dangling_referrers(store: dc.Store) -> None:
    """ADR-003: deleting a TARGET leaves its referrers dangling, the checked-delete seam.

    The locality's record is gone but m still points at its OID, and OIDs are never reused.

    Given a built reverse index where mineral M refers to a locality
    When the locality is deleted and the store commits
    Then incoming(locality) still names M
    """
    loc = Locality(qid="L", name="loc")
    m = Mineral(qid="M", name="m", type_locality=dc.Lazy.of(loc))
    _store_all(store, [loc, m])
    store.commit()
    store.incoming(loc)
    store.delete(loc)
    store.commit()

    referrers = store.incoming(loc)

    assert [x.qid for x in referrers] == ["M"]


@pytest.fixture
def repointed_and_deleted(store_factory: StoreFactory) -> tuple[StoreFactory, dc.Store]:
    """Three localities with nine minerals, then re-point M0 to L1 and delete M1, commit.

    The reverse index is built before the changes, so they are folded in incrementally.
    """
    store = store_factory()
    localities = _localities(3)
    minerals = _minerals_at(localities, 9)
    _store_all(store, [*localities, *minerals])
    store.commit()
    store.incoming(localities[0])
    minerals[0].type_locality = dc.Lazy.of(localities[1])
    store.delete(minerals[1])
    store.commit()
    return store_factory, store


def test_incremental_reverse_index_reflects_the_repoint_and_delete(
    repointed_and_deleted: tuple[StoreFactory, dc.Store],
) -> None:
    """Given M0 re-pointed to L1 and M1 deleted, with the index maintained incrementally
    When incoming is asked of each locality
    Then L0 has M3 and M6
    And L1 has M0, M4 and M7
    And L2 has M2, M5 and M8
    """
    _, store = repointed_and_deleted
    l0 = store.get(Locality, qid="L0")
    l1 = store.get(Locality, qid="L1")
    l2 = store.get(Locality, qid="L2")

    assert sorted(m.qid for m in store.incoming(l0)) == ["M3", "M6"]
    assert sorted(m.qid for m in store.incoming(l1)) == ["M0", "M4", "M7"]
    assert sorted(m.qid for m in store.incoming(l2)) == ["M2", "M5", "M8"]
    store.close()


def test_rebuilt_reverse_index_equals_the_incremental_one(
    repointed_and_deleted: tuple[StoreFactory, dc.Store],
) -> None:
    """Invariant 11: incremental == rebuilt.

    Given M0 re-pointed to L1 and M1 deleted, and the store reopened with a fresh IndexManager
    When incoming is asked of each locality from a cold scan
    Then L0 has M3 and M6
    And L1 has M0, M4 and M7
    And L2 has M2, M5 and M8
    """
    store_factory, store = repointed_and_deleted
    store.close()
    rebuilt = store_factory()
    l0 = rebuilt.get(Locality, qid="L0")
    l1 = rebuilt.get(Locality, qid="L1")
    l2 = rebuilt.get(Locality, qid="L2")

    assert sorted(m.qid for m in rebuilt.incoming(l0)) == ["M3", "M6"]
    assert sorted(m.qid for m in rebuilt.incoming(l1)) == ["M0", "M4", "M7"]
    assert sorted(m.qid for m in rebuilt.incoming(l2)) == ["M2", "M5", "M8"]
    rebuilt.close()


# --- #20-C: Snapshot.incoming() parity (ADR-002 read views) -----------------


@pytest.fixture
def loc_with_five_minerals(store: dc.Store) -> Locality:
    """Commit one locality with M0..M4 pointing at it."""
    loc = Locality(qid="L", name="loc")
    minerals = [
        Mineral(qid=f"M{i}", name=f"m{i}", type_locality=dc.Lazy.of(loc)) for i in range(5)
    ]
    _store_all(store, [loc, *minerals])
    store.commit()
    return loc


def test_live_store_incoming_names_all_five_referrers(
    store: dc.Store, loc_with_five_minerals: Locality
) -> None:
    """Given one locality with five referrers, committed
    When the live store is asked incoming(locality)
    Then it names M0 to M4
    """
    live = store.incoming(loc_with_five_minerals)

    assert sorted(m.qid for m in live) == ["M0", "M1", "M2", "M3", "M4"]


def test_snapshot_incoming_accepts_an_entity_view(
    store: dc.Store, loc_with_five_minerals: Locality
) -> None:
    """Given one locality with five referrers, committed
    When a snapshot is asked incoming(EntityView of the locality)
    Then it names M0 to M4
    """
    with store.snapshot() as snap:
        loc_view = snap.get(store.store(loc_with_five_minerals))

        by_view = snap.incoming(loc_view)

        assert sorted(v.qid for v in by_view) == ["M0", "M1", "M2", "M3", "M4"]


def test_snapshot_incoming_accepts_a_ref(store: dc.Store, loc_with_five_minerals: Locality) -> None:
    """Given one locality with five referrers, committed
    When a snapshot is asked incoming(dc.Ref of the locality)
    Then it names M0 to M4
    """
    with store.snapshot() as snap:
        by_ref = snap.incoming(dc.Ref(store.store(loc_with_five_minerals)))

        assert sorted(v.qid for v in by_ref) == ["M0", "M1", "M2", "M3", "M4"]


def test_snapshot_incoming_accepts_a_raw_oid(
    store: dc.Store, loc_with_five_minerals: Locality
) -> None:
    """Given one locality with five referrers, committed
    When a snapshot is asked incoming(raw oid of the locality)
    Then it names M0 to M4
    """
    with store.snapshot() as snap:
        by_oid = snap.incoming(store.store(loc_with_five_minerals))

        assert sorted(v.qid for v in by_oid) == ["M0", "M1", "M2", "M3", "M4"]


def test_snapshot_incoming_excludes_referrers_committed_later(store: dc.Store) -> None:
    """The pinned view, built lazily even now, sees only the referrers at its watermark.

    Given a snapshot taken when only M1 refers to the locality
    When M2 is committed after the snapshot
    Then the snapshot's incoming(locality) names M1 only
    """
    loc = Locality(qid="L", name="loc")
    m1 = Mineral(qid="M1", name="m1", type_locality=dc.Lazy.of(loc))
    _store_all(store, [loc, m1])
    store.commit()
    with store.snapshot() as snap:
        store.store(Mineral(qid="M2", name="m2", type_locality=dc.Lazy.of(loc)))
        store.commit()
        loc_view = snap.get(store.store(loc))

        pinned = snap.incoming(loc_view)

        assert [v.qid for v in pinned] == ["M1"]


def test_live_store_sees_referrers_committed_after_a_snapshot(store: dc.Store) -> None:
    """Given a snapshot taken when only M1 refers to the locality, and M2 committed later
    When the live store is asked incoming(locality)
    Then it names M1 and M2
    """
    loc = Locality(qid="L", name="loc")
    m1 = Mineral(qid="M1", name="m1", type_locality=dc.Lazy.of(loc))
    _store_all(store, [loc, m1])
    store.commit()
    with store.snapshot():
        store.store(Mineral(qid="M2", name="m2", type_locality=dc.Lazy.of(loc)))
        store.commit()

    live = store.incoming(loc)

    assert sorted(m.qid for m in live) == ["M1", "M2"]


@pytest.fixture
def deleted_locality_oid(store: dc.Store) -> int:
    """Commit M pointing at a locality, then delete the locality; return the dead oid."""
    loc = Locality(qid="L", name="loc")
    _store_all(store, [loc, Mineral(qid="M", name="m", type_locality=dc.Lazy.of(loc))])
    store.commit()
    loc_oid = store.store(loc)
    store.delete(loc)
    store.commit()
    return loc_oid


def test_snapshot_get_of_a_deleted_target_raises_dangling(
    store: dc.Store, deleted_locality_oid: int
) -> None:
    """ADR-003 seam at a watermark: a deleted TARGET keeps no record.

    Given a locality deleted and the deletion committed
    When a snapshot reads the dead oid
    Then DanglingRefError is raised
    """
    with store.snapshot() as snap, pytest.raises(dc.DanglingRefError):
        snap.get(deleted_locality_oid)


def test_snapshot_incoming_names_the_dangling_referrer_after_target_delete(
    store: dc.Store, deleted_locality_oid: int
) -> None:
    """Referrers still point at the dead OID, so incoming(dead) enumerates them, no record needed.

    Given a locality deleted and the deletion committed
    When a snapshot is asked incoming(dead oid)
    Then it names M
    """
    with store.snapshot() as snap:
        referrers = snap.incoming(deleted_locality_oid)

        assert [v.qid for v in referrers] == ["M"]


def test_snapshot_incoming_is_callable_from_a_foreign_thread(store: dc.Store) -> None:
    """Given a locality with referrer M, committed, and a snapshot
    When a foreign thread asks the snapshot incoming(locality oid)
    Then it names M
    """
    loc = Locality(qid="L", name="loc")
    _store_all(store, [loc, Mineral(qid="M", name="m", type_locality=dc.Lazy.of(loc))])
    store.commit()
    loc_oid = store.store(loc)
    with store.snapshot() as snap, ThreadPoolExecutor(max_workers=1) as pool:
        views = pool.submit(snap.incoming, loc_oid).result(timeout=10)

    assert [v.qid for v in views] == ["M"]
