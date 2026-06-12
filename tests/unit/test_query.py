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
