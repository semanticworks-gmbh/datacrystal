"""``store.snapshot()``: frozen views at commit watermarks (M3, ADR-001 r2).

The M3 exit gate (KICKOFF §3): snapshots are readable from a thread pool
WHILE the owner commits, pin exactly one durable commit boundary, and hand
out immutable plain data — never live entities.

The last test fabricates a same-typename class dynamically (the
schema-evolution convention).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from types import MappingProxyType
from typing import BinaryIO, cast

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of
from datacrystal._snapshot import _VIEW_CHUNK, Snapshot, _SnapshotCore
from datacrystal._storage.protocol import BootInfo, StorageReadView, StoredBlob, StoredRecord
from tests.conftest import Locality, Mineral

MINERAL_T = "tests.conftest:Mineral"
M = dc.fields(Mineral)
StoreFactory = Callable[[], dc.Store]


@dc.entity
class LogbookPage:
    """Never committed anywhere — the unseen-type warning case."""

    heading: str


def _by_qid(views: Iterable[dc.EntityView]) -> dict[str, dc.EntityView]:
    return {view.qid: view for view in views}


def _qids(views: Iterable[dc.EntityView | None]) -> list[str]:
    return [view.qid for view in views if view is not None]


def _names(views: Iterable[dc.EntityView]) -> set[str]:
    return {view.name for view in views}


def _qid_set(views: Iterable[dc.EntityView]) -> set[str]:
    return {view.qid for view in views}


@pytest.fixture
def cabinet(store: dc.Store) -> dc.Store:
    """Return a store with quartz and azurite (typed at Tsumeb Mine) committed."""
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine", country="NA")
    store.root = [
        Mineral(
            qid="Q43010", name="quartz", crystal_system="trigonal", tags=["common", "piezoelectric"]
        ),
        Mineral(
            qid="Q193563",
            name="azurite",
            crystal_system="monoclinic",
            mohs=3.8,
            type_locality=dc.Lazy.of(tsumeb),
        ),
    ]
    store.commit()
    return store


@pytest.fixture
def snap(cabinet: dc.Store) -> Iterator[Snapshot]:
    """Yield a snapshot of the cabinet, closed after the test."""
    snapshot = cabinet.snapshot()
    yield snapshot
    snapshot.close()


@pytest.fixture
def quartz_view(snap: Snapshot) -> dc.EntityView:
    """Return the snapshot view of quartz."""
    return _by_qid(snap.all(Mineral))["Q43010"]


def _edit_after_snapshot(cabinet: dc.Store) -> Snapshot:
    """Snapshot the cabinet, then rename quartz and add topaz in a later commit."""
    snapshot = cabinet.snapshot()
    quartz = cabinet.get(Mineral, qid="Q43010")
    if quartz is None:
        pytest.fail("quartz is missing from the seeded cabinet")
    quartz.name = "Bergkristall"
    cabinet.store(Mineral(qid="Q7", name="topaz"))
    cabinet.commit()
    return snapshot


@pytest.fixture
def snap_before_later_edit(cabinet: dc.Store) -> Iterator[Snapshot]:
    """Yield a snapshot taken before quartz was renamed to Bergkristall and topaz added."""
    snapshot = _edit_after_snapshot(cabinet)
    yield snapshot
    snapshot.close()


@pytest.fixture
def snap_before_fluorite_and_quartz_delete(cabinet: dc.Store) -> Iterator[Snapshot]:
    """Yield a snapshot taken before fluorite was added and quartz deleted."""
    snapshot = cabinet.snapshot()
    cabinet.store(Mineral(qid="Q8", name="fluorite", crystal_system="cubic"))
    cabinet.delete(Mineral, qid="Q43010")
    cabinet.commit()
    yield snapshot
    snapshot.close()


# -- reading the committed state --------------------------------------------


def test_snapshot_pins_the_last_committed_tid(cabinet: dc.Store, snap: Snapshot) -> None:
    """Given a cabinet with two committed minerals
    When a snapshot is taken
    Then its tid is the store's last tid
    """
    assert snap.tid == cabinet.last_tid


def test_snapshot_lists_the_committed_minerals(snap: Snapshot) -> None:
    """Given a cabinet with quartz and azurite committed
    When all minerals are read from a snapshot
    Then they are Q43010 and Q193563
    """
    views = snap.all(Mineral)

    assert _qid_set(views) == {"Q43010", "Q193563"}


def test_snapshot_view_carries_the_field_values(snap: Snapshot) -> None:
    """Given a cabinet with azurite (Mohs 3.8) committed
    When azurite's view is read from a snapshot
    Then its name is azurite
    And its Mohs hardness is 3.8
    """
    azurite = _by_qid(snap.all(Mineral))["Q193563"]

    assert azurite.name == "azurite"
    assert azurite.mohs == 3.8


def test_snapshot_reference_is_an_explicit_ref_token(snap: Snapshot) -> None:
    """Given azurite typed at Tsumeb Mine
    When azurite's type_locality is read from a snapshot view
    Then it is a Ref token, not a live entity
    """
    azurite = _by_qid(snap.all(Mineral))["Q193563"]

    assert isinstance(azurite.type_locality, dc.Ref)


def test_snapshot_resolves_a_ref_token_through_the_snapshot(snap: Snapshot) -> None:
    """Given azurite typed at Tsumeb Mine
    When its type_locality Ref is resolved through the snapshot
    Then the locality is named Tsumeb Mine
    """
    azurite = _by_qid(snap.all(Mineral))["Q193563"]

    locality = snap.get(azurite.type_locality)

    assert locality.name == "Tsumeb Mine"


def test_snapshot_root_is_a_tuple_of_two_refs(snap: Snapshot) -> None:
    """Given a store root holding two minerals
    When the snapshot's root is read
    Then it is a tuple
    And it has two entries
    """
    root = snap.root

    assert isinstance(root, tuple)
    assert len(root) == 2


def test_snapshot_root_refs_resolve_to_the_root_minerals(snap: Snapshot) -> None:
    """Given a store root holding quartz and azurite
    When the snapshot's root refs are resolved
    Then they are Q43010 and Q193563
    """
    assert _qid_set(snap.get(ref) for ref in snap.root) == {"Q43010", "Q193563"}


# -- views are immutable plain data -----------------------------------------


def test_view_turns_lists_into_tuples(quartz_view: dc.EntityView) -> None:
    """Given quartz stored with the tags list ["common", "piezoelectric"]
    When its snapshot view's tags are read
    Then they are the tuple ("common", "piezoelectric")
    """
    assert quartz_view.tags == ("common", "piezoelectric")


def test_view_refuses_assignment(quartz_view: dc.EntityView) -> None:
    """Given a snapshot view of quartz
    When its name is assigned
    Then AttributeError says the view is read-only
    """
    with pytest.raises(AttributeError, match="read-only"):
        quartz_view.name = "rock crystal"


def test_view_refuses_deletion(quartz_view: dc.EntityView) -> None:
    """Given a snapshot view of quartz
    When its name is deleted
    Then AttributeError says the view is read-only
    """
    with pytest.raises(AttributeError, match="read-only"):
        del quartz_view.name


def test_view_refuses_an_unknown_field(quartz_view: dc.EntityView) -> None:
    """Given a snapshot view of quartz
    When the field color, which Mineral lacks, is read
    Then AttributeError says the view has no such field
    """
    with pytest.raises(AttributeError, match="no field"):
        _ = quartz_view.color


def test_view_fields_maps_names_to_values(quartz_view: dc.EntityView) -> None:
    """Given a snapshot view of quartz
    When its fields() are read
    Then the name entry is quartz
    """
    assert quartz_view.fields()["name"] == "quartz"


def test_view_fields_is_a_read_only_mapping(quartz_view: dc.EntityView) -> None:
    """Given a snapshot view of quartz
    When its fields() are read
    Then they are a MappingProxyType
    """
    assert isinstance(quartz_view.fields(), MappingProxyType)


def test_view_carries_the_typename(quartz_view: dc.EntityView) -> None:
    """Given a snapshot view of quartz
    When its typename is read
    Then it is "tests.conftest:Mineral"
    """
    assert quartz_view.typename == MINERAL_T


# -- isolation from later commits -------------------------------------------


def test_snapshot_keeps_its_watermark_after_later_commits(
    cabinet: dc.Store, snap_before_later_edit: Snapshot
) -> None:
    """Given a snapshot taken before a later commit
    When the later commit has landed
    Then the snapshot's tid is one below the store's last tid
    """
    assert snap_before_later_edit.tid == cabinet.last_tid - 1


def test_snapshot_still_sees_the_world_at_its_watermark(snap_before_later_edit: Snapshot) -> None:
    """Given a snapshot taken before quartz was renamed and topaz added
    When its minerals' names are read
    Then they are quartz and azurite
    """
    views = snap_before_later_edit.all(Mineral)

    assert _names(views) == {"quartz", "azurite"}


def test_fresh_snapshot_sees_the_later_commit(
    cabinet: dc.Store, snap_before_later_edit: Snapshot
) -> None:
    """Given quartz renamed and topaz added after an older snapshot
    When a fresh snapshot is taken
    Then its tid is the store's last tid
    And its mineral names are Bergkristall, azurite and topaz
    """
    with cabinet.snapshot() as fresh:
        assert fresh.tid == cabinet.last_tid
        assert _names(fresh.all(Mineral)) == {"Bergkristall", "azurite", "topaz"}


def test_snapshot_of_an_empty_store_is_empty(store: dc.Store) -> None:
    """Given a store with nothing committed
    When a snapshot is taken
    Then its tid is 0
    And its root is None
    And it holds no minerals
    And it knows no types
    """
    with store.snapshot() as snap:
        assert snap.tid == 0
        assert snap.root is None
        assert snap.all(Mineral) == []
        assert snap.types == ()


def test_snapshot_get_of_an_unknown_oid_raises(snap: Snapshot) -> None:
    """Given a snapshot of a cabinet
    When an oid with no record is fetched
    Then DataCrystalError says there is no record
    """
    with pytest.raises(dc.DataCrystalError, match="no record"):
        snap.get(1 << 50)


def test_closed_snapshot_refuses_reads(cabinet: dc.Store) -> None:
    """Given a snapshot that was closed twice
    When all minerals are read from it
    Then StoreClosedError names the snapshot
    """
    snap = cabinet.snapshot()
    snap.close()
    snap.close()

    with pytest.raises(dc.StoreClosedError, match="snapshot"):
        snap.all(Mineral)


def _fields_by_typename(types: Iterable[tuple[int, str, Iterable[str]]]) -> dict[str, Iterable[str]]:
    return {typename: fields for _, typename, fields in types}


def test_snapshot_types_expose_the_lineage_for_consumer_bootstrap(snap: Snapshot) -> None:
    """Given a snapshot of a cabinet of minerals
    When its types are read
    Then the Mineral row lists the qid field
    """
    rows = _fields_by_typename(snap.types)

    assert "qid" in rows[MINERAL_T]


def test_all_accepts_a_typename_string_for_engine_free_consumers(snap: Snapshot) -> None:
    """Given a snapshot of a cabinet of minerals
    When all() is asked for by the typename string
    Then it returns Q43010 and Q193563
    """
    views = snap.all(MINERAL_T)

    assert _qid_set(views) == {"Q43010", "Q193563"}


def test_all_refuses_something_that_is_neither_a_class_nor_a_typename(snap: Snapshot) -> None:
    """Given a snapshot of a cabinet of minerals
    When all() is asked for the number 42
    Then TypeError says it wants an entity class or a typename
    """
    with pytest.raises(TypeError, match="entity class or a typename"):
        snap.all(42)  # type: ignore[arg-type]


# -- index bitmaps views (M4, ADR-001 bound decision 4) ---------------------


@pytest.fixture
def mineral_indexes(snap: Snapshot) -> dc.SnapshotIndexes:
    """Return the snapshot's frozen index views for Mineral."""
    return snap.index_bitmaps(Mineral)


def test_index_bitmaps_view_is_a_snapshot_indexes(mineral_indexes: dc.SnapshotIndexes) -> None:
    """Given a snapshot of a cabinet of two minerals
    When the Mineral index views are asked for
    Then they are a SnapshotIndexes
    """
    assert isinstance(mineral_indexes, dc.SnapshotIndexes)


def test_index_bitmaps_extent_covers_every_mineral(mineral_indexes: dc.SnapshotIndexes) -> None:
    """Given a snapshot of a cabinet of two minerals
    When the Mineral index views are asked for
    Then the extent holds two oids
    """
    assert len(mineral_indexes.extent) == 2


def test_index_bitmaps_posting_holds_the_matching_mineral(
    snap: Snapshot, mineral_indexes: dc.SnapshotIndexes
) -> None:
    """Given a snapshot where only quartz is trigonal
    When the crystal_system posting for "trigonal" is read
    Then it holds one oid
    And that oid is quartz
    """
    postings = mineral_indexes.eq["crystal_system"]["trigonal"]

    assert len(postings) == 1
    (quartz_oid,) = postings
    assert snap.get(quartz_oid).qid == "Q43010"


def test_index_bitmaps_unique_map_points_into_the_extent(
    mineral_indexes: dc.SnapshotIndexes,
) -> None:
    """Given a snapshot of a cabinet of two minerals
    When the unique map entry for qid "Q193563" is read
    Then that oid is in the extent
    """
    azurite_oid = mineral_indexes.unique["qid"]["Q193563"]

    assert azurite_oid in mineral_indexes.extent


def test_index_bitmaps_extent_has_no_mutators(mineral_indexes: dc.SnapshotIndexes) -> None:
    """Given the frozen Mineral index views
    When an oid is added to the extent
    Then AttributeError is raised
    """
    with pytest.raises(AttributeError):
        mineral_indexes.extent.add(1)  # type: ignore[attr-defined]


def test_index_bitmaps_mappings_reject_writes(mineral_indexes: dc.SnapshotIndexes) -> None:
    """Given the frozen Mineral index views
    When a posting is assigned into the eq mapping
    Then TypeError is raised
    """
    with pytest.raises(TypeError):
        mineral_indexes.eq["crystal_system"]["cubic"] = None  # type: ignore[index]


def test_index_bitmaps_of_a_non_entity_raises(snap: Snapshot) -> None:
    """Given a snapshot of a cabinet
    When the index views of str are asked for
    Then NotAnEntityError is raised
    """
    with pytest.raises(dc.NotAnEntityError):
        snap.index_bitmaps(str)


# -- query and count over a snapshot ----------------------------------------


def test_snapshot_query_by_an_indexed_field_returns_views(snap: Snapshot) -> None:
    """Given a snapshot where only azurite is monoclinic
    When minerals of crystal_system monoclinic are queried
    Then azurite is returned
    """
    hits = snap.query(M.crystal_system == "monoclinic")

    assert _qids(hits) == ["Q193563"]


def test_snapshot_query_returns_entity_views(snap: Snapshot) -> None:
    """Given a snapshot where only azurite is monoclinic
    When minerals of crystal_system monoclinic are queried
    Then every hit is an EntityView
    """
    hits = snap.query(M.crystal_system == "monoclinic")

    assert all(isinstance(view, dc.EntityView) for view in hits)


def test_snapshot_query_evaluates_residuals_over_views(snap: Snapshot) -> None:
    """Given a snapshot where only azurite has Mohs hardness 3.8
    When minerals with Mohs at least 3.0 are queried
    Then azurite is returned
    """
    hits = snap.query(M.mohs >= 3.0)

    assert _qid_set(hits) == {"Q193563"}


def test_snapshot_query_plans_string_matching_on_index_keys(snap: Snapshot) -> None:
    """Given a snapshot where only quartz has a crystal system starting with "tri"
    When minerals whose crystal_system starts with "tri" are queried
    Then quartz is returned
    """
    hits = snap.query(M.crystal_system.startswith("tri"))

    assert _qid_set(hits) == {"Q43010"}


def test_snapshot_count_of_a_type_is_its_extent(snap: Snapshot) -> None:
    """Given a snapshot of a cabinet of two minerals
    When Mineral is counted
    Then the count is 2
    """
    assert snap.count(Mineral) == 2


def test_snapshot_count_by_an_indexed_field(snap: Snapshot) -> None:
    """Given a snapshot where only quartz is trigonal
    When minerals of crystal_system trigonal are counted
    Then the count is 1
    """
    assert snap.count(M.crystal_system == "trigonal") == 1


def test_snapshot_count_of_a_conjunction(snap: Snapshot) -> None:
    """Given a snapshot where only azurite is monoclinic with Mohs at least 3.0
    When such minerals are counted
    Then the count is 1
    """
    assert snap.count((M.crystal_system == "monoclinic") & (M.mohs >= 3.0)) == 1


def test_snapshot_query_of_the_bare_class_is_the_full_extent(snap: Snapshot) -> None:
    """Given a snapshot of a cabinet of two minerals
    When the bare class Mineral is queried
    Then as many views come back as Mineral counts
    """
    views = snap.query(Mineral)

    assert len(views) == snap.count(Mineral)


def test_snapshot_query_matches_a_lazy_handle_against_the_frozen_ref(
    cabinet: dc.Store, snap: Snapshot
) -> None:
    """Given azurite typed at Tsumeb Mine, queried with a live Lazy handle to Tsumeb
    When minerals whose type_locality equals that Lazy are queried on the snapshot
    Then azurite is returned
    """
    tsumeb = cabinet.get(Locality, qid="Q571997")

    hits = snap.query(M.type_locality == dc.Lazy.of(tsumeb))

    assert _qids(hits) == ["Q193563"]


def test_snapshot_query_matches_a_live_list_against_the_frozen_tuple(snap: Snapshot) -> None:
    """Given quartz stored with the tags ["common", "piezoelectric"]
    When minerals whose tags equal that list are queried on the snapshot
    Then quartz is returned, the list matching the frozen tuple
    """
    hits = snap.query(M.tags == ["common", "piezoelectric"])

    assert _qids(hits) == ["Q43010"]


def test_snapshot_count_ignores_later_commits(snap_before_fluorite_and_quartz_delete: Snapshot) -> None:
    """Given a snapshot taken before fluorite was added and quartz deleted
    When Mineral is counted on the snapshot
    Then the count is still 2
    """
    assert snap_before_fluorite_and_quartz_delete.count(Mineral) == 2


def test_snapshot_indexes_are_rebuilt_from_its_own_read_view(
    snap_before_fluorite_and_quartz_delete: Snapshot,
) -> None:
    """Given a snapshot taken before quartz was deleted
    When trigonal minerals are queried on the snapshot
    Then quartz is still returned
    """
    hits = snap_before_fluorite_and_quartz_delete.query(M.crystal_system == "trigonal")

    assert _qids(hits) == ["Q43010"]


def test_snapshot_does_not_see_a_later_added_mineral(
    snap_before_fluorite_and_quartz_delete: Snapshot,
) -> None:
    """Given a snapshot taken before fluorite was added
    When cubic minerals are queried on the snapshot
    Then none is returned
    """
    hits = snap_before_fluorite_and_quartz_delete.query(M.crystal_system == "cubic")

    assert hits == []


def test_fresh_snapshot_counts_the_added_and_the_deleted_mineral(
    cabinet: dc.Store, snap_before_fluorite_and_quartz_delete: Snapshot
) -> None:
    """Given fluorite added and quartz deleted after an older snapshot
    When a fresh snapshot counts Mineral
    Then the count is 2: one added, one deleted
    """
    with cabinet.snapshot() as fresh:
        assert fresh.count(Mineral) == 2


def test_fresh_snapshot_finds_the_added_mineral(
    cabinet: dc.Store, snap_before_fluorite_and_quartz_delete: Snapshot
) -> None:
    """Given fluorite added after an older snapshot
    When a fresh snapshot queries cubic minerals
    Then fluorite is returned
    """
    with cabinet.snapshot() as fresh:
        hits = fresh.query(M.crystal_system == "cubic")

        assert _qids(hits) == ["Q8"]


def test_snapshot_query_of_an_unseen_type_warns_and_finds_nothing(snap: Snapshot) -> None:
    """Given a snapshot that never saw a LogbookPage
    When logbook pages with heading "x" are queried
    Then UnseenTypeWarning says there are no committed records
    And nothing is returned
    """
    with pytest.warns(dc.UnseenTypeWarning, match="no committed records"):
        hits = snap.query(dc.fields(LogbookPage).heading == "x")

    assert hits == []


def test_snapshot_count_of_an_unseen_type_warns_and_is_zero(snap: Snapshot) -> None:
    """Given a snapshot that never saw a LogbookPage
    When logbook pages are counted
    Then UnseenTypeWarning is raised
    And the count is 0
    """
    with pytest.warns(dc.UnseenTypeWarning):
        counted = snap.count(LogbookPage)

    assert counted == 0


# -- concurrency ------------------------------------------------------------


class _MidCommitProbe:
    """A delta consumer that blocks the owner inside P3 of commit() on a pool-thread query."""

    def __init__(self, store: dc.Store, pool: ThreadPoolExecutor) -> None:
        self.watermark = store.last_tid
        self.result: tuple[int, list[str]] | None = None
        self._store = store
        self._pool = pool

    def apply(self, delta: dict) -> None:
        self.result = self._pool.submit(self._probe).result(timeout=30)
        self.watermark = delta["tid"]

    def _probe(self) -> tuple[int, list[str]]:
        with self._store.snapshot() as snap:
            hits = snap.query(M.crystal_system == "cubic")
            return snap.tid, sorted(view.qid for view in hits)


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    """Yield a one-thread pool, shut down after the test."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


def _commit_fluorite_while_a_pool_thread_queries(
    store: dc.Store, pool: ThreadPoolExecutor
) -> tuple[_MidCommitProbe, int | None]:
    probe = _MidCommitProbe(store, pool)
    store.attach(probe)
    store.store(Mineral(qid="Q8", name="fluorite", crystal_system="cubic"))
    return probe, store.commit()


def test_foreign_thread_snapshots_the_just_durable_commit_mid_commit(
    cabinet: dc.Store, pool: ThreadPoolExecutor
) -> None:
    """Given a consumer that blocks the owner inside P3 of commit() while a pool thread snapshots
    When fluorite is committed
    Then the pool thread's snapshot tid is the just-durable commit
    """
    probe, tid = _commit_fluorite_while_a_pool_thread_queries(cabinet, pool)

    assert probe.result is not None
    assert probe.result[0] == tid


def test_foreign_thread_bitmap_query_sees_the_commit_mid_commit(
    cabinet: dc.Store, pool: ThreadPoolExecutor
) -> None:
    """Given a consumer that blocks the owner inside P3 of commit() while a pool thread queries
    When fluorite is committed
    Then the pool thread's cubic query returns fluorite
    """
    probe, _ = _commit_fluorite_while_a_pool_thread_queries(cabinet, pool)

    assert probe.result is not None
    assert probe.result[1] == ["Q8"]


def _commit_storm_with_snapshot_readers(store: dc.Store, commits: int) -> list[tuple[int, int]]:
    """Commit one mineral per commit while pool threads snapshot; return (tid, minerals) pairs."""

    def read_consistent() -> tuple[int, int]:
        with store.snapshot() as snap:
            return snap.tid, len(snap.all(Mineral))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures: list[Future[tuple[int, int]]] = []
        for i in range(commits):
            store.store(Mineral(qid=f"Q{i}", name=f"specimen {i}"))
            store.commit()
            futures.append(pool.submit(read_consistent))
        return [future.result(timeout=30) for future in futures]


def test_thread_pool_snapshots_are_never_torn_during_a_commit_storm(store: dc.Store) -> None:
    """Given 30 commits that each add exactly one mineral, with pool threads snapshotting between
    When every snapshot's tid and mineral count are collected
    Then each snapshot holds exactly as many minerals as its tid
    """
    results = _commit_storm_with_snapshot_readers(store, 30)

    assert all(mineral_count == tid for tid, mineral_count in results)


# -- get_many: miss-tolerant batch read (#94, datacrystal[web] DataLoader) -----


def test_get_many_preserves_the_input_order(cabinet: dc.Store, snap: Snapshot) -> None:
    """Given the oids of azurite and quartz, in that order
    When they are batch-read from a snapshot
    Then the views come back as azurite, quartz
    """
    quartz = cabinet.get(Mineral, qid="Q43010")
    azurite = cabinet.get(Mineral, qid="Q193563")
    assert quartz is not None
    assert azurite is not None

    views = snap.get_many([cast("int", oid_of(azurite)), cast("int", oid_of(quartz))])

    assert _qids(views) == ["Q193563", "Q43010"]


@pytest.fixture
def live_and_deleted_oids(cabinet: dc.Store) -> tuple[int, int]:
    """Return the oids of quartz (live) and azurite (deleted and committed)."""
    quartz = cabinet.get(Mineral, qid="Q43010")
    azurite = cabinet.get(Mineral, qid="Q193563")
    if quartz is None or azurite is None:
        pytest.fail("the seeded cabinet lost a mineral")
    live_oid = cast("int", oid_of(quartz))
    gone_oid = cast("int", oid_of(azurite))
    cabinet.delete(azurite)
    cabinet.commit()
    return live_oid, gone_oid


def test_get_many_yields_none_in_the_slot_of_a_deleted_oid(
    snap_after_delete: Snapshot, live_and_deleted_oids: tuple[int, int]
) -> None:
    """Given the oids of a live and a deleted mineral
    When they are batch-read from a snapshot
    Then two slots come back
    And the deleted oid's slot is None, without raising (ADR-003 unchecked deletes)
    """
    live_oid, gone_oid = live_and_deleted_oids

    views = snap_after_delete.get_many([live_oid, gone_oid])

    assert len(views) == 2
    assert views[1] is None


@pytest.fixture
def snap_after_delete(live_and_deleted_oids: tuple[int, int], cabinet: dc.Store) -> Iterator[Snapshot]:
    """Yield a snapshot taken after azurite was deleted."""
    snapshot = cabinet.snapshot()
    yield snapshot
    snapshot.close()


def test_get_many_yields_the_view_in_the_slot_of_a_live_oid(
    snap_after_delete: Snapshot, live_and_deleted_oids: tuple[int, int]
) -> None:
    """Given the oids of a live and a deleted mineral
    When they are batch-read from a snapshot
    Then the live oid's slot is an EntityView
    And it is quartz
    """
    live_oid, gone_oid = live_and_deleted_oids

    views = snap_after_delete.get_many([live_oid, gone_oid])

    assert isinstance(views[0], dc.EntityView)
    assert views[0].qid == "Q43010"


def test_get_many_accepts_a_ref_and_an_entity_view_alike(snap: Snapshot) -> None:
    """Given a snapshot Ref to azurite and an already-materialized EntityView of quartz
    When they are batch-read together
    Then both minerals come back
    """
    ref0, ref1 = snap.root[0], snap.root[1]
    view0 = snap.get(ref0)

    out = snap.get_many([ref1, view0])

    assert _qid_set(view for view in out if view is not None) == {"Q43010", "Q193563"}


def test_get_many_of_nothing_returns_nothing(snap: Snapshot) -> None:
    """Given a snapshot of a cabinet
    When an empty list is batch-read
    Then an empty list comes back
    """
    assert snap.get_many([]) == []


class _CountingReadView:
    """Delegates to a real read view, counting ``load_many`` calls.

    It proves the round-trip budget (one ``load_many`` per ``_VIEW_CHUNK`` of misses, not N).
    Constructing a ``Snapshot`` over it also exercises the direct-construction /
    mid-life bootstrap path (ADR-002 read views).
    """

    def __init__(self, inner: StorageReadView) -> None:
        self._inner = inner
        self.load_calls = 0

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        self.load_calls += 1
        return self._inner.load_many(oids)

    def boot(self) -> BootInfo:
        return self._inner.boot()

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        return self._inner.scan_type(cid)

    def load_blob(self, oid: int) -> StoredBlob | None:
        return self._inner.load_blob(oid)

    def open_blob_stream(
        self, oid: int, on_close: Callable[[], None] | None = None
    ) -> BinaryIO:
        return self._inner.open_blob_stream(oid, on_close)

    def close(self) -> None:
        self._inner.close()


def _oids_of_root(store: dc.Store) -> list[int]:
    return [cast("int", oid_of(entity)) for entity in store.root]


@pytest.fixture
def counting_snapshot(cabinet: dc.Store) -> Iterator[tuple[Snapshot, _CountingReadView]]:
    """Yield a snapshot built directly over a counting read view of the cabinet."""
    counting = _CountingReadView(cabinet._backend.read_view())
    snapshot = Snapshot(_SnapshotCore(counting), dc.Principal(uid=0))
    yield snapshot, counting
    snapshot.close()


@pytest.fixture
def counting_snapshot_of_a_chunk_plus_five(
    store: dc.Store,
) -> Iterator[tuple[Snapshot, _CountingReadView, list[int]]]:
    """Yield a counting snapshot of _VIEW_CHUNK + 5 minerals, and their oids."""
    store.root = [Mineral(qid=f"Q{i}", name=f"specimen {i}") for i in range(_VIEW_CHUNK + 5)]
    store.commit()
    counting = _CountingReadView(store._backend.read_view())
    snapshot = Snapshot(_SnapshotCore(counting), dc.Principal(uid=0))
    yield snapshot, counting, _oids_of_root(store)
    snapshot.close()


def test_get_many_reads_all_chunks_in_one_batch_per_chunk(
    counting_snapshot_of_a_chunk_plus_five: tuple[Snapshot, _CountingReadView, list[int]],
) -> None:
    """Given a snapshot of one chunk plus five minerals, over a counting read view
    When all of them are batch-read
    Then every slot holds a view
    And the read view served 2 load_many round trips, never one per oid
    """
    snap, counting, oids = counting_snapshot_of_a_chunk_plus_five

    views = snap.get_many(oids)

    assert len(views) == _VIEW_CHUNK + 5
    assert all(view is not None for view in views)
    assert counting.load_calls == 2


def test_get_many_first_pass_loads_every_oid(
    cabinet: dc.Store, counting_snapshot: tuple[Snapshot, _CountingReadView]
) -> None:
    """Given a snapshot over a counting read view, nothing materialized yet
    When two oids are batch-read
    Then both slots hold a view
    And at least one load_many round trip happened
    """
    snap, counting = counting_snapshot

    first = snap.get_many(_oids_of_root(cabinet))

    assert all(view is not None for view in first)
    assert counting.load_calls >= 1


def test_get_many_second_pass_costs_no_further_round_trips(
    cabinet: dc.Store, counting_snapshot: tuple[Snapshot, _CountingReadView]
) -> None:
    """Given a snapshot where two oids were batch-read already
    When the same oids are batch-read again
    Then the same minerals come back
    And no further load_many round trip happened
    """
    snap, counting = counting_snapshot
    oids = _oids_of_root(cabinet)
    first = snap.get_many(oids)
    calls_after_first = counting.load_calls

    second = snap.get_many(oids)

    assert _qids(second) == _qids(first)
    assert counting.load_calls == calls_after_first


def _evolving_view(**fields: tuple[object, object]) -> type:
    """Redefine 'EvolvingView' with the given (annotation, default) fields, under one typename."""
    annotations: dict = {}
    namespace: dict = {
        "__module__": __name__,
        "__qualname__": "EvolvingView",
        "__annotations__": annotations,
    }
    for name, (annotation, default) in fields.items():
        annotations[name] = annotation
        if default is not ...:
            namespace[name] = default
    return dc.entity(type("EvolvingView", (), namespace))


def test_snapshot_decodes_an_old_lineage_row_with_new_field_defaults(
    store_factory: StoreFactory,
) -> None:
    """Given a record stored before the class gained a mohs field
    When the reopened store is snapshotted and the record read
    Then its name is quartz, decoded through its own persisted shape
    And its mohs is None, filled from the new field's default
    """
    v1 = _evolving_view(name=(str, ...))
    store = store_factory()
    store.store(v1(name="quartz"))
    store.commit()
    store.close()
    _evolving_view(name=(str, ...), mohs=(float | None, None))
    reopened = store_factory()

    with reopened.snapshot() as snap:
        (view,) = snap.all(f"{__name__}:EvolvingView")

        assert view.name == "quartz"
        assert view.mohs is None
    reopened.close()
