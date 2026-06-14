"""`dc.SortedIndex` — range queries from a sorted index (ADR-004 / #18).

A SortedIndex field answers `>=`/`>`/`<=`/`<` (and `between` as their And) from
an in-memory sorted run, not a full-extent scan, and still answers `==` as a
point lookup. Validated against a brute-force oracle, over both backends.
"""

from __future__ import annotations

from typing import Annotated

import pytest

import datacrystal as dc


@dc.entity
class Specimen:
    label: Annotated[str, dc.Unique]
    mass_g: Annotated[float, dc.SortedIndex] = 0.0       # range-queryable
    year: Annotated[int | None, dc.SortedIndex] = None    # range + None
    quality: Annotated[str | None, dc.Index] = None       # plain bitmap (compound)


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
