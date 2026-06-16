"""`dc.SortedIndex` — range queries from a sorted index (ADR-004 / #18).

A SortedIndex field answers `>=`/`>`/`<=`/`<` (and `between` as their And) from
an in-memory sorted run, not a full-extent scan, and still answers `==` as a
point lookup. Validated against a brute-force oracle, over both backends.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

import pytest

import datacrystal as dc


@dc.entity
class Specimen:
    label: Annotated[str, dc.Unique]
    mass_g: Annotated[float, dc.SortedIndex] = 0.0       # range-queryable
    year: Annotated[int | None, dc.SortedIndex] = None    # range + None
    quality: Annotated[str | None, dc.Index] = None       # plain bitmap (compound)


@dc.entity(frozen=True)
class CatalogEvent:
    """A frozen cabinet event whose acquisition timestamp is a SortedIndex
    datetime key (#106) — the eval's actual need (newest-N on a `published`
    timestamp). Aware datetimes ride msgspec's timestamp ext as a UTC instant
    (_records.py); ``at=None`` is an event with no recorded time."""

    seq: Annotated[int, dc.Unique]
    kind: Annotated[str, dc.Index] = "acquire"
    at: Annotated[datetime | None, dc.SortedIndex] = None


def _seed(store: dc.Store) -> list[Specimen]:
    specs = [
        Specimen(label=f"S{i}", mass_g=float(i * 10), year=2000 + i,
                 quality="fine" if i % 2 else "rough")
        for i in range(10)
    ]
    specs.append(Specimen(label="undated", mass_g=55.0, year=None, quality="fine"))
    for s in specs:
        store.store(s)
    store.commit()
    return specs


F = dc.fields(Specimen)


def test_range_ge_matches_oracle(store):
    _seed(store)
    hits = sorted(s.label for s in store.query(F.mass_g >= 50.0))
    oracle = sorted(s.label for s in store.query(Specimen) if s.mass_g >= 50.0)
    assert hits == oracle
    assert "S5" in hits and "S0" not in hits


def test_all_four_range_ops(store):
    _seed(store)
    for op, fn in [
        (F.mass_g > 50.0, lambda s: s.mass_g > 50.0),
        (F.mass_g >= 50.0, lambda s: s.mass_g >= 50.0),
        (F.mass_g < 50.0, lambda s: s.mass_g < 50.0),
        (F.mass_g <= 50.0, lambda s: s.mass_g <= 50.0),
    ]:
        got = sorted(s.label for s in store.query(op))
        want = sorted(s.label for s in store.query(Specimen) if fn(s))
        assert got == want, op


def test_between_via_and(store):
    _seed(store)
    got = sorted(s.label for s in store.query((F.mass_g >= 30.0) & (F.mass_g <= 70.0)))
    want = sorted(s.label for s in store.query(Specimen) if 30.0 <= s.mass_g <= 70.0)
    assert got == want


def test_point_lookup_on_sorted_field(store):
    _seed(store)
    # == on a SortedIndex field still answers from the index (it is an eq index)
    hits = store.query(F.mass_g == 30.0)
    assert [s.label for s in hits] == ["S3"]
    assert store.explain(F.mass_g == 30.0).indexed  # not a residual


def test_none_never_matches_a_range(store):
    _seed(store)
    # the undated specimen (year=None) is excluded from every year range
    after = [s.label for s in store.query(F.year >= 2000)]
    assert "undated" not in after
    assert set(after) == {f"S{i}" for i in range(10)}


def test_compound_sorted_and_bitmap(store):
    _seed(store)
    got = sorted(s.label for s in store.query((F.mass_g >= 40.0) & (F.quality == "fine")))
    want = sorted(s.label for s in store.query(Specimen)
                  if s.mass_g >= 40.0 and s.quality == "fine")
    assert got == want


def test_range_is_index_backed_not_residual(store):
    _seed(store)
    plan = store.explain(F.mass_g >= 50.0)
    assert plan.indexed                 # answered via the sorted index
    assert plan.residual is None        # no Python residual scan
    assert plan.candidates < plan.extent  # only the matching slice considered


def test_update_maintains_the_range(store):
    s0 = _seed(store)[0]               # S0, mass_g 0.0
    assert [x.label for x in store.query(F.mass_g >= 1000.0)] == []
    s0.mass_g = 5000.0
    store.commit()
    assert [x.label for x in store.query(F.mass_g >= 1000.0)] == ["S0"]
    assert [x.label for x in store.query(F.mass_g == 0.0)] == []  # old key gone


def test_delete_drops_from_the_range(store):
    _seed(store)
    assert "S9" in {s.label for s in store.query(F.mass_g >= 80.0)}
    store.delete(Specimen, label="S9")
    store.commit()
    assert "S9" not in {s.label for s in store.query(F.mass_g >= 80.0)}


def test_sorted_index_rebuilt_after_reopen(store_factory):
    s = store_factory()
    _seed(s)
    s.close()
    reopened = store_factory()  # fresh index — the sorted run rebuilds from a scan
    got = sorted(x.label for x in reopened.query(F.mass_g >= 50.0))
    assert got == sorted(f"S{i}" for i in range(5, 10)) + ["undated"]
    reopened.close()


def test_sorted_index_rejects_a_list_field():
    with pytest.raises(TypeError, match="SortedIndex"):
        @dc.entity
        class Bad:
            tags: Annotated[list[str], dc.SortedIndex] = ()  # type: ignore[assignment]


# --- datetime as a SortedIndex key (#106 / ADR-004 §1) ------------------------

_BASE = datetime(2021, 1, 1, 9, 0, tzinfo=timezone.utc)
EF = dc.fields(CatalogEvent)


def _seed_events(store: dc.Store) -> None:
    """Ten timestamped acquisitions one day apart (aware UTC) + one undated
    event (at=None). The undated one is SQL-NULL-like: out of every range, last
    in every order_by."""
    for i in range(10):
        store.store(CatalogEvent(seq=i, at=_BASE + timedelta(days=i)))
    store.store(CatalogEvent(seq=99, at=None))
    store.commit()


def test_datetime_range_all_four_ops_match_oracle(store):
    # AC2: >=/>/<=/< on a SortedIndex datetime answer from the sorted-run slice,
    # identical to a Python full-scan. None is excluded from every range.
    _seed_events(store)
    cut = _BASE + timedelta(days=5)
    for cond, fn in [
        (EF.at >= cut, lambda e: e.at is not None and e.at >= cut),
        (EF.at > cut, lambda e: e.at is not None and e.at > cut),
        (EF.at <= cut, lambda e: e.at is not None and e.at <= cut),
        (EF.at < cut, lambda e: e.at is not None and e.at < cut),
    ]:
        got = sorted(e.seq for e in store.query(cond))
        want = sorted(e.seq for e in store.query(CatalogEvent) if fn(e))
        assert got == want, cond
        assert 99 not in got  # the undated (None) event is in no range


def test_datetime_between_via_and(store):
    _seed_events(store)
    lo, hi = _BASE + timedelta(days=3), _BASE + timedelta(days=7)
    got = sorted(e.seq for e in store.query((EF.at >= lo) & (EF.at <= hi)))
    want = sorted(e.seq for e in store.query(CatalogEvent)
                  if e.at is not None and lo <= e.at <= hi)
    assert got == want


def test_datetime_range_is_index_backed_not_residual(store):
    _seed_events(store)
    plan = store.explain(EF.at >= _BASE + timedelta(days=5))
    assert plan.indexed              # answered via the sorted index
    assert plan.residual is None     # no Python residual scan
    assert plan.candidates < plan.extent


def test_datetime_order_by_straight_from_index_nulls_last(store):
    # AC2: order_by reads straight from the sorted index; the undated event sorts
    # NULLs-last in BOTH directions, matching a brute-force oracle.
    _seed_events(store)
    asc = [e.seq for e in store.query(CatalogEvent, order_by=EF.at)]
    assert asc == list(range(10)) + [99]            # ascending, None last
    desc = [e.seq for e in store.query(CatalogEvent, order_by=(EF.at, "desc"))]
    assert desc == list(range(9, -1, -1)) + [99]    # descending, None STILL last


def test_datetime_order_by_top_k_newest(store):
    # The eval's headline: newest-N on a published timestamp, straight off the run.
    _seed_events(store)
    newest3 = [e.seq for e in store.query(CatalogEvent, order_by=(EF.at, "desc"),
                                          limit=3)]
    assert newest3 == [9, 8, 7]


def test_datetime_snapshot_range_and_order_by(store):
    # AC2: a snapshot rebuilds the same build_class_indexes from its pinned view,
    # so it answers the datetime range + order_by identically (and exercises the
    # comparability rule on the rebuild).
    _seed_events(store)
    snap = store.snapshot()
    cut = _BASE + timedelta(days=5)
    got = sorted(v.seq for v in snap.query(EF.at >= cut))
    want = sorted(e.seq for e in store.query(CatalogEvent)
                  if e.at is not None and e.at >= cut)
    assert got == want
    ordered = [v.seq for v in snap.query(CatalogEvent, order_by=(EF.at, "desc"))]
    assert ordered == list(range(9, -1, -1)) + [99]


def test_datetime_range_rebuilt_after_reopen(store_factory):
    # The sorted run rebuilds from a backend scan on reopen (invariant 11).
    s = store_factory()
    _seed_events(s)
    s.close()
    reopened = store_factory()
    cut = _BASE + timedelta(days=7)
    got = sorted(e.seq for e in reopened.query(EF.at >= cut))
    assert got == [7, 8, 9]
    reopened.close()


def test_aware_datetimes_order_by_utc_instant(store):
    # AC3 edge: aware datetimes order by their UTC instant — a +5h-offset clock
    # at 12:00 (07:00Z) sorts BEFORE a UTC clock at 09:00 (09:00Z), DST/offset
    # irrelevant. The msgpack codec already normalizes aware → UTC instant.
    plus5 = timezone(timedelta(hours=5))
    store.store(CatalogEvent(seq=1, at=datetime(2021, 6, 1, 12, 0, tzinfo=plus5)))
    store.store(CatalogEvent(seq=2, at=datetime(2021, 6, 1, 9, 0, tzinfo=timezone.utc)))
    store.commit()
    ordered = [e.seq for e in store.query(CatalogEvent, order_by=EF.at)]
    assert ordered == [1, 2]  # offset-noon (07:00Z) is the earlier instant


def test_mixing_naive_and_aware_raises_named_error_within_one_commit(store):
    # AC3 edge: a naive + an aware datetime in the SAME field — never a bare
    # comparison TypeError leaking from insort/bisect; a loud datacrystal error.
    store.store(CatalogEvent(seq=1, at=datetime(2021, 6, 1, 12, 0, tzinfo=timezone.utc)))
    store.store(CatalogEvent(seq=2, at=datetime(2021, 6, 1, 12, 0)))  # naive
    with pytest.raises(dc.MixedTemporalIndexError, match="naive and timezone-aware"):
        store.commit()


def test_mixing_naive_and_aware_raises_across_commits(store):
    # The mix can straddle two commits: the second is validated against the run
    # the first built. Still the named error, never a bare TypeError, both backends.
    store.store(CatalogEvent(seq=1, at=datetime(2021, 6, 1, 12, 0, tzinfo=timezone.utc)))
    store.commit()
    store.store(CatalogEvent(seq=2, at=datetime(2021, 6, 2, 12, 0)))  # naive
    with pytest.raises(dc.MixedTemporalIndexError):
        store.commit()


def test_naive_datetimes_alone_order_and_range_fine(store):
    # All-naive is a valid single convention — only MIXING is rejected.
    base = datetime(2021, 1, 1, 9, 0)  # naive
    for i in range(5):
        store.store(CatalogEvent(seq=i, at=base + timedelta(days=i)))
    store.commit()
    cut = base + timedelta(days=2)
    got = sorted(e.seq for e in store.query(EF.at >= cut))
    assert got == [2, 3, 4]
    ordered = [e.seq for e in store.query(CatalogEvent, order_by=(EF.at, "desc"))]
    assert ordered == [4, 3, 2, 1, 0]
