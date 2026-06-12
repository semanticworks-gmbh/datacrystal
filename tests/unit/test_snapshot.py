"""``store.snapshot()``: frozen views at commit watermarks (M3, ADR-001 r2).

The M3 exit gate (KICKOFF §3): snapshots are readable from a thread pool
WHILE the owner commits, pin exactly one durable commit boundary, and hand
out immutable plain data — never live entities.

The last test fabricates a same-typename class dynamically (the
schema-evolution convention); the pragma below exists only for that.
"""
# pyright: reportCallIssue=false

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import MappingProxyType

import pytest

import datacrystal as dc
from tests.conftest import Locality, Mineral

MINERAL_T = "tests.conftest:Mineral"


def _seed(store: dc.Store) -> None:
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine", country="NA")
    store.root = [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal",
                tags=["common", "piezoelectric"]),
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic",
                mohs=3.8, type_locality=dc.Lazy.of(tsumeb)),
    ]
    store.commit()


def test_snapshot_reads_committed_state(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        assert snap.tid == store.last_tid
        minerals = {v.qid: v for v in snap.all(Mineral)}
        assert set(minerals) == {"Q43010", "Q193563"}
        azurite = minerals["Q193563"]
        assert azurite.name == "azurite" and azurite.mohs == 3.8
        # references are explicit Ref tokens, resolved via the snapshot
        locality = snap.get(azurite.type_locality)
        assert isinstance(azurite.type_locality, dc.Ref)
        assert locality.name == "Tsumeb Mine"
        # the root value mirrors store.root, as plain data
        root = snap.root
        assert isinstance(root, tuple) and len(root) == 2
        assert {snap.get(ref).qid for ref in root} == {"Q43010", "Q193563"}
    store.close()


def test_views_are_immutable_plain_data(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        quartz = next(v for v in snap.all(Mineral) if v.qid == "Q43010")
        assert quartz.tags == ("common", "piezoelectric")  # list → tuple
        with pytest.raises(AttributeError, match="read-only"):
            quartz.name = "rock crystal"
        with pytest.raises(AttributeError, match="read-only"):
            del quartz.name
        with pytest.raises(AttributeError, match="no field"):
            _ = quartz.color
        assert quartz.fields()["name"] == "quartz"
        assert isinstance(quartz.fields(), MappingProxyType)
        assert quartz.typename == MINERAL_T
    store.close()


def test_snapshot_is_isolated_from_later_commits(store_factory):
    store = store_factory()
    _seed(store)
    snap = store.snapshot()
    quartz = store.get(Mineral, qid="Q43010")
    quartz.name = "Bergkristall"
    store.store(Mineral(qid="Q7", name="topaz"))
    store.commit()

    # the snapshot still sees the world at its watermark
    assert snap.tid == store.last_tid - 1
    names = {v.name for v in snap.all(Mineral)}
    assert names == {"quartz", "azurite"}
    snap.close()

    with store.snapshot() as fresh:
        assert fresh.tid == store.last_tid
        assert {v.name for v in fresh.all(Mineral)} == {
            "Bergkristall", "azurite", "topaz"}
    store.close()


def test_snapshot_of_an_empty_store(store_factory):
    store = store_factory()
    with store.snapshot() as snap:
        assert snap.tid == 0
        assert snap.root is None
        assert snap.all(Mineral) == []
        assert snap.types == ()
    store.close()


def test_snapshot_get_unknown_oid_raises(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        with pytest.raises(dc.DataCrystalError, match="no record"):
            snap.get(1 << 50)
    store.close()


def test_closed_snapshot_refuses_reads(store_factory):
    store = store_factory()
    _seed(store)
    snap = store.snapshot()
    snap.close()
    snap.close()  # idempotent
    with pytest.raises(dc.StoreClosedError, match="snapshot"):
        snap.all(Mineral)
    store.close()


def test_snapshot_types_expose_the_lineage_for_consumer_bootstrap(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        rows = {typename: fields for _, typename, fields in snap.types}
        assert "qid" in rows[MINERAL_T]
    store.close()


def test_all_accepts_typename_string_for_engine_free_consumers(store_factory):
    store = store_factory()
    _seed(store)
    with store.snapshot() as snap:
        assert {v.qid for v in snap.all(MINERAL_T)} == {"Q43010", "Q193563"}
        with pytest.raises(TypeError, match="entity class or a typename"):
            snap.all(42)  # type: ignore[arg-type]
    store.close()


def test_index_bitmaps_slot_is_reserved_honestly(store_factory):
    store = store_factory()
    with store.snapshot() as snap:
        with pytest.raises(NotImplementedError, match="planned — M4"):
            snap.index_bitmaps()
    store.close()


def test_thread_pool_reads_snapshots_while_the_owner_commits(store_factory):
    """The M3 exit criterion: every snapshot a foreign thread takes during a
    commit storm is internally consistent — each commit adds exactly one
    mineral, so a snapshot at watermark T must contain exactly T minerals."""
    store = store_factory()

    def read_consistent(_: int) -> tuple[int, int]:
        with store.snapshot() as snap:
            return snap.tid, len(snap.all(Mineral))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for i in range(30):
            store.store(Mineral(qid=f"Q{i}", name=f"specimen {i}"))
            store.commit()
            futures.append(pool.submit(read_consistent, i))
        results = [f.result(timeout=30) for f in futures]

    for tid, mineral_count in results:
        assert mineral_count == tid  # one mineral per commit, never torn
    store.close()


def test_snapshot_decodes_old_lineage_rows_with_defaults(store_factory):
    """Snapshots follow the same additive-evolution rules as live hydration:
    old records decode by name through their own persisted shape, fields the
    live class added are filled from defaults (KICKOFF invariant 8)."""
    def evolve(**fields):
        annotations = {}
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

    v1 = evolve(name=(str, ...))
    store = store_factory()
    store.store(v1(name="quartz"))
    store.commit()
    store.close()

    evolve(name=(str, ...), mohs=(float | None, None))  # the "code changed"
    reopened = store_factory()
    with reopened.snapshot() as snap:
        (view,) = snap.all(f"{__name__}:EvolvingView")
        assert view.name == "quartz"
        assert view.mohs is None  # filled from the new field's default
    reopened.close()
