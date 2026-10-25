"""Condition AST + bitmap planner (ROADMAP item 4).

These tests deliberately exercise the magic class-attribute path
(``Mineral.mohs >= 3.7``), which type checkers cannot model, so each such line carries a
Pyrefly line ignore with the error code. Checker-clean user code uses ``dc.fields()``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

import pytest

import datacrystal as dc
from tests.conftest import Locality, Mineral

StoreFactory = Callable[[], dc.Store]


@pytest.fixture
def cabinet(store: dc.Store) -> dc.Store:
    """Commit quartz, azurite, malachite, diamond and opal as the root list."""
    store.root = [
        Mineral(qid="Q1", name="quartz", crystal_system="trigonal", mohs=7.0),
        Mineral(qid="Q2", name="azurite", crystal_system="monoclinic", mohs=3.5),
        Mineral(qid="Q3", name="malachite", crystal_system="monoclinic", mohs=3.8),
        Mineral(qid="Q4", name="diamond", crystal_system="cubic", mohs=10.0),
        Mineral(qid="Q5", name="opal", crystal_system=None, mohs=5.5),
    ]
    store.commit()
    return store


def _names(hits: Iterable[Mineral]) -> list[str]:
    return sorted(m.name for m in hits)


def _oids(store: dc.Store, entities: Iterable[Mineral]) -> list[int]:
    return [store.store(entity) for entity in entities]


def _three_minerals() -> list[Mineral]:
    return [Mineral(qid=f"Q{i}", name=f"m{i}") for i in range(3)]


def _in_order(hits: Iterable[Mineral]) -> list[str]:
    return [m.name for m in hits]


def test_indexed_equality_finds_the_matching_minerals(cabinet: dc.Store) -> None:
    """Given a cabinet with azurite and malachite monoclinic
    When crystal_system == "monoclinic" is queried
    Then azurite and malachite are returned
    """
    # pyrefly: ignore[bad-argument-type]
    hits = cabinet.query(Mineral.crystal_system == "monoclinic")

    assert _names(hits) == ["azurite", "malachite"]


def test_fields_proxy_builds_the_same_conditions(cabinet: dc.Store) -> None:
    """Given a cabinet with malachite the only monoclinic mineral of at least mohs 3.7
    When (crystal_system == "monoclinic") & (mohs >= 3.7) is built with dc.fields and queried
    Then malachite is returned
    """
    fields = dc.fields(Mineral)

    hits = cabinet.query((fields.crystal_system == "monoclinic") & (fields.mohs >= 3.7))

    assert _names(hits) == ["malachite"]


def test_fields_proxy_rejects_an_unknown_field(cabinet: dc.Store) -> None:
    """Given the dc.fields proxy of Mineral
    When a field Mineral does not have is read
    Then AttributeError says there is no persisted field
    """
    fields = dc.fields(Mineral)

    with pytest.raises(AttributeError, match="no persisted field"):
        _ = fields.no_such_field


def test_fields_proxy_rejects_a_non_entity() -> None:
    """Given the class str
    When dc.fields is called with it
    Then NotAnEntityError is raised
    """
    with pytest.raises(dc.NotAnEntityError):
        dc.fields(str)


def test_and_of_indexed_and_residual(cabinet: dc.Store) -> None:
    """Given a cabinet with malachite the only monoclinic mineral of at least mohs 3.7
    When (crystal_system == "monoclinic") & (mohs >= 3.7) is queried
    Then malachite is returned
    """
    hits = cabinet.query(
        # pyrefly: ignore[bad-argument-type, unsupported-operation]
        (Mineral.crystal_system == "monoclinic") & (Mineral.mohs >= 3.7)
    )

    assert _names(hits) == ["malachite"]


def test_or_fully_indexed(cabinet: dc.Store) -> None:
    """Given a cabinet with diamond cubic and quartz trigonal
    When (crystal_system == "cubic") | (crystal_system == "trigonal") is queried
    Then diamond and quartz are returned
    """
    hits = cabinet.query(
        # pyrefly: ignore[bad-argument-type]
        (Mineral.crystal_system == "cubic") | (Mineral.crystal_system == "trigonal")
    )

    assert _names(hits) == ["diamond", "quartz"]


def test_or_with_residual_falls_back_to_scan(cabinet: dc.Store) -> None:
    """Given a cabinet with diamond cubic and azurite, malachite at most mohs 4.0
    When (crystal_system == "cubic") | (mohs <= 4.0) is queried
    Then azurite, diamond and malachite are returned
    """
    # pyrefly: ignore[bad-argument-type, unsupported-operation]
    hits = cabinet.query((Mineral.crystal_system == "cubic") | (Mineral.mohs <= 4.0))

    assert _names(hits) == ["azurite", "diamond", "malachite"]


def test_in_on_indexed_field(cabinet: dc.Store) -> None:
    """Given a cabinet with diamond cubic and quartz trigonal
    When crystal_system in ["cubic", "trigonal"] is queried
    Then diamond and quartz are returned
    """
    # pyrefly: ignore[missing-attribute]
    hits = cabinet.query(Mineral.crystal_system.in_(["cubic", "trigonal"]))

    assert _names(hits) == ["diamond", "quartz"]


def test_negated_equality_returns_the_rest_of_the_extent(cabinet: dc.Store) -> None:
    """Given a cabinet where azurite and malachite are monoclinic
    When ~(crystal_system == "monoclinic") is queried
    Then diamond, opal and quartz are returned
    """
    # pyrefly: ignore[bad-argument-type, deprecated]
    hits = cabinet.query(~(Mineral.crystal_system == "monoclinic"))

    assert _names(hits) == ["diamond", "opal", "quartz"]


def test_none_is_an_indexable_value(cabinet: dc.Store) -> None:
    """Given a cabinet where opal has no crystal system
    When crystal_system == None is queried
    Then opal is returned
    """
    # pyrefly: ignore[bad-argument-type]
    hits = cabinet.query(Mineral.crystal_system == None)  # noqa: E711 - the magic query path compares with ==

    assert _names(hits) == ["opal"]


def test_residual_only_query_scans_the_extent(cabinet: dc.Store) -> None:
    """Given a cabinet where diamond (10.0) and quartz (7.0) exceed mohs 6.0
    When mohs > 6.0 is queried
    Then diamond and quartz are returned
    """
    # pyrefly: ignore[bad-argument-type, unsupported-operation]
    hits = cabinet.query(Mineral.mohs > 6.0)

    assert _names(hits) == ["diamond", "quartz"]


def test_ordering_comparison_with_none_never_matches(cabinet: dc.Store) -> None:
    """Given a cabinet and a committed mineral "unknown" whose mohs is None
    When mohs >= 0.0 is queried
    Then "unknown" is not among the hits
    """
    cabinet.store(Mineral(qid="Q6", name="unknown", mohs=None))
    cabinet.commit()

    # pyrefly: ignore[bad-argument-type, unsupported-operation]
    hits = cabinet.query(Mineral.mohs >= 0.0)

    assert "unknown" not in _names(hits)


def test_cross_entity_condition_raises(cabinet: dc.Store) -> None:
    """Given a Mineral condition and a Locality condition joined by &
    When the joined condition is queried
    Then QueryError asks for exactly one entity class
    """
    with pytest.raises(dc.QueryError, match="exactly one entity class"):
        # pyrefly: ignore[bad-argument-type]
        cabinet.query((Mineral.crystal_system == "cubic") & (Locality.country == "Namibia"))


def test_query_before_any_records_returns_empty(store: dc.Store) -> None:
    """Empty is legitimate on a first run (2026-06-12).

    Given a store with no committed records
    When country == "Namibia" is queried
    Then the result is empty
    """
    with pytest.warns(dc.UnseenTypeWarning):
        # pyrefly: ignore[bad-argument-type]
        hits = store.query(Locality.country == "Namibia")

    assert hits == []


def test_query_before_any_records_warns(store: dc.Store) -> None:
    """The warning is the footgun guard: forgot to commit, or opened a different store file?

    Given a store with no committed records
    When country == "Namibia" is queried
    Then UnseenTypeWarning says there are no committed records
    """
    with pytest.warns(dc.UnseenTypeWarning, match="no committed records"):
        # pyrefly: ignore[bad-argument-type]
        store.query(Locality.country == "Namibia")


def test_uncommitted_changes_are_invisible_to_query(cabinet: dc.Store) -> None:
    """Given a cabinet with a cubic halite stored but not committed
    When crystal_system == "cubic" is queried
    Then only diamond is returned
    """
    cabinet.store(Mineral(qid="Q7", name="halite", crystal_system="cubic"))

    # pyrefly: ignore[bad-argument-type]
    hits = cabinet.query(Mineral.crystal_system == "cubic")

    assert _names(hits) == ["diamond"]


def test_committed_changes_become_visible_to_query(cabinet: dc.Store) -> None:
    """Given a cabinet with a cubic halite stored and committed
    When crystal_system == "cubic" is queried
    Then diamond and halite are returned
    """
    cabinet.store(Mineral(qid="Q7", name="halite", crystal_system="cubic"))
    cabinet.commit()

    # pyrefly: ignore[bad-argument-type]
    hits = cabinet.query(Mineral.crystal_system == "cubic")

    assert _names(hits) == ["diamond", "halite"]


def test_forgotten_parentheses_give_a_helpful_error() -> None:
    """Given the expression crystal_system == "cubic" & (mohs >= 1) without parentheses
    When it is evaluated
    Then QueryError mentions parentheses
    """
    with pytest.raises(dc.QueryError, match="parentheses"):
        # pyrefly: ignore[unsupported-operation]
        Mineral.crystal_system == "cubic" & (Mineral.mohs >= 1)  # noqa: B015


# -- contains / startswith (KICKOFF M4: distinct-key iteration) -----------------


def test_startswith_on_an_indexed_field_finds_matches(cabinet: dc.Store) -> None:
    """Given a cabinet with azurite and malachite monoclinic
    When crystal_system starts with "mono"
    Then azurite and malachite are returned
    """
    # pyrefly: ignore[missing-attribute]
    hits = cabinet.query(Mineral.crystal_system.startswith("mono"))

    assert _names(hits) == ["azurite", "malachite"]


def test_startswith_on_an_indexed_field_is_answered_from_the_index_alone(
    cabinet: dc.Store,
) -> None:
    """The None key in the cabinet's index is skipped, not a crash.

    Given a cabinet whose crystal_system index holds a None key for opal
    When the store explains crystal_system starts with "mono"
    Then the plan is indexed
    And no residual remains
    And the plan has 2 candidates
    """
    # pyrefly: ignore[missing-attribute]
    condition = Mineral.crystal_system.startswith("mono")

    query_plan = cabinet.explain(condition)

    assert query_plan.indexed
    assert query_plan.residual is None
    assert query_plan.candidates == 2


def test_contains_on_an_indexed_field_finds_matches(cabinet: dc.Store) -> None:
    """Given a cabinet with azurite and malachite monoclinic
    When crystal_system contains "clin"
    Then azurite and malachite are returned
    """
    # pyrefly: ignore[missing-attribute]
    hits = cabinet.query(Mineral.crystal_system.contains("clin"))

    assert _names(hits) == ["azurite", "malachite"]


def test_contains_on_an_indexed_field_counts_matches(cabinet: dc.Store) -> None:
    """Given a cabinet with azurite and malachite monoclinic
    When crystal_system contains "clin" is counted
    Then the count is 2
    """
    # pyrefly: ignore[missing-attribute]
    count = cabinet.count(Mineral.crystal_system.contains("clin"))

    assert count == 2


def test_contains_on_a_residual_field(cabinet: dc.Store) -> None:
    """Given a cabinet with azurite and malachite named ...ite
    When name contains "ite" is queried
    Then azurite and malachite are returned
    """
    # pyrefly: ignore[missing-attribute]
    hits = cabinet.query(Mineral.name.contains("ite"))

    assert _names(hits) == ["azurite", "malachite"]


def test_startswith_on_a_residual_field(cabinet: dc.Store) -> None:
    """Given a cabinet with quartz
    When name starts with "qua" is queried
    Then quartz is returned
    """
    # pyrefly: ignore[bad-argument-type]
    hits = cabinet.query(Mineral.name.startswith("qua"))

    assert _names(hits) == ["quartz"]


def test_pluck_with_a_contains_condition(cabinet: dc.Store) -> None:
    """Given a cabinet with azurite (Q2) and malachite (Q3) named ...ite
    When qid is plucked for name contains "ite"
    Then the qids are Q2 and Q3
    """
    # pyrefly: ignore[missing-attribute]
    qids = cabinet.pluck(Mineral.name.contains("ite"), "qid")

    assert qids == ["Q2", "Q3"]


def test_contains_is_case_sensitive(cabinet: dc.Store) -> None:
    """Linguistic matching (stemming, folding) is the datacrystal[fts] extra's job.

    Given a cabinet with azurite and malachite named ...ite
    When name contains "ITE" is queried
    Then nothing matches
    """
    # pyrefly: ignore[missing-attribute]
    hits = cabinet.query(Mineral.name.contains("ITE"))

    assert hits == []


def test_startswith_is_case_sensitive(cabinet: dc.Store) -> None:
    """Given a cabinet with quartz
    When name starts with "Qua" is queried
    Then nothing matches
    """
    # pyrefly: ignore[bad-argument-type]
    hits = cabinet.query(Mineral.name.startswith("Qua"))

    assert hits == []


def test_contains_skips_non_string_values(cabinet: dc.Store) -> None:
    """A non-str field value never matches, the SQL-NULL-like rule ordering comparisons follow.

    Given a cabinet where mohs is a float
    When mohs contains "7" is counted
    Then the count is 0
    """
    # pyrefly: ignore[missing-attribute]
    count = cabinet.count(Mineral.mohs.contains("7"))

    assert count == 0


def test_contains_skips_a_none_value(cabinet: dc.Store) -> None:
    """Given a cabinet where opal's crystal_system is None
    When crystal_system contains "o" is queried
    Then opal is not among the hits
    """
    # pyrefly: ignore[missing-attribute]
    hits = cabinet.query(Mineral.crystal_system.contains("o"))

    assert "opal" not in _names(hits)


def test_contains_validates_the_needle_type() -> None:
    """Given the name field
    When contains is called with 7
    Then QueryError says it takes a str
    """
    with pytest.raises(dc.QueryError, match="takes a str"):
        # pyrefly: ignore[missing-attribute]
        Mineral.name.contains(7)


def test_startswith_validates_the_needle_type() -> None:
    """Given the name field
    When startswith is called with None
    Then QueryError says it takes a str
    """
    with pytest.raises(dc.QueryError, match="takes a str"):
        # pyrefly: ignore[bad-argument-type]
        Mineral.name.startswith(None)


# -- query(type) symmetry + explain() (decided 2026-06-12, pre-freeze) ----------


def test_query_of_a_bare_class_hydrates_the_full_extent(cabinet: dc.Store) -> None:
    """query(Mineral) is the honest spelling of the expensive shape.

    It is symmetric with count(), pluck() and Snapshot.all().

    Given a cabinet of five minerals
    When Mineral is queried
    Then all five are returned
    """
    hits = cabinet.query(Mineral)

    assert _names(hits) == ["azurite", "diamond", "malachite", "opal", "quartz"]


def test_query_of_a_bare_class_returns_as_many_as_count(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is queried
    Then as many minerals come back as Mineral counts
    """
    hits = cabinet.query(Mineral)

    assert len(hits) == cabinet.count(Mineral)


def test_query_of_a_bare_unseen_class_is_empty(store: dc.Store) -> None:
    """Given a store with no committed Locality
    When Locality is queried
    Then the result is empty
    """
    with pytest.warns(dc.UnseenTypeWarning):
        hits = store.query(Locality)

    assert hits == []


def test_query_rejects_a_number(cabinet: dc.Store) -> None:
    """Given a cabinet
    When 42 is queried
    Then TypeError says query takes an @entity class or a Condition
    """
    with pytest.raises(TypeError, match="takes an @entity class or a Condition"):
        cabinet.query(42)  # pyrefly: ignore[bad-argument-type, no-matching-overload]


def test_query_rejects_a_non_entity_class(cabinet: dc.Store) -> None:
    """Given a cabinet
    When the class str is queried
    Then NotAnEntityError is raised
    """
    with pytest.raises(dc.NotAnEntityError):
        cabinet.query(str)


def test_explain_of_a_fully_indexed_condition(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals, two of them monoclinic
    When crystal_system == "monoclinic" is explained
    Then the plan is indexed
    And no residual remains
    And it has 2 candidates of an extent of 5
    """
    # pyrefly: ignore[bad-argument-type]
    fully_indexed = cabinet.explain(Mineral.crystal_system == "monoclinic")

    assert fully_indexed.indexed
    assert fully_indexed.residual is None
    assert fully_indexed.candidates == 2
    assert fully_indexed.extent == 5


def test_explain_of_a_mixed_condition(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals, two of them monoclinic
    When (crystal_system == "monoclinic") & (mohs >= 3.7) is explained
    Then the plan is indexed
    And a residual on mohs remains
    And the residual evaluates over 2 bitmap candidates
    """
    # pyrefly: ignore[bad-argument-type, unsupported-operation]
    mixed = cabinet.explain((Mineral.crystal_system == "monoclinic") & (Mineral.mohs >= 3.7))

    assert mixed.indexed
    assert mixed.residual is not None
    assert "mohs" in mixed.residual
    assert mixed.candidates == 2


def test_explain_of_an_unindexed_condition_is_the_perf_cliff(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When mohs >= 3.7 is explained
    Then the plan is not indexed
    And all 5 minerals are candidates
    And the text warns of NO index
    """
    # pyrefly: ignore[bad-argument-type, unsupported-operation]
    cliff = cabinet.explain(Mineral.mohs >= 3.7)

    assert not cliff.indexed
    assert cliff.candidates == 5
    assert cliff.extent == 5
    assert "NO index" in str(cliff)


def test_explain_of_a_bare_class_is_the_full_extent(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is explained
    Then the plan has no condition
    And all 5 minerals are candidates
    And the text names the full extent
    """
    bare = cabinet.explain(Mineral)

    assert bare.condition is None
    assert bare.candidates == 5
    assert bare.extent == 5
    assert "full extent" in str(bare)


def test_explain_bounds_the_hydration_of_query(cabinet: dc.Store) -> None:
    """Given the plan of (crystal_system == "monoclinic") & (mohs >= 3.7)
    When the same condition is queried
    Then no more minerals are returned than the plan has candidates
    """
    # pyrefly: ignore[unsupported-operation]
    condition = (Mineral.crystal_system == "monoclinic") & (Mineral.mohs >= 3.7)
    # pyrefly: ignore[bad-argument-type]
    explained = cabinet.explain(condition)

    # pyrefly: ignore[bad-argument-type]
    hits = cabinet.query(condition)

    assert len(hits) <= explained.candidates


def test_snapshot_query_returns_the_full_extent(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals and a snapshot
    When the snapshot queries Mineral
    Then all five names are returned
    """
    with cabinet.snapshot() as snap:
        views = snap.query(Mineral)

        assert sorted(v.name for v in views) == [
            "azurite",
            "diamond",
            "malachite",
            "opal",
            "quartz",
        ]


def test_snapshot_explain_reports_the_index_plan(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals, two monoclinic, and a snapshot
    When the snapshot explains crystal_system == "monoclinic"
    Then the plan is indexed
    And it has 2 candidates
    """
    with cabinet.snapshot() as snap:
        # pyrefly: ignore[bad-argument-type]
        explained = snap.explain(Mineral.crystal_system == "monoclinic")

        assert explained.indexed
        assert explained.candidates == 2


def test_explain_of_an_unseen_type_is_an_empty_plan(store: dc.Store) -> None:
    """Given a store with no committed Locality
    When Locality is explained
    Then UnseenTypeWarning is raised
    And the extent is 0
    And there are 0 candidates
    """
    with pytest.warns(dc.UnseenTypeWarning):
        explained = store.explain(Locality)

    assert explained.extent == 0
    assert explained.candidates == 0


# --- #14: limit / offset windowing ------------------------------------------
# The cabinet's OID order is quartz, azurite, malachite, diamond, opal.


def test_limit_takes_the_first_minerals(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is queried with limit=2
    Then quartz and azurite are returned
    """
    hits = cabinet.query(Mineral, limit=2)

    assert _in_order(hits) == ["quartz", "azurite"]


def test_limit_and_offset_take_a_middle_window(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is queried with limit=2 and offset=1
    Then azurite and malachite are returned
    """
    hits = cabinet.query(Mineral, limit=2, offset=1)

    assert _in_order(hits) == ["azurite", "malachite"]


def test_offset_alone_takes_the_tail(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is queried with offset=3
    Then diamond and opal are returned
    """
    hits = cabinet.query(Mineral, offset=3)

    assert _in_order(hits) == ["diamond", "opal"]


def test_limit_on_an_indexed_condition(cabinet: dc.Store) -> None:
    """Given a cabinet where azurite and malachite are monoclinic
    When crystal_system == "monoclinic" is queried with limit=1
    Then azurite is returned
    """
    # pyrefly: ignore[bad-argument-type]
    hits = cabinet.query(Mineral.crystal_system == "monoclinic", limit=1)

    assert _in_order(hits) == ["azurite"]


def test_offset_on_an_indexed_condition(cabinet: dc.Store) -> None:
    """Given a cabinet where azurite and malachite are monoclinic
    When crystal_system == "monoclinic" is queried with offset=1
    Then malachite is returned
    """
    # pyrefly: ignore[bad-argument-type]
    hits = cabinet.query(Mineral.crystal_system == "monoclinic", offset=1)

    assert _in_order(hits) == ["malachite"]


def test_limit_on_a_residual_condition(cabinet: dc.Store) -> None:
    """The window trims after the filter: mohs > 4.0 is a residual predicate.

    Given a cabinet where quartz, diamond and opal exceed mohs 4.0
    When mohs > 4.0 is queried with limit=1
    Then quartz is returned
    """
    # pyrefly: ignore[bad-argument-type, unsupported-operation]
    hits = cabinet.query(Mineral.mohs > 4.0, limit=1)

    assert _in_order(hits) == ["quartz"]


def test_offset_and_limit_on_a_residual_condition(cabinet: dc.Store) -> None:
    """Given a cabinet where quartz, diamond and opal exceed mohs 4.0
    When mohs > 4.0 is queried with offset=1 and limit=1
    Then diamond is returned
    """
    # pyrefly: ignore[bad-argument-type, unsupported-operation]
    hits = cabinet.query(Mineral.mohs > 4.0, offset=1, limit=1)

    assert _in_order(hits) == ["diamond"]


def test_limit_zero_returns_nothing(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is queried with limit=0
    Then the result is empty
    """
    hits = cabinet.query(Mineral, limit=0)

    assert hits == []


def test_offset_past_the_end_returns_nothing(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is queried with offset=999
    Then the result is empty
    """
    hits = cabinet.query(Mineral, offset=999)

    assert hits == []


def test_offset_past_the_end_with_a_limit_returns_nothing(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is queried with offset=999 and limit=5
    Then the result is empty
    """
    hits = cabinet.query(Mineral, offset=999, limit=5)

    assert hits == []


def test_negative_limit_is_rejected(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is queried with limit=-1
    Then ValueError is raised
    """
    with pytest.raises(ValueError, match="limit"):
        cabinet.query(Mineral, limit=-1)


def test_negative_offset_is_rejected(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is queried with offset=-1
    Then ValueError is raised
    """
    with pytest.raises(ValueError, match="offset"):
        cabinet.query(Mineral, offset=-1)


def test_string_limit_is_rejected(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is queried with limit="2"
    Then TypeError is raised
    """
    with pytest.raises(TypeError, match="limit"):
        cabinet.query(Mineral, limit="2")  # pyrefly: ignore[bad-argument-type]


def test_float_offset_is_rejected(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is queried with offset=1.5
    Then TypeError is raised
    """
    with pytest.raises(TypeError, match="offset"):
        cabinet.query(Mineral, offset=1.5)  # pyrefly: ignore[bad-argument-type]


def test_pluck_limit_takes_the_first_names(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When names are plucked with limit=2
    Then the names are quartz and azurite
    """
    names = cabinet.pluck(Mineral, "name", limit=2)

    assert names == ["quartz", "azurite"]


def test_pluck_offset_and_limit_take_a_middle_window(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When names are plucked with offset=1 and limit=2
    Then the names are azurite and malachite
    """
    names = cabinet.pluck(Mineral, "name", offset=1, limit=2)

    assert names == ["azurite", "malachite"]


def test_pluck_limit_zero_returns_nothing(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When names are plucked with limit=0
    Then the result is empty
    """
    names = cabinet.pluck(Mineral, "name", limit=0)

    assert names == []


def test_count_takes_no_window(cabinet: dc.Store) -> None:
    """count() is unchanged: it never grows a window.

    Given a cabinet where azurite and malachite are monoclinic
    When crystal_system == "monoclinic" is counted
    Then the count is 2
    """
    # pyrefly: ignore[bad-argument-type]
    count = cabinet.count(Mineral.crystal_system == "monoclinic")

    assert count == 2


def test_explain_takes_no_window(cabinet: dc.Store) -> None:
    """The plan never grows a window.

    Given a cabinet where azurite and malachite are monoclinic
    When crystal_system == "monoclinic" is explained
    Then the plan is indexed
    And it has 2 candidates
    """
    # pyrefly: ignore[bad-argument-type]
    explained = cabinet.explain(Mineral.crystal_system == "monoclinic")

    assert explained.indexed
    assert explained.candidates == 2


def test_snapshot_query_limit(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals and a snapshot
    When the snapshot queries Mineral with limit=2
    Then quartz and azurite are returned
    """
    with cabinet.snapshot() as snap:
        views = snap.query(Mineral, limit=2)

        # pyrefly: ignore[bad-argument-type]
        assert _in_order(views) == ["quartz", "azurite"]


def test_snapshot_query_offset_and_limit(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals and a snapshot
    When the snapshot queries Mineral with offset=1 and limit=2
    Then azurite and malachite are returned
    """
    with cabinet.snapshot() as snap:
        views = snap.query(Mineral, offset=1, limit=2)

        # pyrefly: ignore[bad-argument-type]
        assert _in_order(views) == ["azurite", "malachite"]


def test_snapshot_all_limit(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals and a snapshot
    When the snapshot reads all Minerals with limit=2
    Then quartz and azurite are returned
    """
    with cabinet.snapshot() as snap:
        views = snap.all(Mineral, limit=2)

        # pyrefly: ignore[bad-argument-type]
        assert _in_order(views) == ["quartz", "azurite"]


def test_snapshot_all_offset(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals and a snapshot
    When the snapshot reads all Minerals with offset=3
    Then diamond and opal are returned
    """
    with cabinet.snapshot() as snap:
        views = snap.all(Mineral, offset=3)

        # pyrefly: ignore[bad-argument-type]
        assert _in_order(views) == ["diamond", "opal"]


def test_snapshot_residual_path_windows_too(cabinet: dc.Store) -> None:
    """Given a cabinet where azurite and malachite are monoclinic, and a snapshot
    When the snapshot queries crystal_system == "monoclinic" with limit=1
    Then azurite is returned
    """
    with cabinet.snapshot() as snap:
        # pyrefly: ignore[bad-argument-type]
        views = snap.query(Mineral.crystal_system == "monoclinic", limit=1)

        # pyrefly: ignore[bad-argument-type]
        assert _in_order(views) == ["azurite"]


def test_snapshot_negative_offset_is_rejected(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals and a snapshot
    When the snapshot reads all Minerals with offset=-1
    Then ValueError is raised
    """
    with cabinet.snapshot() as snap, pytest.raises(ValueError, match="offset"):
        snap.all(Mineral, offset=-1)


# --- #15: store.query_iter() streaming query ---------------------------------------


def test_query_iter_yields_the_oids_query_returns(cabinet: dc.Store) -> None:
    """Given a cabinet where azurite and malachite are monoclinic
    When crystal_system == "monoclinic" is streamed and queried
    Then both yield the same set of oids
    """
    # pyrefly: ignore[bad-argument-type]
    streamed = list(cabinet.query_iter(Mineral.crystal_system == "monoclinic"))
    # pyrefly: ignore[bad-argument-type]
    queried = cabinet.query(Mineral.crystal_system == "monoclinic")

    assert {cabinet.store(m) for m in streamed} == {cabinet.store(m) for m in queried}


def test_query_iter_of_the_full_extent_yields_every_oid_once(cabinet: dc.Store) -> None:
    """Given a cabinet of five minerals
    When Mineral is streamed in full
    Then the oids equal those of query(Mineral)
    And none repeats
    """
    streamed_oids = _oids(cabinet, cabinet.query_iter(Mineral))
    queried_oids = _oids(cabinet, cabinet.query(Mineral))

    assert sorted(streamed_oids) == sorted(queried_oids)
    assert len(streamed_oids) == len(set(streamed_oids))


def test_query_iter_guards_every_next_against_a_foreign_thread(cabinet: dc.Store) -> None:
    """Given a stream of which the owner pulled one mineral
    When a foreign thread pulls the next one
    Then WrongThreadError is raised
    """
    stream = cabinet.query_iter(Mineral)
    next(stream)

    with ThreadPoolExecutor(max_workers=1) as pool:
        attempt = pool.submit(next, stream)

        with pytest.raises(dc.WrongThreadError):
            attempt.result(timeout=10)


def test_query_iter_stops_when_the_store_is_closed_mid_stream(store_factory: StoreFactory) -> None:
    """Given a stream of three minerals of which one was pulled
    When the store is closed and the next one is pulled
    Then StoreClosedError is raised
    """
    store = store_factory()
    store.root = _three_minerals()
    store.commit()
    stream = store.query_iter(Mineral)
    next(stream)
    store.close()

    with pytest.raises(dc.StoreClosedError):
        next(stream)


def test_query_iter_excludes_uncommitted(store: dc.Store) -> None:
    """Given a committed quartz and a topaz stored but not committed
    When Mineral is streamed
    Then only quartz is yielded
    """
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()
    store.store(Mineral(qid="Q2", name="topaz"))

    streamed = store.query_iter(Mineral)

    assert {m.name for m in streamed} == {"quartz"}
