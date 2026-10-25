"""Fitness gate #19 (KICKOFF, 2026-06-12): indexed reads cost f(hits), never
f(extent).

The MaStR import showed the failure mode at 5.4M rows: reads that should be
index-shaped degenerating into extent-shaped scans. This gate pins the
*shape*, not the speed (invariant 12 — op counts, never wall-clock): the same
fixed-hit-count workload runs at two extents 4× apart and every indexed
operation must cost exactly the same number of record loads at both. What
kills you at millions of rows is accidental O(extent), not constants — if the
shape holds at 25k/100k it holds at 5M.

The residual cliff (any non-``==``/``in_`` predicate scans the extent) is
pinned too — as the documented cost it is, so it can never silently grow
worse, and with zero entity constructions (the decode-level read path).
"""

from __future__ import annotations

import gc
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import StoredRecord
from datacrystal._store import _RAW_CHUNK
from tests.fitness._counting_backend import CountingBackend


@dc.entity
class GateSpecimen:
    """A specimen with a unique key, an indexed crystal system and an unindexed hardness."""

    qid: Annotated[str, dc.Unique]
    crystal_system: Annotated[str | None, dc.Index] = None
    mohs: float | None = None


class ScanCountingBackend(CountingBackend):
    """A counting backend that also counts the ``scan_type`` calls an index build costs."""

    def __init__(self, inner: MemoryBackend) -> None:
        super().__init__(inner)
        self.scans = 0

    def reset(self) -> None:
        """Zero the load counters and the scan counter."""
        super().reset()
        self.scans = 0

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        """Count the scan, then delegate."""
        self.scans += 1
        return super().scan_type(cid)


HITS = 500  # fixed across both extents: only the haystack grows
SMALL = 25_000  # the other extent, 100_000, is 4x larger


@dataclass
class Cabinet:
    """A committed store of ``extent`` specimens, 500 of them cubic, over a counting backend."""

    store: dc.Store
    backend: ScanCountingBackend
    extent: int
    mohs_nine_and_up: int
    live_baseline: int


def _grow(store: dc.Store, upto: int) -> None:
    for i in range(upto):
        system = "cubic" if i < HITS else f"sys-{i % 7}"
        store.store(GateSpecimen(qid=f"Q{i}", crystal_system=system, mohs=float(i % 10)))
    store.commit()


def _open_cabinet(extent: int, mohs_nine_and_up: int) -> Cabinet:
    backend = ScanCountingBackend(MemoryBackend())
    store = dc.Store._from_backend(backend)
    _grow(store, extent)
    store.count(GateSpecimen)  # force the one-time index build (scan_type)
    gc.collect()
    backend.reset()
    return Cabinet(store, backend, extent, mohs_nine_and_up, len(store._registry))


def _qids(count: int) -> list[str]:
    return [f"Q{i}" for i in range(count)]


@pytest.fixture(params=[(25_000, 2_500), (100_000, 10_000)], ids=["25k", "100k"])
def cabinet(request: pytest.FixtureRequest) -> Iterator[Cabinet]:
    """Yield a cabinet at each of the two extents 4x apart."""
    extent, mohs_nine_and_up = request.param
    opened = _open_cabinet(extent, mohs_nine_and_up)
    yield opened
    opened.store.close()


GROWN_CUBIC = 100  # specimens added after the index exists, on top of the 500 already cubic
GROWN_OTHER = 100


@pytest.fixture
def grown_cabinet(cabinet: Cabinet) -> Cabinet:
    """Grow the cabinet by 200 specimens, 100 of them cubic, once its index is built."""
    for i in range(GROWN_CUBIC):
        cabinet.store.store(GateSpecimen(qid=f"N{i}", crystal_system="cubic", mohs=1.0))
    for i in range(GROWN_OTHER):
        cabinet.store.store(GateSpecimen(qid=f"M{i}", crystal_system="trigonal", mohs=1.0))
    cabinet.store.commit()
    return cabinet


@pytest.fixture
def small_cabinet() -> Iterator[Cabinet]:
    """Yield a cabinet of 25_000 specimens, more than three raw chunks of 8192."""
    opened = _open_cabinet(SMALL, 2_500)
    yield opened
    opened.store.close()


# What kills you at millions of rows is accidental O(extent), not constants: every indexed
# operation costs the same number of record loads at both extents.


def test_count_of_the_extent_loads_no_record(cabinet: Cabinet) -> None:
    """Given a cabinet of specimens at either extent
    When the specimens are counted
    Then the count is the extent
    And no record was loaded
    """
    total = cabinet.store.count(GateSpecimen)

    assert total == cabinet.extent
    assert cabinet.backend.records_loaded == 0


def test_count_of_indexed_hits_loads_no_record(cabinet: Cabinet) -> None:
    """Given a cabinet of specimens at either extent
    When the cubic specimens are counted
    Then the count is 500
    And no record was loaded, as cardinality comes from the index alone
    """
    cubic = cabinet.store.count(dc.fields(GateSpecimen).crystal_system == "cubic")

    assert cubic == 500
    assert cabinet.backend.records_loaded == 0


def test_indexed_query_loads_exactly_the_hits(cabinet: Cabinet) -> None:
    """Given a cabinet of specimens at either extent
    When the cubic specimens are queried
    Then 500 hits are returned
    And exactly 500 records were loaded, never the extent
    """
    hits = cabinet.store.query(dc.fields(GateSpecimen).crystal_system == "cubic")

    assert len(hits) == 500
    assert cabinet.backend.records_loaded == 500


def test_pluck_loads_the_hits_only(cabinet: Cabinet) -> None:
    """Given a cabinet of specimens at either extent
    When the qids of the cubic specimens are plucked
    Then 500 qids are returned
    And exactly 500 records were loaded
    """
    qids = cabinet.store.pluck(dc.fields(GateSpecimen).crystal_system == "cubic", "qid")

    assert len(qids) == 500
    assert cabinet.backend.records_loaded == 500


def test_pluck_constructs_no_live_entity(cabinet: Cabinet) -> None:
    """Given a cabinet of specimens at either extent
    When the qids of the cubic specimens are plucked and the collector runs
    Then the registry holds as many live entities as before, as pluck stays decode-level
    """
    cabinet.store.pluck(dc.fields(GateSpecimen).crystal_system == "cubic", "qid")
    gc.collect()

    assert len(cabinet.store._registry) == cabinet.live_baseline


def test_bulk_unique_get_many_is_one_round_trip(cabinet: Cabinet) -> None:
    """Given a cabinet of specimens at either extent
    When 100 specimens are fetched by unique key in one get_many
    Then all 100 are found
    And one load_many call served them, so there is no N+1
    And exactly 100 records were loaded
    """
    found = cabinet.store.get_many(GateSpecimen, qid=_qids(100))

    assert all(specimen is not None for specimen in found)
    assert cabinet.backend.load_calls == 1
    assert cabinet.backend.records_loaded == 100


def test_startswith_count_on_an_indexed_field_loads_no_record(cabinet: Cabinet) -> None:
    """Given a cabinet of specimens at either extent
    When the specimens whose crystal system starts with "cub" are counted
    Then the count is 500
    And no record was loaded, as the index keys' postings are ORed
    """
    cubic = cabinet.store.count(dc.fields(GateSpecimen).crystal_system.startswith("cub"))

    assert cubic == 500
    assert cabinet.backend.records_loaded == 0


def test_contains_query_on_an_indexed_field_loads_exactly_the_hits(cabinet: Cabinet) -> None:
    """Given a cabinet of specimens at either extent
    When the specimens whose crystal system contains "ubi" are queried
    Then 500 hits are returned
    And exactly 500 records were loaded, as only the distinct index keys are scanned
    """
    hits = cabinet.store.query(dc.fields(GateSpecimen).crystal_system.contains("ubi"))

    assert len(hits) == 500
    assert cabinet.backend.records_loaded == 500


# The residual cliff, pinned at its documented cost: a non-indexed predicate scans the extent
# (never more than the extent, and with zero entity constructions).


def test_unindexed_count_scans_exactly_the_extent(cabinet: Cabinet) -> None:
    """Given a cabinet of specimens at either extent
    When the specimens with mohs 9 or more are counted
    Then a tenth of the extent matches
    And every record of the extent was loaded once
    """
    hard = cabinet.store.count(dc.fields(GateSpecimen).mohs >= 9.0)

    assert hard == cabinet.mohs_nine_and_up
    assert cabinet.backend.records_loaded == cabinet.extent


def test_unindexed_count_constructs_no_live_entity(cabinet: Cabinet) -> None:
    """Given a cabinet of specimens at either extent
    When the specimens with mohs 9 or more are counted and the collector runs
    Then the registry holds as many live entities as before
    """
    cabinet.store.count(dc.fields(GateSpecimen).mohs >= 9.0)
    gc.collect()

    assert len(cabinet.store._registry) == cabinet.live_baseline


# Commit-time index maintenance: a store that keeps growing after its index is built updates the
# index in the commit, never by rebuilding it from a scan of the extent.


def test_growing_a_built_index_triggers_no_rebuild_scan(grown_cabinet: Cabinet) -> None:
    """Given a cabinet whose index is built, grown by 200 specimens in one commit
    When the cubic specimens are counted afterwards
    Then the count includes the 100 new cubic specimens
    And no scan of the extent rebuilt the index
    """
    cubic = grown_cabinet.store.count(dc.fields(GateSpecimen).crystal_system == "cubic")

    assert cubic == HITS + GROWN_CUBIC
    assert grown_cabinet.backend.scans == 0


def test_indexed_query_after_growth_loads_exactly_the_hits(grown_cabinet: Cabinet) -> None:
    """Given a cabinet whose index is built, grown by 200 specimens in one commit
    When the cubic specimens are queried
    Then 600 hits are returned
    And exactly 600 records were loaded, never the grown extent
    """
    grown_cabinet.backend.reset()

    hits = grown_cabinet.store.query(dc.fields(GateSpecimen).crystal_system == "cubic")

    assert len(hits) == HITS + GROWN_CUBIC
    assert grown_cabinet.backend.records_loaded == HITS + GROWN_CUBIC


def test_unique_lookup_of_a_specimen_added_after_the_build_loads_one_record(
    grown_cabinet: Cabinet,
) -> None:
    """Given a cabinet whose index is built, grown by 200 specimens in one commit
    When one new specimen is fetched by its unique qid
    Then it is found
    And exactly one record was loaded
    """
    grown_cabinet.backend.reset()

    found = grown_cabinet.store.get(GateSpecimen, qid="N7")

    assert found is not None
    assert grown_cabinet.backend.records_loaded == 1


def test_recrystallised_specimen_moves_between_index_postings_without_a_scan(
    grown_cabinet: Cabinet,
) -> None:
    """Given a cabinet whose index is built, grown by 200 specimens in one commit
    When one of the new cubic specimens is changed to trigonal and committed
    Then the cubic count drops by one
    And no scan of the extent rebuilt the index
    """
    grown_cabinet.backend.reset()
    moved = grown_cabinet.store.get(GateSpecimen, qid="N7")
    assert moved is not None
    moved.crystal_system = "trigonal"
    grown_cabinet.store.commit()

    cubic = grown_cabinet.store.count(dc.fields(GateSpecimen).crystal_system == "cubic")

    assert cubic == HITS + GROWN_CUBIC - 1
    assert grown_cabinet.backend.scans == 0


def test_limit_stops_a_query_early(small_cabinet: Cabinet) -> None:
    """Given 25_000 specimens of which 500 are cubic
    When the cubic specimens are queried with limit 10
    Then 10 hits are returned
    And exactly 10 records were loaded, as the limit slices OIDs before hydration (#14)
    """
    hits = small_cabinet.store.query(dc.fields(GateSpecimen).crystal_system == "cubic", limit=10)

    assert len(hits) == 10
    assert small_cabinet.backend.records_loaded == 10


def test_limit_stops_a_pluck_early(small_cabinet: Cabinet) -> None:
    """Given 25_000 specimens of which 500 are cubic
    When the qids of the cubic specimens are plucked with limit 10
    Then 10 qids are returned
    And exactly 10 records were decoded
    """
    qids = small_cabinet.store.pluck(
        dc.fields(GateSpecimen).crystal_system == "cubic", "qid", limit=10
    )

    assert len(qids) == 10
    assert small_cabinet.backend.records_loaded == 10


def test_query_iter_yields_every_specimen(small_cabinet: Cabinet) -> None:
    """Given 25_000 specimens
    When the whole extent is streamed through query_iter
    Then 25_000 specimens are yielded
    """
    streamed = list(small_cabinet.store.query_iter(GateSpecimen))

    assert len(streamed) == 25_000


@pytest.mark.parametrize("consumed", [5_000, 10_000, 15_000, 20_000, 25_000])
def test_query_iter_keeps_a_bounded_live_set(small_cabinet: Cabinet, consumed: int) -> None:
    """Given 25_000 specimens streamed through query_iter and dropped as they come
    When a number of them have been consumed and the collector runs
    Then at most two raw chunks of entities are live, never the extent (#15)
    """
    stream = small_cabinet.store.query_iter(GateSpecimen)

    deque(islice(stream, consumed), maxlen=0)
    gc.collect()

    assert len(small_cabinet.store._registry) <= 2 * _RAW_CHUNK
