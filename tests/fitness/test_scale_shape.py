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
from typing import Annotated

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend


@dc.entity
class GateSpecimen:
    qid: Annotated[str, dc.Unique]
    crystal_system: Annotated[str | None, dc.Index] = None
    mohs: float | None = None


class CountingBackend:
    """Storage wrapper counting load_many traffic (the KICKOFF 'counting
    storage wrapper' pattern; index builds go through scan_type and are
    deliberately not counted — they are the documented one-time cost)."""

    def __init__(self, inner: MemoryBackend) -> None:
        self._inner = inner
        self.load_calls = 0
        self.records_loaded = 0

    def reset(self) -> None:
        self.load_calls = 0
        self.records_loaded = 0

    def boot(self):
        return self._inner.boot()

    def load_many(self, oids):
        out = self._inner.load_many(oids)
        self.load_calls += 1
        self.records_loaded += len(out)
        return out

    def scan_type(self, cid):
        return self._inner.scan_type(cid)

    def apply(self, batch):
        return self._inner.apply(batch)

    def read_view(self):
        return self._inner.read_view()

    def close(self):
        self._inner.close()


HITS = 500          # fixed across both extents: only the haystack grows
SMALL, LARGE = 25_000, 100_000  # 4× apart


def _grow(store: dc.Store, start: int, upto: int) -> None:
    for i in range(start, upto):
        system = "cubic" if i < HITS else f"sys-{i % 7}"
        store.store(GateSpecimen(qid=f"Q{i}", crystal_system=system,
                                 mohs=float(i % 10)))
    store.commit()


def test_indexed_reads_scale_with_hits_not_extent():
    backend = CountingBackend(MemoryBackend())
    store = dc.Store._from_backend(backend)
    S = dc.fields(GateSpecimen)

    grown = 0
    for extent in (SMALL, LARGE):
        _grow(store, grown, extent)
        grown = extent
        store.count(GateSpecimen)  # force the one-time index build (scan_type)
        gc.collect()
        live_baseline = len(store._registry)

        # count(): cardinality answers — ZERO record loads, both extents.
        backend.reset()
        assert store.count(GateSpecimen) == extent
        assert store.count(S.crystal_system == "cubic") == HITS
        assert backend.records_loaded == 0, (
            f"count() loaded {backend.records_loaded} records at extent "
            f"{extent} — cardinality must come from the index alone"
        )

        # query() on an indexed predicate: loads exactly |hits|, never extent.
        backend.reset()
        hits = store.query(S.crystal_system == "cubic")
        assert len(hits) == HITS
        assert backend.records_loaded == HITS, (
            f"indexed query loaded {backend.records_loaded} records for "
            f"{HITS} hits at extent {extent} — extent-shaped scan detected"
        )
        del hits
        gc.collect()

        # pluck(): loads |hits|, constructs ZERO entities.
        backend.reset()
        names = store.pluck(S.crystal_system == "cubic", "qid")
        assert len(names) == HITS
        assert backend.records_loaded == HITS
        gc.collect()
        assert len(store._registry) == live_baseline, (
            "pluck() constructed live entities — it must stay decode-level"
        )

        # bulk unique-key get_many(): one round trip, loads == keys.
        backend.reset()
        got = store.get_many(GateSpecimen, qid=[f"Q{i}" for i in range(100)])
        assert all(entity is not None for entity in got)
        assert backend.load_calls == 1, "bulk key lookup must batch (no N+1)"
        assert backend.records_loaded == 100
        del got
        gc.collect()

        # contains/startswith on an indexed field iterate DISTINCT index
        # keys (8 here), never records: count() stays at zero loads and
        # query() at exactly |hits| — at both extents.
        backend.reset()
        assert store.count(S.crystal_system.startswith("cub")) == HITS
        assert backend.records_loaded == 0, (
            f"indexed startswith count loaded {backend.records_loaded} "
            f"records at extent {extent} — it must OR index-key postings"
        )
        hits = store.query(S.crystal_system.contains("ubi"))
        assert len(hits) == HITS
        assert backend.records_loaded == HITS
        del hits
        gc.collect()

        # The residual cliff, pinned at its documented cost: a non-indexed
        # predicate scans the extent (never MORE than the extent, and with
        # zero entity constructions).
        backend.reset()
        assert store.count(S.mohs >= 9.0) == extent // 10
        assert backend.records_loaded == extent
        gc.collect()
        assert len(store._registry) == live_baseline

    store.close()


def test_limit_stops_early_on_indexed_read():
    # #14: a windowed no-residual read slices OIDs before hydration, so it loads
    # at most `limit` records — never the full hit set, never the extent.
    backend = CountingBackend(MemoryBackend())
    store = dc.Store._from_backend(backend)
    _grow(store, 0, SMALL)
    store.count(GateSpecimen)  # one-time index build
    gc.collect()  # evict the non-root-reachable specimens from the weak registry
    S = dc.fields(GateSpecimen)

    backend.reset()
    hits = store.query(S.crystal_system == "cubic", limit=10)
    assert len(hits) == 10
    assert backend.records_loaded == 10, (
        f"windowed query loaded {backend.records_loaded}, expected 10 — limit "
        "must slice OIDs before get_many"
    )
    del hits
    gc.collect()

    backend.reset()
    names = store.pluck(S.crystal_system == "cubic", "qid", limit=10)
    assert len(names) == 10
    assert backend.records_loaded == 10, (
        f"windowed pluck decoded {backend.records_loaded}, expected 10"
    )
    store.close()


def test_iter_streams_with_bounded_live_set():
    # #15: streaming a full-extent query keeps O(chunk) entities live, never
    # O(extent) — the registry must not accumulate the whole result set.
    from datacrystal._store import _RAW_CHUNK

    backend = CountingBackend(MemoryBackend())
    store = dc.Store._from_backend(backend)
    _grow(store, 0, SMALL)  # 25_000 > 3 chunks of 8192
    gc.collect()

    peak = 0
    seen = 0
    for obj in store.iter(GateSpecimen):
        seen += 1
        if seen % 1000 == 0:
            gc.collect()
            peak = max(peak, len(store._registry))
        del obj
    assert seen == SMALL
    assert peak <= 2 * _RAW_CHUNK, (
        f"iter() held {peak} live entities at extent {SMALL} (chunk "
        f"{_RAW_CHUNK}) — a streaming read must stay O(chunk), not O(extent)"
    )
    store.close()
