"""PR-cadence perf gates (KICKOFF §6 benchmark table, the same-run subset).

Each test is one row of the KICKOFF table at PR scale: a ratio between the
engine and a floor measured in the same run with the same harness, or a
shape ratio between two extents. Nightly absolutes/trends and the
psutil-based memory gates land with the nightly lane (KICKOFF: gates
harden after 14 green nights; until then breaches warn, 3× fails).
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from pathlib import Path
from statistics import median
from typing import Any

import msgspec
import pytest

import datacrystal as dc
from benchmarks import _gen
from benchmarks.conftest import (
    LARGE_SHAPE_EXTENTS,
    SMALL_SPECIMENS,
    SPECIMENS,
    gate,
    time_it,
)
from datacrystal._pipeline import DeltaConsumer
from datacrystal._records import decode_payload
from datacrystal._snapshot import Snapshot


def _decode_loop_floor(keys: list[str]) -> Callable[[], None]:
    @dataclasses.dataclass(slots=True)
    class PlainSpecimen:
        specimen_no: str
        quality: str
        mass_g: float
        mineral: object
        acquired_from: object

    raw_payloads = [msgspec.msgpack.encode([k, "A", 123.4, None, None]) for k in keys]

    def floor_run() -> None:
        for payload in raw_payloads:
            PlainSpecimen(*decode_payload(payload))

    return floor_run


def _commit_tput_small_ratio(tmp_path: Path) -> float:
    """Time the engine's commits against the sqlite floor; return engine / floor."""
    rounds, batch = 30, 50
    store = dc.Store.open(tmp_path / "engine.store")
    field_lists = [[f"T-{i:07d}", "A", 12.5, None, None] for i in range(batch)]
    counter = iter(range(10_000_000))
    minerals = [_gen.Mineral(qid="QX0001", name="quartz", crystal_system="trigonal")]

    def engine_run() -> None:
        for _ in range(rounds):
            for _ in range(batch):
                i = next(counter)
                store.store(_gen.Specimen(
                    specimen_no=f"T-{i:07d}",
                    mineral=dc.Lazy.of(minerals[0]),
                    quality="A", mass_g=12.5,
                ))
            store.commit()

    store.root = minerals
    store.commit()

    floor_conn = sqlite3.connect(tmp_path / "floor.sqlite")
    floor_conn.execute("PRAGMA journal_mode=WAL")
    floor_conn.execute("PRAGMA synchronous=NORMAL")
    floor_conn.execute(
        "CREATE TABLE objects (oid INTEGER PRIMARY KEY, cid INTEGER, "
        "tid INTEGER, payload BLOB, crc INTEGER)"
    )
    encode = msgspec.msgpack.Encoder().encode

    def floor_run() -> None:
        # floor parity (KICKOFF): msgspec ENCODE + executemany, identical
        # txn boundaries — encoding happens inside the timed region too
        for r in range(rounds):
            with floor_conn:
                floor_conn.executemany(
                    "INSERT OR REPLACE INTO objects VALUES (?, ?, ?, ?, ?)",
                    [(r * batch + j, 1, r, encode(field_lists[j]), 0) for j in range(batch)],
                )

    engine = time_it(engine_run, rounds=1)
    floor = time_it(floor_run, rounds=1)
    store.close()
    floor_conn.close()
    return engine / floor


def _commit_tput_large_ratio(tmp_path: Path) -> float:
    """Build the two extents of the shape gate; return t(10N) / t(N)."""
    times = []
    for n in LARGE_SHAPE_EXTENTS:
        store = dc.Store.open(tmp_path / f"large-{n}.store")

        def run(store: dc.Store = store, n: int = n) -> None:
            _gen.build(store, specimens=n, batch=n)

        times.append(time_it(run, rounds=1))
        store.close()
    return times[1] / times[0]


def _hydrate_batch_ratio(store: dc.Store) -> float:
    """Time get_many against the raw decode-to-dataclass floor; return engine / floor."""
    keys = store.pluck(_gen.Specimen.quality == "A", "specimen_no")[:1_000]  # pyrefly: ignore[bad-argument-type]  # magic-query predicate is untypeable by design
    floor_run = _decode_loop_floor(keys)

    def engine_run() -> None:
        store.get_many(_gen.Specimen, specimen_no=keys)

    floor = time_it(floor_run)
    engine = time_it(engine_run)
    return engine / floor


def _hydrate_n_plus_1_ratio(store: dc.Store) -> float:
    """Time 1k single lookups against one batch; return batch / singles."""
    keys = store.pluck(_gen.Specimen, "specimen_no")[:1_000]

    def singles() -> None:
        for key in keys:
            store.get(_gen.Specimen, specimen_no=key)

    def batch() -> None:
        store.get_many(_gen.Specimen, specimen_no=keys)

    t_singles = time_it(singles, rounds=3)
    t_batch = time_it(batch, rounds=3)
    return t_batch / t_singles


_CANONICAL_PREDICATE = (_gen.Specimen.quality == "A") & (_gen.Specimen.mass_g >= 100.0)


def _bitmap_vs_scan(store: dc.Store) -> tuple[int, int, float]:
    """Return (selected rows, rows the scan finds, indexed / scan time)."""
    expected = store.count(_CANONICAL_PREDICATE)  # pyrefly: ignore[bad-argument-type]  # magic-query predicate is untypeable by design
    all_keys = store.pluck(_gen.Specimen, "specimen_no")
    scanned: list[int] = []

    def indexed() -> None:
        store.query(_CANONICAL_PREDICATE)  # pyrefly: ignore[bad-argument-type]  # magic-query predicate is untypeable by design

    def scan() -> None:
        hits = [
            s for s in store.get_many(_gen.Specimen, specimen_no=all_keys)
            if s.quality == "A" and s.mass_g >= 100.0
        ]
        scanned.append(len(hits))

    t_scan = time_it(scan, rounds=2)
    t_indexed = time_it(indexed, rounds=5)
    return expected, scanned[-1], t_indexed / t_scan


def _unique_key_lookup_ratio(store: dc.Store) -> tuple[list[object], float]:
    """Return the live entities and the engine / dict lookup time."""
    keys = store.pluck(_gen.Specimen, "specimen_no")[:200]
    mirror = {k: i for i, k in enumerate(keys)}
    # hold strong refs during timing: the gate measures LOOKUP of live
    # entities, not re-hydration (entities are weakly registered, so a
    # dropped result list would force a reload every call — invariant 6)
    live = store.get_many(_gen.Specimen, specimen_no=keys)

    def engine_run() -> None:
        for key in keys:
            store.get(_gen.Specimen, specimen_no=key)

    def floor_run() -> None:
        for key in keys:
            mirror[key]

    engine = time_it(engine_run)
    floor = time_it(floor_run)
    return list(live), engine / floor


def _timed_deltas(store: dc.Store, prefix: str) -> list[float]:
    """Commit 12 one-specimen deltas, timing each commit (the consumer is already attached)."""
    anchor = store.get(_gen.Mineral, qid="QM0000")
    if anchor is None:
        raise RuntimeError("anchor mineral QM0000 is missing from the benchmark store")
    times: list[float] = []
    for i in range(12):
        store.store(_gen.Specimen(
            specimen_no=f"{prefix}-{store.last_tid}-{i:04d}",
            mineral=dc.Lazy.of(anchor),
            quality="B", mass_g=10.0,
        ))
        times.append(time_it(store.commit, rounds=1))
    return times


def _oids_of_first_specimens(store: dc.Store) -> list[int]:
    keys = store.pluck(_gen.Specimen, "specimen_no")[:1_000]
    return [s.__dc_oid__ for s in store.get_many(_gen.Specimen, specimen_no=keys)]


def _to_pydantic_ratio(store: dc.Store) -> float:
    """Time the view-read floor against read + to_pydantic; return engine / floor."""
    from datacrystal.web import to_pydantic

    oids = _oids_of_first_specimens(store)

    def floor_run() -> None:
        # the EntityView read floor: decode each record into a frozen view
        with store.snapshot() as snap:
            for oid in oids:
                snap.get(oid)

    def engine_run() -> None:
        # read + project + validate: the full to_pydantic cost over the same reads
        with store.snapshot() as snap:
            for oid in oids:
                to_pydantic(snap.get(oid))

    floor = time_it(floor_run, rounds=3)
    engine = time_it(engine_run, rounds=3)
    return engine / floor


def _delta_scaling(
    small: dc.Store, big: dc.Store, make_consumer: Callable[[Any], Any], prefix: str
) -> float:
    """Apply fixed deltas on both stores; return median(big) / median(small)."""

    def apply_times(store: dc.Store) -> list[float]:
        with store.snapshot() as snap:
            consumer = make_consumer(snap)
        store.attach(consumer)
        times = _timed_deltas(store, prefix)
        store.detach(consumer)
        if hasattr(consumer, "close"):
            consumer.close()
        return times

    return median(apply_times(big)) / median(apply_times(small))


def _counting_consumer(snap: Snapshot) -> DeltaConsumer:
    from datacrystal.testing import CountingConsumer

    return CountingConsumer.bootstrap(snap)


def _extra_consumer_factory(extra: str, tmp_path: Path) -> Callable[[Any], Any]:
    counter = iter(range(10_000_000))

    if extra == "fts":
        pytest.importorskip("snowballstemmer")
        from datacrystal.fts import FullTextIndex

        def make_fts(snap: Snapshot) -> DeltaConsumer:
            idx = FullTextIndex.bootstrap(
                tmp_path / f"fts-{next(counter)}.fts", snap,
                fulltext={"benchmarks._gen:Specimen": {"specimen_no": None}},
            )
            # pinned merge policy (KICKOFF): merge to quiescence pre-timing
            idx._conn.execute("INSERT INTO docs (docs) VALUES ('optimize')")
            return idx

        return make_fts

    pytest.importorskip("pyarrow")
    from datacrystal.arrow import ArrowMirror

    def make_arrow(snap: Snapshot) -> DeltaConsumer:
        return ArrowMirror.bootstrap(
            tmp_path / f"arrow-{next(counter)}.mirror", snap, only=[_gen.Specimen],
        )

    return make_arrow


def _snapshot_open_read_ratio(store: dc.Store) -> float:
    """Time a fresh snapshot per read against a live owner read; return engine / floor."""
    oids = _oids_of_first_specimens(store)

    def floor_run() -> None:
        # owner read floor: one live single-record read per OID, no snapshot
        for oid in oids:
            store.get_many([oid])

    def engine_run() -> None:
        # the per-request snapshot tax: a fresh snapshot per single-record read
        for oid in oids:
            with store.snapshot() as snap:
                snap.get(oid)

    floor = time_it(floor_run, rounds=3)
    engine = time_it(engine_run, rounds=3)
    return engine / floor


_PROTECTED_PREDICATE = (
    (_gen.CuratedSpecimen.quality == "A") & (_gen.CuratedSpecimen.mass_g >= 100.0)
)


def _read_fence_ratio(tmp_path: Path) -> tuple[int, int, float]:
    """Return both candidate counts and the protected / unprotected query time."""
    n = 5000
    owner = dc.Principal(uid=1001, memberships={dc.WORLD: dc.CURATOR})
    prot_store = dc.Store.open(tmp_path / "fence_prot.store", principal=owner)
    _gen.build(prot_store, specimens=n, protected=True)
    plain_store = dc.Store.open(tmp_path / "fence_plain.store")
    _gen.build(plain_store, specimens=n, protected=False)

    with prot_store.snapshot(principal=owner) as ps, plain_store.snapshot() as us:
        n_prot = ps.count(_PROTECTED_PREDICATE)  # pyrefly: ignore[bad-argument-type]  # magic-query predicate is untypeable by design
        n_plain = us.count(_CANONICAL_PREDICATE)  # pyrefly: ignore[bad-argument-type]  # magic-query predicate is untypeable by design

    def filtered_run() -> None:
        # the protected, principal-bound discovery path: snapshot compiles the
        # readable set, intersects it before the window (ADR-008 W4)
        with prot_store.snapshot(principal=owner) as snap:
            snap.query(_PROTECTED_PREDICATE)  # pyrefly: ignore[bad-argument-type]  # magic-query predicate is untypeable by design

    def unfiltered_run() -> None:
        # the unprotected twin: no fence, snapshot open + query only
        with plain_store.snapshot() as snap:
            snap.query(_CANONICAL_PREDICATE)  # pyrefly: ignore[bad-argument-type]  # magic-query predicate is untypeable by design

    t_filtered = time_it(filtered_run, rounds=5)
    t_unfiltered = time_it(unfiltered_run, rounds=5)
    prot_store.close()
    plain_store.close()
    return n_prot, n_plain, t_filtered / t_unfiltered


def _commit_gate_ratio(tmp_path: Path) -> float:
    """Time update commits on a protected and an unprotected twin; return protected / plain."""
    n = 200
    owner = dc.Principal(uid=1001, memberships={dc.WORLD: dc.CURATOR})

    prot_store = dc.Store.open(tmp_path / "protected.store", principal=owner)
    plain_store = dc.Store.open(tmp_path / "plain.store", principal=owner)
    mineral_p = _gen.Mineral(qid="QP0001", name="quartz", crystal_system="trigonal")
    mineral_u = _gen.Mineral(qid="QU0001", name="quartz", crystal_system="trigonal")
    prot_store.root = [mineral_p]
    plain_store.root = [mineral_u]
    curated = [
        _gen.CuratedSpecimen(specimen_no=f"C-{i:05d}", mineral=dc.Lazy.of(mineral_p),
                             quality="A", mass_g=10.0)
        for i in range(n)
    ]
    plain = [
        _gen.Specimen(specimen_no=f"P-{i:05d}", mineral=dc.Lazy.of(mineral_u),
                      quality="A", mass_g=10.0)
        for i in range(n)
    ]
    for s in curated:
        prot_store.store(s)
    for s in plain:
        plain_store.store(s)
    prot_store.commit()
    plain_store.commit()

    ticks = iter(range(10_000_000))

    def prot_run() -> None:
        bump = next(ticks)  # a fresh value every round — pending never empty
        for s in curated:
            s.mass_g = 10.0 + bump
        prot_store.commit()

    def plain_run() -> None:
        bump = next(ticks)
        for s in plain:
            s.mass_g = 10.0 + bump
        plain_store.commit()

    t_prot = time_it(prot_run, rounds=3)
    t_plain = time_it(plain_run, rounds=3)
    prot_store.close()
    plain_store.close()
    return t_prot / t_plain


def test_commit_throughput_stays_within_floor_ratio(tmp_path: Path) -> None:
    """Keep small-batch commits within 3x of the raw sqlite floor (KICKOFF ``commit_tput_small``).

    Given a store committing batches of specimens and an sqlite floor of msgspec encode plus
    executemany with identical transaction boundaries
    When both run the same number of rounds
    Then the three-phase overhead stays at most 3x the floor
    """
    ratio = _commit_tput_small_ratio(tmp_path)

    gate("commit_tput_small (engine/floor)", ratio, 3.0)


def test_commit_throughput_never_quadratic_across_extents(tmp_path: Path) -> None:
    """Keep bulk commits linear in the extent (KICKOFF ``commit_tput_large``).

    Given two stores built at extents N and 10N
    When both are timed on the same machine in the same run
    Then t(10N) / t(N) stays at most 12
    """
    ratio = _commit_tput_large_ratio(tmp_path)

    gate("commit_tput_large (t(10N)/t(N))", ratio, 12.0)


def test_hydrate_batch_stays_within_raw_decode_floor(big_cabinet: dc.Store) -> None:
    """Keep ``get_many`` within 4x of a raw decode loop (KICKOFF ``hydrate_batch``).

    Floor parity: the anchor constructs plain dataclass instances, not just value lists.

    Given the big cabinet and 1k keys of its A-quality specimens
    When ``get_many`` and a raw msgspec decode-to-dataclass loop each read them
    Then the engine takes at most 4x the raw loop
    """
    ratio = _hydrate_batch_ratio(big_cabinet)

    gate("hydrate_batch (engine/raw-decode)", ratio, 4.0)


def test_hydrate_batch_beats_single_lookups_five_to_one(big_cabinet: dc.Store) -> None:
    """Keep N+1 hydration off the user (KICKOFF ``hydrate_n_plus_1``).

    Given the big cabinet and 1k specimen keys
    When they are read as 1k single lookups and as one batch
    Then the batch takes at most 0.2 of the singles, a 5x speedup
    """
    ratio = _hydrate_n_plus_1_ratio(big_cabinet)

    gate("hydrate_n_plus_1 (singles/batch ≥ 5 → batch/singles ≤ 0.2)", ratio, 0.2)


def test_bitmap_query_beats_hydrating_scan_ten_to_one(big_cabinet: dc.Store) -> None:
    """Keep the canonical ~1% predicate on bitmaps (KICKOFF ``query_bitmap_vs_scan``).

    Given the big cabinet and a predicate selecting about 1% of the specimens
    When it is answered from bitmaps and by a full hydrating scan
    Then the scan finds the same rows and the bitmap answer is at least 10x faster
    """
    expected, scanned, ratio = _bitmap_vs_scan(big_cabinet)

    assert 0 < expected < SPECIMENS * 0.05, "predicate selectivity drifted"
    assert scanned == expected
    gate("query_bitmap_vs_scan (indexed/scan ≤ 0.1 ⇔ ≥10× speedup)", ratio, 0.1)


def test_unique_key_lookup_stays_within_dict_floor(big_cabinet: dc.Store) -> None:
    """Keep a natural-key lookup within 50x of a dict lookup (KICKOFF ``unique_key_lookup``).

    Given the big cabinet with 200 live specimens held by strong references
    When each is looked up by its natural key and in a plain dict
    Then the engine takes at most 50x the dict
    """
    live, ratio = _unique_key_lookup_ratio(big_cabinet)

    assert all(live)
    gate("unique_key_lookup (engine/dict)", ratio, 50.0)


def test_to_pydantic_stays_within_view_read_floor(big_cabinet: dc.Store) -> None:
    """Keep ``to_pydantic`` within 2x of the EntityView read floor (#97).

    A breach means accidental ref-hydration or per-call re-reflection crept on top of the
    read, both forbidden by #97. Each round opens a fresh snapshot so ``get`` truly decodes.

    Given the big cabinet and 1k specimen records
    When they are read into views, and read and converted into validated DTOs
    Then the conversion takes at most 2x the view read
    """
    pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")

    ratio = _to_pydantic_ratio(big_cabinet)

    gate("to_pydantic (read+convert+validate / view-read floor)", ratio, 2.0)


def test_watermark_apply_cost_independent_of_corpus_size(
    small_cabinet: dc.Store, big_cabinet: dc.Store
) -> None:
    """Keep applying a fixed delta O(delta), never O(corpus) (``watermark_apply_fixed_delta``).

    Median over 12 consecutive deltas on the bitmap-consumer path; the in-tree
    CountingConsumer keeps this gate extra-free.

    Given a small and a big cabinet, each with a counting consumer attached
    When twelve one-specimen deltas are committed on each
    Then the big cabinet's median apply time is at most 1.2x the small one's
    """
    ratio = _delta_scaling(small_cabinet, big_cabinet, _counting_consumer, "WM")

    gate(f"watermark_apply_fixed_delta (t@{SPECIMENS}/t@{SMALL_SPECIMENS})", ratio, 1.2)


@pytest.mark.parametrize("extra", ["fts", "arrow"])
def test_watermark_apply_cost_independent_of_corpus_size_through_extras(
    small_cabinet: dc.Store, big_cabinet: dc.Store, tmp_path: Path, extra: str
) -> None:
    """Keep the O(delta) shape through the real extras, ``datacrystal[fts]`` and ``[arrow]``.

    The FTS5 merge policy is pinned ('optimize' before timing, per KICKOFF); the arrow mirror
    writes a parquet segment per flush.

    Given a small and a big cabinet, each with the extra's consumer attached
    When twelve one-specimen deltas are committed on each
    Then the big cabinet's median apply time is at most 1.2x the small one's
    """
    make_consumer = _extra_consumer_factory(extra, tmp_path)

    ratio = _delta_scaling(small_cabinet, big_cabinet, make_consumer, f"WX-{extra}")

    gate(
        f"watermark_apply_fixed_delta[{extra}] (t@{SPECIMENS}/t@{SMALL_SPECIMENS})",
        ratio, 1.2,
    )


def test_snapshot_per_read_stays_within_owner_read_floor(big_cabinet: dc.Store) -> None:
    """Keep the per-request snapshot tax within 25x of an owner read (``snapshot_open_read``, #92).

    A ``datacrystal[web]`` route reads every request off a snapshot (any-thread, ADR-002).
    Both runs do the same number of single-record reads of the same OIDs, so the ratio isolates
    the snapshot open and close. The owner floor is a warm identity-registry hit, near-free, so
    25x (about 11x observed) catches a regression in the snapshot lifecycle, such as an
    O(extent) index rebuild on every open; the absolute cost stays near 0.094 ms per request.

    Given the big cabinet and 1k specimen OIDs
    When each is read through a fresh snapshot and through the live owner
    Then the snapshot path takes at most 25x the owner path
    """
    ratio = _snapshot_open_read_ratio(big_cabinet)

    gate("snapshot_open_read (snapshot open+read+close / owner read)", ratio, 25.0)


def test_read_fence_discovery_stays_within_twice_unprotected_query(tmp_path: Path) -> None:
    """Keep a principal-bound discovery query within 2x of its unprotected twin (ADR-008 W4).

    The read-side counterpart of the write gate: a fresh ``store.snapshot(principal=...)``
    per request compiles the principal's readable bitmap once (O(extent), R15) and intersects
    it before the window. ``CuratedSpecimen`` is ``Specimen``'s protected twin, so both stores
    hold the same shape and the same predicate selects the same candidate count. Observed
    about 1.3x; warn-stage, hardens via ``DC_BENCH_STRICT`` after 14 green nights.

    Given a protected and an unprotected store of 5000 specimens and a non-root owner
    When the same discovery query runs on each, in a fresh snapshot per run
    Then both find the same candidates and the protected run takes at most 2x
    """
    n_protected, n_plain, ratio = _read_fence_ratio(tmp_path)

    assert n_protected == n_plain
    assert n_plain > 0
    gate("read_fence_discovery (protected principal-bound / unprotected query)", ratio, 2.0)


def test_commit_gate_stays_within_twice_unprotected_update(tmp_path: Path) -> None:
    """Keep a protected-class update commit within 2x of its unprotected twin (ADR-008 W2-9).

    The fence's marginal work is one batched ``load_many`` of the delta's priors, one msgpack
    decode per prior and integer compares, bounded by the commit's own encode and apply.
    Discipline: update commits only, a non-indexed field mutated, no consumers attached (they
    would flatten the ratio) and a non-root owner (root short-circuits before the prior read).

    Given a protected and an unprotected store of 200 specimens each and a non-root owner
    When every specimen's mass is mutated and committed on each
    Then the protected commit takes at most 2x the unprotected one
    """
    ratio = _commit_gate_ratio(tmp_path)

    gate("commit_gate_overhead (protected/unprotected update commit)", ratio, 2.0)
