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
store = dc.Store.open(sys.argv[1], durability="relaxed")
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


def test_streaming_ingest_and_query_stay_in_budget(tmp_path):
    """Child process: 100k objects ingested + queried under a peak-RSS
    byte budget, with the registry empty between phases."""
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_PROBE, str(tmp_path / "probe.store")],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert "MEM-OK" in result.stdout, result.stdout


def test_query_results_are_collectable(store_factory):
    store = store_factory()
    for i in range(2_000):
        store.store(Mineral(qid=f"Q{i}", name=f"m{i}", crystal_system="cubic"))
    store.commit()
    gc.collect()
    assert len(store._registry) == 0

    hits = store.query(Mineral.crystal_system == "cubic")
    assert len(hits) == 2_000
    assert len(store._registry) == 2_000
    del hits
    gc.collect()
    assert len(store._registry) == 0
    store.close()


def test_unique_get_hydrates_exactly_one(store_factory):
    """Searching by unique key must never materialize the extent."""
    store = store_factory()
    for i in range(1_000):
        store.store(Mineral(qid=f"Q{i}", name=f"m{i}"))
    store.commit()
    store.close()

    reopened = store_factory()
    found = reopened.get(Mineral, qid="Q500")
    assert found is not None and found.name == "m500"
    assert len(reopened._registry) == 1
    reopened.close()
