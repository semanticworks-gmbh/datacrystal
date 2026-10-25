"""The decode-level read path (ROADMAP item 4, 2026-06-12 amendment).

count() / pluck() without entity construction, bulk unique-key get_many(),
and the loud-empty UnseenTypeWarning.
"""

from __future__ import annotations

import gc
import warnings

import pytest

import datacrystal as dc
from tests.conftest import Locality, Mineral

M = dc.fields(Mineral)


@pytest.fixture
def cabinet(store: dc.Store) -> dc.Store:
    """Commit quartz, azurite and topaz."""
    store.store(
        Mineral(
            qid="Q43010", name="quartz", crystal_system="trigonal", mohs=7.0, tags=["common"]
        )
    )
    store.store(Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic", mohs=3.8))
    store.store(Mineral(qid="Q134583", name="topaz", crystal_system="orthorhombic", mohs=8.0))
    store.commit()
    return store


@pytest.fixture
def azurite_at_tsumeb(store: dc.Store) -> Locality:
    """Commit azurite pointing lazily at Tsumeb Mine and return the locality."""
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.store(Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(tsumeb)))
    store.commit()
    return tsumeb


def _live_entities(store: dc.Store) -> int:
    """Count the entities the store holds live, after a garbage collection."""
    gc.collect()
    return len(store._registry)


# -- count ---------------------------------------------------------------------


def test_count_of_a_class_is_its_extent(cabinet: dc.Store) -> None:
    """Given a cabinet of three minerals
    When Mineral is counted
    Then the count is 3
    """
    count = cabinet.count(Mineral)

    assert count == 3


def test_count_of_an_indexed_equality_is_the_bitmap_cardinality(cabinet: dc.Store) -> None:
    """Given a cabinet with one trigonal mineral
    When crystal_system == "trigonal" is counted
    Then the count is 1
    """
    count = cabinet.count(M.crystal_system == "trigonal")

    assert count == 1


def test_count_of_an_indexed_in_set_sums_its_members(cabinet: dc.Store) -> None:
    """Given a cabinet with one trigonal and one monoclinic mineral
    When crystal_system in ["trigonal", "monoclinic"] is counted
    Then the count is 2
    """
    count = cabinet.count(M.crystal_system.in_(["trigonal", "monoclinic"]))

    assert count == 2


def test_count_of_an_absent_indexed_value_is_zero(cabinet: dc.Store) -> None:
    """Given a cabinet without a cubic mineral
    When crystal_system == "cubic" is counted
    Then the count is 0
    """
    count = cabinet.count(M.crystal_system == "cubic")

    assert count == 0


def test_count_of_a_residual_condition_scans(cabinet: dc.Store) -> None:
    """Given a cabinet where quartz (7.0) and topaz (8.0) are at least mohs 7
    When mohs >= 7.0 is counted
    Then the count is 2
    """
    count = cabinet.count(M.mohs >= 7.0)

    assert count == 2


def test_count_of_an_indexed_and_residual_conjunction(cabinet: dc.Store) -> None:
    """Given a cabinet with one trigonal mineral of mohs 7.0
    When crystal_system == "trigonal" and mohs >= 7.0 is counted
    Then the count is 1
    """
    count = cabinet.count((M.crystal_system == "trigonal") & (M.mohs >= 7.0))

    assert count == 1


def test_count_of_a_residual_condition_hydrates_nothing(cabinet: dc.Store) -> None:
    """Given a cabinet and the number of live entities beforehand
    When mohs >= 7.0 is counted
    Then no entity was constructed
    """
    live_before = _live_entities(cabinet)

    cabinet.count(M.mohs >= 7.0)

    assert _live_entities(cabinet) == live_before


def test_count_of_a_conjunction_hydrates_nothing(cabinet: dc.Store) -> None:
    """Given a cabinet and the number of live entities beforehand
    When crystal_system == "trigonal" and mohs >= 7.0 is counted
    Then no entity was constructed
    """
    live_before = _live_entities(cabinet)

    cabinet.count((M.crystal_system == "trigonal") & (M.mohs >= 7.0))

    assert _live_entities(cabinet) == live_before


def test_count_ignores_a_buffered_insert(cabinet: dc.Store) -> None:
    """Given a cabinet with a pending, uncommitted mineral
    When Mineral is counted
    Then the count is 3
    """
    cabinet.store(Mineral(qid="Q-pending", name="pending"))

    count = cabinet.count(Mineral)

    assert count == 3


def test_count_includes_a_committed_insert(cabinet: dc.Store) -> None:
    """Given a cabinet with a pending mineral that was then committed
    When Mineral is counted
    Then the count is 4
    """
    cabinet.store(Mineral(qid="Q-pending", name="pending"))
    cabinet.commit()

    count = cabinet.count(Mineral)

    assert count == 4


def test_count_ignores_a_buffered_delete(cabinet: dc.Store) -> None:
    """Given a cabinet of four minerals with one deletion pending
    When Mineral is counted
    Then the count is 4
    """
    cabinet.store(Mineral(qid="Q-pending", name="pending"))
    cabinet.commit()
    cabinet.delete(Mineral, qid="Q-pending")

    count = cabinet.count(Mineral)

    assert count == 4


def test_count_excludes_a_committed_delete(cabinet: dc.Store) -> None:
    """Given a cabinet of four minerals with one deletion committed
    When Mineral is counted
    Then the count is 3
    """
    cabinet.store(Mineral(qid="Q-pending", name="pending"))
    cabinet.commit()
    cabinet.delete(Mineral, qid="Q-pending")
    cabinet.commit()

    count = cabinet.count(Mineral)

    assert count == 3


def test_count_rejects_a_non_entity_target(store: dc.Store) -> None:
    """Given a store
    When the string "Mineral" is counted
    Then TypeError asks for an @entity class or a Condition
    """
    with pytest.raises(TypeError, match="@entity class or a Condition"):
        store.count("Mineral")  # pyrefly: ignore[bad-argument-type]


# -- pluck ---------------------------------------------------------------------


def test_pluck_of_one_field_returns_its_values(cabinet: dc.Store) -> None:
    """Given a cabinet of quartz, azurite and topaz
    When the name of every Mineral is plucked
    Then the names are azurite, quartz and topaz
    """
    names = cabinet.pluck(Mineral, "name")

    assert sorted(names) == ["azurite", "quartz", "topaz"]


def test_pluck_of_many_fields_returns_tuples(cabinet: dc.Store) -> None:
    """Given a cabinet where quartz (7.0) and topaz (8.0) are at least mohs 7
    When name and mohs are plucked for mohs >= 7.0
    Then the rows are ("quartz", 7.0) and ("topaz", 8.0)
    """
    rows = cabinet.pluck(M.mohs >= 7.0, "name", "mohs")

    assert sorted(rows) == [("quartz", 7.0), ("topaz", 8.0)]


def test_pluck_constructs_no_entities(cabinet: dc.Store) -> None:
    """Given a cabinet and the number of live entities beforehand
    When name and tags are plucked for every Mineral
    Then no entity was constructed
    """
    live_before = _live_entities(cabinet)

    cabinet.pluck(Mineral, "name", "tags")

    assert _live_entities(cabinet) == live_before


def test_pluck_refs_come_back_as_ref_tokens(
    store: dc.Store, azurite_at_tsumeb: Locality
) -> None:
    """Given azurite committed with a lazy type_locality
    When type_locality is plucked for azurite
    Then the single value is a dc.Ref
    """
    (ref,) = store.pluck(M.qid == "Q193563", "type_locality")

    assert isinstance(ref, dc.Ref)


def test_plucked_ref_resolves_through_get_many(
    store: dc.Store, azurite_at_tsumeb: Locality
) -> None:
    """Given the Ref plucked from azurite's type_locality
    When it is passed to get_many
    Then the single result is the identical Tsumeb Mine instance
    """
    (ref,) = store.pluck(M.qid == "Q193563", "type_locality")

    (resolved,) = store.get_many([ref])

    assert resolved is azurite_at_tsumeb


def test_pluck_rejects_an_unknown_field_name(cabinet: dc.Store) -> None:
    """Given a cabinet
    When the field "company_name" is plucked
    Then QueryError says it is not a persisted field
    """
    with pytest.raises(dc.QueryError, match="not a persisted field"):
        cabinet.pluck(Mineral, "company_name")


def test_pluck_rejects_a_call_without_fields(cabinet: dc.Store) -> None:
    """Given a cabinet
    When Mineral is plucked with no field names
    Then TypeError asks for at least one field
    """
    with pytest.raises(TypeError, match="at least one field"):
        cabinet.pluck(Mineral)


def test_pluck_returns_a_container_field_as_its_values(cabinet: dc.Store) -> None:
    """Given quartz tagged ["common"]
    When tags are plucked for quartz
    Then the value equals ["common"]
    """
    (tags,) = cabinet.pluck(M.qid == "Q43010", "tags")

    assert tags == ["common"]


def test_pluck_returns_a_container_field_as_a_plain_list(cabinet: dc.Store) -> None:
    """Given quartz tagged ["common"]
    When tags are plucked for quartz
    Then the value's type is exactly list
    """
    (tags,) = cabinet.pluck(M.qid == "Q43010", "tags")

    assert type(tags) is list


@pytest.fixture
def azurite_and_quartz(store: dc.Store) -> Locality:
    """Commit azurite at Tsumeb Mine and a quartz without a locality; return the locality."""
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.store(Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(tsumeb)))
    store.store(Mineral(qid="Q43010", name="quartz"))
    store.commit()
    return tsumeb


def test_count_matches_an_entity_valued_predicate(
    store: dc.Store, azurite_and_quartz: Locality
) -> None:
    """Given azurite at Tsumeb Mine and quartz without a locality
    When type_locality == Lazy.of(Tsumeb Mine) is counted
    Then the count is 1
    """
    count = store.count(M.type_locality == dc.Lazy.of(azurite_and_quartz))

    assert count == 1


def test_pluck_matches_an_entity_valued_predicate(
    store: dc.Store, azurite_and_quartz: Locality
) -> None:
    """Given azurite at Tsumeb Mine and quartz without a locality
    When name is plucked for type_locality == Lazy.of(Tsumeb Mine)
    Then the names are ["azurite"]
    """
    names = store.pluck(M.type_locality == dc.Lazy.of(azurite_and_quartz), "name")

    assert names == ["azurite"]


# -- bulk unique-key get_many ----------------------------------------------------


def test_get_many_by_unique_key_aligns_with_the_input(cabinet: dc.Store) -> None:
    """Given a cabinet of quartz and azurite
    When the qids "Q193563", "no-such" and "Q43010" are fetched with get_many
    Then the results are azurite, None and quartz in that order
    """
    quartz = cabinet.get(Mineral, qid="Q43010")
    azurite = cabinet.get(Mineral, qid="Q193563")

    got = cabinet.get_many(Mineral, qid=["Q193563", "no-such", "Q43010"])

    assert got == [azurite, None, quartz]


def test_get_many_by_key_rejects_a_non_unique_field(cabinet: dc.Store) -> None:
    """Given a cabinet
    When get_many is keyed by the non-unique field name
    Then QueryError says it is not a Unique field
    """
    with pytest.raises(dc.QueryError, match="not a Unique field"):
        cabinet.get_many(Mineral, name=["quartz"])


def test_get_many_by_key_rejects_a_non_class_target(cabinet: dc.Store) -> None:
    """Given a cabinet
    When get_many is given the list [1, 2] with a keyword key
    Then TypeError asks for an @entity class
    """
    with pytest.raises(TypeError, match="@entity class"):
        cabinet.get_many([1, 2], qid=["x"])


def test_get_many_accepts_snapshot_refs(cabinet: dc.Store) -> None:
    """Given a cabinet and a dc.Ref built from quartz's oid
    When the ref is passed to get_many
    Then the result is the quartz instance
    """
    quartz = cabinet.get(Mineral, qid="Q43010")
    oid = cabinet.store(quartz)

    got = cabinet.get_many([dc.Ref(oid)])

    assert got == [quartz]


# -- the loud empty ---------------------------------------------------------------


def test_query_of_an_unseen_type_warns_and_returns_empty(store: dc.Store) -> None:
    """Given a store with no committed Mineral
    When crystal_system == "trigonal" is queried
    Then UnseenTypeWarning says there are no committed records
    And the result is empty
    """
    with pytest.warns(dc.UnseenTypeWarning, match="no committed records"):
        hits = store.query(M.crystal_system == "trigonal")

    assert hits == []


def test_count_of_an_unseen_type_warns_and_returns_zero(store: dc.Store) -> None:
    """Given a store with no committed Mineral
    When Mineral is counted
    Then UnseenTypeWarning is raised
    And the count is 0
    """
    with pytest.warns(dc.UnseenTypeWarning):
        count = store.count(Mineral)

    assert count == 0


def test_pluck_of_an_unseen_type_warns_and_returns_empty(store: dc.Store) -> None:
    """Given a store with no committed Mineral
    When name is plucked for Mineral
    Then UnseenTypeWarning is raised
    And the result is empty
    """
    with pytest.warns(dc.UnseenTypeWarning):
        names = store.pluck(Mineral, "name")

    assert names == []


def test_get_of_an_unseen_type_stays_silent(store: dc.Store) -> None:
    """The get-or-create idiom must not warn.

    Given a store with no committed Mineral
    When quartz is fetched by its qid with all warnings turned into errors
    Then the result is None
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        found = store.get(Mineral, qid="Q43010")

    assert found is None


def test_get_many_of_an_unseen_type_stays_silent(store: dc.Store) -> None:
    """Given a store with no committed Mineral
    When get_many fetches one qid with all warnings turned into errors
    Then the result is [None]
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        found = store.get_many(Mineral, qid=["Q43010"])

    assert found == [None]


def test_count_of_a_seen_type_does_not_warn(cabinet: dc.Store) -> None:
    """Given a cabinet of three minerals
    When Mineral is counted with all warnings turned into errors
    Then the count is 3
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        count = cabinet.count(Mineral)

    assert count == 3


def test_query_of_a_seen_type_does_not_warn(cabinet: dc.Store) -> None:
    """Given a cabinet with one trigonal mineral
    When crystal_system == "trigonal" is queried with all warnings turned into errors
    Then one mineral is returned
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        hits = cabinet.query(M.crystal_system == "trigonal")

    assert len(hits) == 1
