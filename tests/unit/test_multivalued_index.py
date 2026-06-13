"""Multi-valued (inverted) index on list fields (#13).

A mineral specimen carries several tags (museum / fine / fluorescent / rare);
``query(Specimen.tags.contains("rare"))`` must answer from an element-keyed
bitmap with zero record reads — instead of exploding each tag into its own
entity. Parametrized over both backends: the storage protocol is the seam,
memory and sqlite must behave identically.

These tests exercise the magic class-attribute path (``Specimen.tags.contains``)
that type checkers cannot model; the pragma below silences exactly that.
"""
# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
# pyright: reportArgumentType=false, reportOperatorIssue=false

from __future__ import annotations

from dataclasses import field
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import type_info


@dc.entity
class Specimen:
    specimen_no: Annotated[str, dc.Unique]
    tags: Annotated[list[str], dc.Index] = field(default_factory=list)
    aliases: Annotated[list[str] | None, dc.Index] = None


def _nos(hits) -> list[str]:
    return sorted(s.specimen_no for s in hits)


@pytest.fixture
def drawer(store):
    store.root = [
        Specimen(specimen_no="S1", tags=["museum", "fluorescent", "rare"],
                 aliases=["alt"]),
        Specimen(specimen_no="S2", tags=["fine", "fluorescent"]),
        Specimen(specimen_no="S3", tags=["rare"]),
        Specimen(specimen_no="S4", tags=[]),
    ]
    store.commit()
    return store


def test_field_marked_multivalued():
    by_name = {s.name: s for s in type_info(Specimen).specs}
    assert by_name["tags"].multivalued and by_name["tags"].indexed
    assert by_name["aliases"].multivalued
    assert not by_name["specimen_no"].multivalued


def test_contains_membership(drawer):
    assert _nos(drawer.query(Specimen.tags.contains("rare"))) == ["S1", "S3"]
    assert _nos(drawer.query(Specimen.tags.contains("fluorescent"))) == ["S1", "S2"]
    assert _nos(drawer.query(Specimen.tags.contains("museum"))) == ["S1"]
    assert _nos(drawer.query(Specimen.tags.contains("nonexistent"))) == []


def test_contains_is_membership_not_substring(drawer):
    # The defining difference from a scalar-string field: list membership is
    # EXACT — a substring of an element does not match.
    assert _nos(drawer.query(Specimen.tags.contains("fluor"))) == []
    assert _nos(drawer.query(Specimen.tags.contains("fluorescent"))) == ["S1", "S2"]


def test_contains_is_index_only_no_residual(drawer):
    plan = drawer.explain(Specimen.tags.contains("rare"))
    assert plan.indexed and plan.residual is None


def test_membership_matches_brute_force(drawer):
    # Oracle: the bitmap result equals plain Python membership over the extent.
    everything = list(drawer.query(Specimen))
    for needle in ("museum", "fine", "fluorescent", "rare", "ghost"):
        expected = sorted(s.specimen_no for s in everything if needle in s.tags)
        assert _nos(drawer.query(Specimen.tags.contains(needle))) == expected


def test_posting_diff_on_assignment_update(drawer):
    # S2: fine, fluorescent  ->  fine, rare.  Drop fluorescent, add rare, keep
    # fine — the un-index must diff old vs new, not swap one scalar.
    s2 = next(s for s in drawer.query(Specimen) if s.specimen_no == "S2")
    s2.tags = ["fine", "rare"]
    drawer.commit()
    assert _nos(drawer.query(Specimen.tags.contains("fluorescent"))) == ["S1"]
    assert _nos(drawer.query(Specimen.tags.contains("rare"))) == ["S1", "S2", "S3"]
    assert _nos(drawer.query(Specimen.tags.contains("fine"))) == ["S2"]


def test_in_place_removal_uses_prior_snapshot(drawer):
    # In-place .remove() mutates the SAME PersistentList the index captured.
    # Without the last_values copy, the un-index would read the already-mutated
    # list and leave S1 stale in the 'rare' posting.
    s1 = next(s for s in drawer.query(Specimen) if s.specimen_no == "S1")
    s1.tags.remove("rare")
    drawer.commit()
    assert _nos(drawer.query(Specimen.tags.contains("rare"))) == ["S3"]
    assert _nos(drawer.query(Specimen.tags.contains("museum"))) == ["S1"]


def test_eq_on_list_field_is_residual_whole_list(drawer):
    # == over a whole list can't use the element index -> residual that compares
    # the actual list (and must not crash on an unhashable-list bitmap lookup).
    plan = drawer.explain(Specimen.tags == ["rare"])
    assert not plan.indexed
    assert _nos(drawer.query(Specimen.tags == ["rare"])) == ["S3"]


def test_optional_list_none_is_safe(drawer):
    # S2/S3/S4 have aliases=None: they contribute no postings, and querying a
    # store that holds None list values never raises.
    assert _nos(drawer.query(Specimen.aliases.contains("alt"))) == ["S1"]


def test_delete_clears_all_element_postings(drawer):
    s1 = next(s for s in drawer.query(Specimen) if s.specimen_no == "S1")
    drawer.delete(s1)
    drawer.commit()
    assert _nos(drawer.query(Specimen.tags.contains("museum"))) == []
    assert _nos(drawer.query(Specimen.tags.contains("rare"))) == ["S3"]
    assert _nos(drawer.query(Specimen.tags.contains("fluorescent"))) == ["S2"]


def test_rebuilt_index_equals_incremental(store_factory):
    # invariant 11: a rebuilt-from-scan index must match the incrementally
    # maintained one — across assignment + in-place edits, on both backends.
    s = store_factory()
    s.root = [
        Specimen(specimen_no="A", tags=["rare", "blue"]),
        Specimen(specimen_no="B", tags=["fine"]),
    ]
    s.commit()
    a = next(x for x in s.query(Specimen) if x.specimen_no == "A")
    a.tags.remove("blue")
    a.tags.append("fluorescent")
    s.commit()
    incremental = {
        needle: _nos(s.query(Specimen.tags.contains(needle)))
        for needle in ("rare", "blue", "fine", "fluorescent")
    }
    s.close()

    s2 = store_factory()  # fresh IndexManager -> rebuilt from a backend scan
    rebuilt = {
        needle: _nos(s2.query(Specimen.tags.contains(needle)))
        for needle in ("rare", "blue", "fine", "fluorescent")
    }
    s2.close()
    assert rebuilt == incremental
    assert incremental["blue"] == []  # the in-place removal stuck
