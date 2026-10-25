"""Fitness function #11 (memory envelope) — streaming-workload variant.

Datasets larger than RAM are handled by NOT pinning them: entities that are
not reachable from the (pinned) root are collectable the moment the caller
drops them, and rehydrate on demand. These gates hold that property down:

* streaming ingest leaves nothing live and stays under a per-object peak-RSS
  byte budget (measured in a child process via ``resource.getrusage`` —
  stdlib only, per the dependency budget);
* query results are collectable after release;
* a unique-key lookup hydrates exactly one entity, never the extent.

Budgets are byte counts (never wall-clock), with ≥2x headroom over the
locally measured cost (~750 B/obj peak incl. indexes and batch transients).
The full perf suite (KICKOFF §6: ``mem_bytes_per_object`` @1M nightly etc.)
lands with the benchmark milestone; these are its PR-cheap forerunners.
"""

from __future__ import annotations

import gc
import subprocess
import sys
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import datacrystal as dc
from tests.conftest import Mineral

_CHILD_PROBE = """
import gc, resource, sys
from typing import Annotated
import datacrystal as dc

def peak_bytes():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # linux: KB

@dc.entity
class Item:
    key: Annotated[str, dc.Unique]
    facet: Annotated[str, dc.Index]
    payload: str

N, BATCH = 100_000, 10_000
store = dc.Store.open(sys.argv[1], durability="interval")
baseline = peak_bytes()

for start in range(0, N, BATCH):
    for i in range(start, start + BATCH):
        store.store(Item(key=f"K{i:07d}", facet="ABCD"[i % 4], payload="x" * 64))
    store.commit()
gc.collect()
assert len(store._registry) == 0, (
    f"streaming ingest left {len(store._registry)} live entities"
)

hits = store.query(dc.fields(Item).facet == "A")
assert len(hits) == N // 4, len(hits)
del hits
gc.collect()
assert len(store._registry) == 0, "query results were not collectable"

per_obj = (peak_bytes() - baseline) / N
budget = 2048
assert per_obj <= budget, (
    f"streaming ingest+query peaked at {per_obj:.0f} B/obj (budget {budget})"
)
store.close()
print(f"MEM-OK {per_obj:.0f} B/obj peak")
"""


def weak_refs(minerals: list[Mineral]) -> list[weakref.ref[Mineral]]:
    """Return a weak reference to each mineral."""
    return [weakref.ref(mineral) for mineral in minerals]


def commit_cubic_minerals(store: dc.Store) -> list[weakref.ref[Mineral]]:
    """Commit 2000 cubic minerals and return weak references to them, holding none."""
    refs = []
    for i in range(2_000):
        mineral = Mineral(qid=f"Q{i}", name=f"m{i}", crystal_system="cubic")
        refs.append(weakref.ref(mineral))
        store.store(mineral)
    store.commit()
    return refs


@pytest.fixture
def cubic_cabinet(store: dc.Store) -> dc.Store:
    """Return a store holding 2000 committed cubic minerals, none of them live."""
    commit_cubic_minerals(store)
    return store


@pytest.fixture
def reopened_cabinet(store_factory: Callable[[], dc.Store]) -> Iterator[dc.Store]:
    """Yield a reopened store whose 1000 minerals were committed by a closed predecessor."""
    first = store_factory()
    for i in range(1_000):
        first.store(Mineral(qid=f"Q{i}", name=f"m{i}"))
    first.commit()
    first.close()
    reopened = store_factory()
    yield reopened
    reopened.close()


def test_streaming_ingest_and_query_stay_in_peak_memory_budget(tmp_path: Path) -> None:
    """Given a child process that streams 100k objects into a store
    When it ingests them, queries a quarter of them and releases the hits
    Then it finishes under the per-object peak-RSS budget with the registry empty between phases
    """
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_PROBE, str(tmp_path / "probe.store")],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "MEM-OK" in result.stdout, result.stdout


def test_committed_entities_are_not_kept_alive(store: dc.Store) -> None:
    """Given 2000 minerals stored and committed, with no strong reference held
    When the garbage collector has run
    Then every weak reference to them is dead
    """
    refs = commit_cubic_minerals(store)

    gc.collect()

    assert all(ref() is None for ref in refs)


def test_query_hits_are_live_while_held(cubic_cabinet: dc.Store) -> None:
    """Given 2000 committed cubic minerals
    When a query for the cubic crystal system is held
    Then the registry holds exactly the 2000 hits
    """
    hits = cubic_cabinet.query(dc.fields(Mineral).crystal_system == "cubic")

    assert len(hits) == 2_000
    assert len(cubic_cabinet._registry) == 2_000


def test_query_hits_are_collectable_after_release(cubic_cabinet: dc.Store) -> None:
    """Given a held query result of 2000 cubic minerals
    When the caller drops it and the garbage collector runs
    Then every weak reference to a hit is dead
    """
    hits = cubic_cabinet.query(dc.fields(Mineral).crystal_system == "cubic")
    hit_refs = weak_refs(hits)

    del hits
    gc.collect()

    assert all(ref() is None for ref in hit_refs)


def test_unique_get_returns_the_requested_mineral(reopened_cabinet: dc.Store) -> None:
    """Given a reopened store of 1000 minerals
    When the mineral with qid Q500 is fetched by its unique key
    Then it is m500
    """
    found = reopened_cabinet.get(Mineral, qid="Q500")

    assert found is not None
    assert found.name == "m500"


def test_unique_get_hydrates_exactly_one_entity(reopened_cabinet: dc.Store) -> None:
    """Given a reopened store of 1000 minerals
    When one mineral is fetched by its unique key
    Then the registry holds exactly that one entity, never the extent
    """
    reopened_cabinet.get(Mineral, qid="Q500")

    assert len(reopened_cabinet._registry) == 1
