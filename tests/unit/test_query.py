"""Condition AST + bitmap planner (ROADMAP item 4).

These tests deliberately exercise the magic class-attribute path
(``Mineral.mohs >= 3.7``), which type checkers cannot model — the pragma
below silences exactly that. Checker-clean user code uses ``dc.fields()``.
"""
# pyright: reportOptionalOperand=false, reportOptionalMemberAccess=false
# pyright: reportAttributeAccessIssue=false, reportOperatorIssue=false
# pyright: reportArgumentType=false

from __future__ import annotations

import pytest

import datacrystal as dc
from tests.conftest import Locality, Mineral


@pytest.fixture
def cabinet(store):
    store.root = [
        Mineral(qid="Q1", name="quartz", crystal_system="trigonal", mohs=7.0),
        Mineral(qid="Q2", name="azurite", crystal_system="monoclinic", mohs=3.5),
        Mineral(qid="Q3", name="malachite", crystal_system="monoclinic", mohs=3.8),
        Mineral(qid="Q4", name="diamond", crystal_system="cubic", mohs=10.0),
        Mineral(qid="Q5", name="opal", crystal_system=None, mohs=5.5),
    ]
    store.commit()
    return store


def _names(hits) -> list[str]:
    return sorted(m.name for m in hits)


def test_indexed_eq(cabinet):
    assert _names(cabinet.query(Mineral.crystal_system == "monoclinic")) == [
        "azurite", "malachite",
    ]


def test_fields_proxy_is_equivalent_and_validated(cabinet):
    M = dc.fields(Mineral)
    hits = cabinet.query((M.crystal_system == "monoclinic") & (M.mohs >= 3.7))
    assert _names(hits) == ["malachite"]
    with pytest.raises(AttributeError, match="no persisted field"):
        _ = M.no_such_field
    with pytest.raises(dc.NotAnEntityError):
        dc.fields(str)


def test_and_of_indexed_and_residual(cabinet):
    hits = cabinet.query((Mineral.crystal_system == "monoclinic") & (Mineral.mohs >= 3.7))
    assert _names(hits) == ["malachite"]


def test_or_fully_indexed(cabinet):
    hits = cabinet.query(
        (Mineral.crystal_system == "cubic") | (Mineral.crystal_system == "trigonal")
    )
    assert _names(hits) == ["diamond", "quartz"]


def test_or_with_residual_falls_back_to_scan(cabinet):
    hits = cabinet.query((Mineral.crystal_system == "cubic") | (Mineral.mohs <= 4.0))
    assert _names(hits) == ["azurite", "diamond", "malachite"]


def test_in_on_indexed_field(cabinet):
    hits = cabinet.query(Mineral.crystal_system.in_(["cubic", "trigonal"]))
    assert _names(hits) == ["diamond", "quartz"]


def test_negation(cabinet):
    hits = cabinet.query(~(Mineral.crystal_system == "monoclinic"))
    assert _names(hits) == ["diamond", "opal", "quartz"]


def test_none_is_an_indexable_value(cabinet):
    assert _names(cabinet.query(Mineral.crystal_system == None)) == ["opal"]  # noqa: E711


def test_residual_only_query_scans_the_extent(cabinet):
    assert _names(cabinet.query(Mineral.mohs > 6.0)) == ["diamond", "quartz"]


def test_ordering_comparison_with_none_never_matches(cabinet):
    cabinet.root.append(Mineral(qid="Q6", name="unknown", mohs=None))
    cabinet.mark_dirty(cabinet._load_oid(cabinet._root_oid))
    cabinet.commit()
    assert "unknown" not in _names(cabinet.query(Mineral.mohs >= 0.0))


def test_cross_entity_condition_raises(cabinet):
    with pytest.raises(dc.QueryError, match="exactly one entity class"):
        cabinet.query((Mineral.crystal_system == "cubic") & (Locality.country == "Namibia"))


def test_query_before_any_records_returns_empty_but_warns(store):
    # Empty is legitimate on a first run; the warning is the footgun guard
    # (forgot to commit? opened a different store file?) — 2026-06-12.
    with pytest.warns(dc.UnseenTypeWarning, match="no committed records"):
        assert store.query(Locality.country == "Namibia") == []


def test_uncommitted_changes_are_invisible_to_query(cabinet):
    cabinet.store(Mineral(qid="Q7", name="halite", crystal_system="cubic"))
    assert _names(cabinet.query(Mineral.crystal_system == "cubic")) == ["diamond"]
    cabinet.commit()
    assert _names(cabinet.query(Mineral.crystal_system == "cubic")) == ["diamond", "halite"]


def test_forgot_parentheses_gives_a_helpful_error(cabinet):
    with pytest.raises(dc.QueryError, match="parentheses"):
        Mineral.crystal_system == "cubic" & (Mineral.mohs >= 1)  # noqa: B015


# -- contains / startswith (KICKOFF M4: distinct-key iteration) -----------------


def test_startswith_on_indexed_field_uses_index_keys(cabinet):
    from datacrystal._entity import type_info
    from datacrystal._indexes import plan

    cond = Mineral.crystal_system.startswith("mono")
    assert _names(cabinet.query(cond)) == ["azurite", "malachite"]
    # The planner must answer this from the index alone (no residual) —
    # the None key in the cabinet's index is skipped, not a crash.
    ci = cabinet._index.ensure(type_info(Mineral))
    bitmap, residual = plan(cond, ci)
    assert bitmap is not None and residual is None
    assert len(bitmap) == 2


def test_contains_on_indexed_field_uses_index_keys(cabinet):
    cond = Mineral.crystal_system.contains("clin")
    assert _names(cabinet.query(cond)) == ["azurite", "malachite"]
    assert cabinet.count(cond) == 2


def test_contains_and_startswith_on_residual_fields(cabinet):
    assert _names(cabinet.query(Mineral.name.contains("ite"))) == [
        "azurite", "malachite",
    ]
    assert _names(cabinet.query(Mineral.name.startswith("qua"))) == ["quartz"]
    assert cabinet.pluck(Mineral.name.contains("ite"), "qid") == ["Q2", "Q3"]


def test_string_matching_is_case_sensitive_and_exact(cabinet):
    # Linguistic matching (stemming, folding) is the datacrystal[fts]
    # extra's job — the core predicate is plain substring semantics.
    assert cabinet.query(Mineral.name.contains("ITE")) == []
    assert cabinet.query(Mineral.name.startswith("Qua")) == []


def test_string_matching_skips_non_string_values(cabinet):
    # A None (or any non-str) field value never matches — the SQL-NULL-like
    # rule ordering comparisons already follow.
    assert cabinet.count(Mineral.mohs.contains("7")) == 0
    assert "opal" not in _names(cabinet.query(Mineral.crystal_system.contains("o")))


def test_string_matching_validates_the_needle_type(cabinet):
    with pytest.raises(dc.QueryError, match="takes a str"):
        Mineral.name.contains(7)
    with pytest.raises(dc.QueryError, match="takes a str"):
        Mineral.name.startswith(None)


# -- query(type) symmetry + explain() (decided 2026-06-12, pre-freeze) ----------


def test_query_bare_class_hydrates_full_extent(cabinet):
    """query(Mineral) is the honest spelling of the expensive shape —
    symmetric with count()/pluck()/Snapshot.all()."""
    hits = cabinet.query(Mineral)
    assert _names(hits) == ["azurite", "diamond", "malachite", "opal", "quartz"]
    assert len(hits) == cabinet.count(Mineral)


def test_query_bare_class_unseen_type_is_empty_and_warns(store):
    with pytest.warns(dc.UnseenTypeWarning):
        assert store.query(Locality) == []


def test_query_rejects_non_entity_targets(cabinet):
    with pytest.raises(TypeError, match="takes an @entity class or a Condition"):
        cabinet.query(42)
    with pytest.raises(dc.NotAnEntityError):
        cabinet.query(str)


def test_explain_reports_the_two_rules(cabinet):
    fully_indexed = cabinet.explain(Mineral.crystal_system == "monoclinic")
    assert fully_indexed.indexed and fully_indexed.residual is None
    assert fully_indexed.candidates == 2 and fully_indexed.extent == 5

    mixed = cabinet.explain(
        (Mineral.crystal_system == "monoclinic") & (Mineral.mohs >= 3.7)
    )
    assert mixed.indexed and mixed.residual is not None
    assert "mohs" in mixed.residual
    assert mixed.candidates == 2  # residual evaluates over the bitmap hits

    cliff = cabinet.explain(Mineral.mohs >= 3.7)
    assert not cliff.indexed and cliff.candidates == cliff.extent == 5
    assert "NO index" in str(cliff)

    bare = cabinet.explain(Mineral)
    assert bare.condition is None and bare.candidates == bare.extent == 5
    assert "full extent" in str(bare)


def test_explain_matches_query_hydration_bound(cabinet):
    cond = (Mineral.crystal_system == "monoclinic") & (Mineral.mohs >= 3.7)
    plan = cabinet.explain(cond)
    assert len(cabinet.query(cond)) <= plan.candidates


def test_snapshot_query_and_explain_are_symmetric(cabinet):
    with cabinet.snapshot() as snap:
        views = snap.query(Mineral)
        assert sorted(v.name for v in views) == [
            "azurite", "diamond", "malachite", "opal", "quartz",
        ]
        plan = snap.explain(Mineral.crystal_system == "monoclinic")
        assert plan.indexed and plan.candidates == 2


def test_explain_unseen_type_is_empty_plan(store):
    with pytest.warns(dc.UnseenTypeWarning):
        plan = store.explain(Locality)
    assert plan.extent == 0 and plan.candidates == 0


# --- #14: limit / offset windowing ------------------------------------------

def test_limit_offset_window_matches_full_slice(cabinet):
    # Determinism oracle: a windowed read equals the full read sliced the same.
    full = cabinet.query(Mineral)
    assert cabinet.query(Mineral, limit=2) == full[:2]
    assert cabinet.query(Mineral, limit=2, offset=1) == full[1:3]
    assert cabinet.query(Mineral, offset=3) == full[3:]


def test_limit_offset_on_indexed_condition(cabinet):
    full = cabinet.query(Mineral.crystal_system == "monoclinic")
    assert len(full) == 2
    assert cabinet.query(Mineral.crystal_system == "monoclinic", limit=1) == full[:1]
    assert cabinet.query(Mineral.crystal_system == "monoclinic", offset=1) == full[1:]


def test_limit_offset_on_residual_condition(cabinet):
    # mohs > 4.0 is a residual predicate — the window trims after the filter.
    full = cabinet.query(Mineral.mohs > 4.0)
    assert cabinet.query(Mineral.mohs > 4.0, limit=1) == full[:1]
    assert cabinet.query(Mineral.mohs > 4.0, offset=1, limit=1) == full[1:2]


def test_limit_offset_edges(cabinet):
    assert cabinet.query(Mineral, limit=0) == []
    assert cabinet.query(Mineral, offset=999) == []
    assert cabinet.query(Mineral, offset=999, limit=5) == []
    with pytest.raises(ValueError):
        cabinet.query(Mineral, limit=-1)
    with pytest.raises(ValueError):
        cabinet.query(Mineral, offset=-1)
    with pytest.raises(TypeError):
        cabinet.query(Mineral, limit="2")
    with pytest.raises(TypeError):
        cabinet.query(Mineral, offset=1.5)


def test_pluck_limit_offset(cabinet):
    full = cabinet.pluck(Mineral, "name")
    assert cabinet.pluck(Mineral, "name", limit=2) == full[:2]
    assert cabinet.pluck(Mineral, "name", offset=1, limit=2) == full[1:3]
    assert cabinet.pluck(Mineral, "name", limit=0) == []


def test_count_and_explain_take_no_window(cabinet):
    # count()/explain() are unchanged — the plan never grows a window.
    assert cabinet.count(Mineral.crystal_system == "monoclinic") == 2
    plan = cabinet.explain(Mineral.crystal_system == "monoclinic")
    assert plan.indexed and plan.candidates == 2


def test_snapshot_query_and_all_window(cabinet):
    with cabinet.snapshot() as snap:
        full = snap.query(Mineral)
        assert snap.query(Mineral, limit=2) == full[:2]
        assert snap.query(Mineral, offset=1, limit=2) == full[1:3]
        full_all = snap.all(Mineral)
        assert snap.all(Mineral, limit=2) == full_all[:2]
        assert snap.all(Mineral, offset=3) == full_all[3:]
        # residual path windows too
        mono = snap.query(Mineral.crystal_system == "monoclinic")
        assert snap.query(Mineral.crystal_system == "monoclinic", limit=1) == mono[:1]
        with pytest.raises(ValueError):
            snap.all(Mineral, offset=-1)


# --- #15: store.iter() streaming query ---------------------------------------

def test_iter_matches_query_as_oid_sets(cabinet):
    from datacrystal._entity import oid_of
    streamed = list(cabinet.iter(Mineral.crystal_system == "monoclinic"))
    queried = cabinet.query(Mineral.crystal_system == "monoclinic")
    assert {oid_of(o) for o in streamed} == {oid_of(o) for o in queried}
    # full extent: every entity enumerated exactly once
    all_oids = [oid_of(o) for o in cabinet.iter(Mineral)]
    assert sorted(all_oids) == sorted(oid_of(o) for o in cabinet.query(Mineral))
    assert len(all_oids) == len(set(all_oids))


def test_iter_guards_every_next_against_foreign_thread(cabinet):
    import threading
    it = cabinet.iter(Mineral)
    next(it)  # owner pulls one — fine
    errors: list[str] = []

    def foreign():
        try:
            next(it)
        except dc.WrongThreadError as e:
            errors.append(type(e).__name__)

    t = threading.Thread(target=foreign)
    t.start()
    t.join()
    assert errors == ["WrongThreadError"]


def test_iter_stops_when_closed_mid_stream(store_factory):
    s = store_factory()
    s.root = [Mineral(qid=f"Q{i}", name=f"m{i}") for i in range(3)]
    s.commit()
    it = s.iter(Mineral)
    next(it)
    s.close()
    with pytest.raises(dc.StoreClosedError):
        next(it)


def test_iter_excludes_uncommitted(store_factory):
    s = store_factory()
    s.root = [Mineral(qid="Q1", name="quartz")]
    s.commit()
    s.store(Mineral(qid="Q2", name="topaz"))  # stored, not committed
    assert {m.name for m in s.iter(Mineral)} == {"quartz"}
    s.close()
