"""Cardinality-matched index cache (#12, ADR-005 amendment / Design A).

A **pure-Unique** field (Unique, not also Index/SortedIndex) carries NO eq
(bitmap) postings: its ``==``/``in_``/``contains``/``startswith`` answer from the
value→oid unique map, and the cache stores only that flat map — never the 6.2M
single-element bitmaps that made ``load ≈ rebuild``. A ``Unique+Index`` /
``Unique+SortedIndex`` field keeps its eq postings. Un-index on update stays
correct after a warm cache reload (the ``_last_values`` memory is reconstructed
from the unique map).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated

import msgspec.msgpack
import pytest

import datacrystal as dc
from datacrystal._entity import type_info
from datacrystal._indexes import ClassIndexes, plan


@dc.entity
class Specimen:
    """A cabinet specimen with a pure-Unique catalog number and a Unique+Index sku."""

    catalog: Annotated[str, dc.Unique]  # pure Unique: no eq postings
    sku: Annotated[str, dc.Unique, dc.Index]  # Unique + Index: keeps eq
    locality: Annotated[str | None, dc.Index] = None
    grade: Annotated[float, dc.SortedIndex] = 0.0


F = dc.fields(Specimen)


def _oids(bitmap: Iterable[int]) -> set[int]:
    return set(bitmap)


def _stock_six_specimens(store: dc.Store) -> None:
    for i in range(6):
        locality = "Tsumeb" if i % 2 else "Broken Hill"
        store.store(Specimen(catalog=f"C{i}", sku=f"S{i}", locality=locality, grade=float(i)))


def _reload(ci: ClassIndexes) -> ClassIndexes | None:
    blob = msgspec.msgpack.decode(msgspec.msgpack.encode(ci.dump()))
    return ClassIndexes.load(blob, type_info(Specimen))


@pytest.fixture
def built(store: dc.Store) -> ClassIndexes:
    """Return the class indexes of six committed specimens C0..C5 (odd ones at Tsumeb)."""
    _stock_six_specimens(store)
    store.commit()
    return store._index.ensure(type_info(Specimen))


@pytest.fixture
def loaded(built: ClassIndexes) -> ClassIndexes:
    """Return the same indexes after a cache dump and reload."""
    reloaded = _reload(built)
    if reloaded is None:
        pytest.fail("the dumped cache did not load back")
    return reloaded


def test_pure_unique_field_has_no_eq_postings(built: ClassIndexes) -> None:
    """Given six committed specimens whose catalog is pure Unique
    When the class indexes are built
    Then catalog has no eq postings
    And catalog is answered from the unique map
    """
    assert "catalog" not in built.eq
    assert "catalog" in built.unique


def test_unique_plus_index_field_keeps_its_eq_postings(built: ClassIndexes) -> None:
    """Given six committed specimens whose sku is Unique and Index
    When the class indexes are built
    Then sku has eq postings
    And sku is also in the unique map
    """
    assert "sku" in built.eq
    assert "sku" in built.unique


def test_unique_equality_is_answered_from_the_index(built: ClassIndexes) -> None:
    """Given six committed specimens C0..C5
    When catalog is compared equal to "C3"
    Then one specimen matches
    And no residual scan is left
    """
    bitmap, residual = plan(F.catalog == "C3", built)
    assert bitmap is not None

    assert len(_oids(bitmap)) == 1
    assert residual is None


def test_unique_equality_with_an_absent_key_matches_nothing(built: ClassIndexes) -> None:
    """Given six committed specimens C0..C5
    When catalog is compared equal to "absent"
    Then no specimen matches
    And no residual scan is left
    """
    bitmap, residual = plan(F.catalog == "absent", built)
    assert bitmap is not None

    assert len(_oids(bitmap)) == 0
    assert residual is None


def test_unique_in_is_answered_from_the_index(built: ClassIndexes) -> None:
    """Given six committed specimens C0..C5
    When catalog is tested for membership in ["C1", "C4", "nope"]
    Then two specimens match
    And no residual scan is left
    """
    bitmap, residual = plan(F.catalog.in_(["C1", "C4", "nope"]), built)
    assert bitmap is not None

    assert len(_oids(bitmap)) == 2
    assert residual is None


def test_unique_startswith_is_answered_from_the_index(built: ClassIndexes) -> None:
    """Given six committed specimens C0..C5
    When catalog starts with "C"
    Then six specimens match
    And no residual scan is left
    """
    bitmap, residual = plan(F.catalog.startswith("C"), built)
    assert bitmap is not None

    assert len(_oids(bitmap)) == 6
    assert residual is None


def test_unique_contains_is_answered_from_the_index(built: ClassIndexes) -> None:
    """Given six committed specimens C0..C5
    When catalog contains "3"
    Then one specimen matches
    And no residual scan is left
    """
    bitmap, residual = plan(F.catalog.contains("3"), built)
    assert bitmap is not None

    assert len(_oids(bitmap)) == 1
    assert residual is None


def test_get_by_pure_unique_key_finds_the_specimen(store: dc.Store, built: ClassIndexes) -> None:
    """Given six committed specimens C0..C5
    When a specimen is fetched by catalog "C2"
    Then its sku is "S2"
    """
    found = store.get(Specimen, catalog="C2")

    assert found is not None
    assert found.sku == "S2"


def test_query_by_pure_unique_key_finds_one_specimen(store: dc.Store, built: ClassIndexes) -> None:
    """Given six committed specimens C0..C5
    When catalog is queried equal to "C2"
    Then exactly one specimen is returned
    And its sku is "S2"
    """
    hits = store.query(F.catalog == "C2")

    assert len(hits) == 1
    assert hits[0].sku == "S2"


def test_explain_of_pure_unique_equality_is_indexed(store: dc.Store, built: ClassIndexes) -> None:
    """Given six committed specimens C0..C5
    When catalog equal to "C2" is explained
    Then the plan is indexed, not a full-extent scan
    """
    explained = store.explain(F.catalog == "C2")

    assert explained.indexed is True


def test_count_of_pure_unique_equality_is_one(store: dc.Store, built: ClassIndexes) -> None:
    """Given six committed specimens C0..C5
    When specimens with catalog "C2" are counted
    Then the count is 1
    """
    counted = store.count(F.catalog == "C2")

    assert counted == 1


def test_unique_and_index_conjunction_matches_the_intersection(built: ClassIndexes) -> None:
    """Given six committed specimens, C1 at Tsumeb
    When catalog "C1" and locality "Tsumeb" are required together
    Then one specimen matches
    """
    bitmap, _ = plan((F.catalog == "C1") & (F.locality == "Tsumeb"), built)
    assert bitmap is not None

    assert len(_oids(bitmap)) == 1


def test_unique_and_index_conjunction_with_the_wrong_locality_matches_nothing(
    built: ClassIndexes,
) -> None:
    """Given six committed specimens, C0 at Broken Hill
    When catalog "C0" and locality "Tsumeb" are required together
    Then no specimen matches
    """
    bitmap, _ = plan((F.catalog == "C0") & (F.locality == "Tsumeb"), built)
    assert bitmap is not None

    assert len(_oids(bitmap)) == 0


def test_unique_disjunction_matches_the_union(built: ClassIndexes) -> None:
    """Given six committed specimens C0..C5
    When catalog "C0" or catalog "C2" is accepted
    Then two specimens match
    """
    bitmap, _ = plan((F.catalog == "C0") | (F.catalog == "C2"), built)
    assert bitmap is not None

    assert len(_oids(bitmap)) == 2


def test_cache_dump_carries_no_eq_postings_for_a_pure_unique_field(built: ClassIndexes) -> None:
    """Given six committed specimens C0..C5
    When the class indexes are dumped for the cache
    Then catalog has no eq entry
    And sku has an eq entry
    """
    eq = built.dump()["eq"]

    assert "catalog" not in [name for name, _ in eq]
    assert "sku" in [name for name, _ in eq]


def test_cache_reload_keeps_the_extent(built: ClassIndexes, loaded: ClassIndexes) -> None:
    """Given six committed specimens C0..C5 and their reloaded cache
    When the extents are compared
    Then the reloaded extent equals the original
    """
    assert set(loaded.extent) == set(built.extent)


@pytest.mark.parametrize(
    ("field_name", "method", "argument"),
    [
        ("catalog", "__eq__", "C3"),
        ("catalog", "startswith", "C"),
        ("catalog", "contains", "4"),
        ("sku", "__eq__", "S2"),
        ("grade", "__ge__", 3.0),
        ("locality", "__eq__", "Tsumeb"),
    ],
    ids=["catalog-eq", "catalog-startswith", "catalog-contains", "sku-eq",
         "grade-ge", "locality-eq"],
)
def test_cache_reload_answers_queries_like_the_original(
    built: ClassIndexes, loaded: ClassIndexes, field_name: str, method: str, argument: object
) -> None:
    """Given six committed specimens C0..C5 and their reloaded cache
    When the same condition is planned on both
    Then the reloaded indexes match the same specimens as the original
    """
    condition = getattr(getattr(F, field_name), method)(argument)

    original_bitmap, original_residual = plan(condition, built)
    assert original_bitmap is not None
    loaded_bitmap, loaded_residual = plan(condition, loaded)
    assert loaded_bitmap is not None

    assert original_residual is None
    assert loaded_residual is None
    assert _oids(loaded_bitmap) == _oids(original_bitmap)


def test_cache_reload_answers_in_like_the_original(
    built: ClassIndexes, loaded: ClassIndexes
) -> None:
    """Given six committed specimens C0..C5 and their reloaded cache
    When catalog is tested for membership in ["C0", "C5"] on both
    Then the reloaded indexes match the same specimens as the original
    """
    condition = F.catalog.in_(["C0", "C5"])

    original_bitmap, original_residual = plan(condition, built)
    loaded_bitmap, loaded_residual = plan(condition, loaded)

    assert original_bitmap is not None
    assert loaded_bitmap is not None
    assert original_residual is None
    assert loaded_residual is None
    assert _oids(loaded_bitmap) == _oids(original_bitmap)


def test_updating_a_unique_key_on_a_reloaded_index_un_indexes_the_old_key(
    loaded: ClassIndexes,
) -> None:
    """Given a reloaded index where "C2" belongs to one specimen
    When that specimen's catalog changes to "C99"
    Then catalog "C2" matches nothing
    """
    oid = loaded.unique["catalog"]["C2"]

    loaded.insert(oid, {"catalog": "C99", "sku": "S2", "locality": "Tsumeb", "grade": 2.0})

    old_key_bitmap, _ = plan(F.catalog == "C2", loaded)
    assert old_key_bitmap is not None
    assert _oids(old_key_bitmap) == set()


def test_updating_a_unique_key_on_a_reloaded_index_indexes_the_new_key(
    loaded: ClassIndexes,
) -> None:
    """Given a reloaded index where "C2" belongs to one specimen
    When that specimen's catalog changes to "C99"
    Then catalog "C99" matches that specimen
    """
    oid = loaded.unique["catalog"]["C2"]

    loaded.insert(oid, {"catalog": "C99", "sku": "S2", "locality": "Tsumeb", "grade": 2.0})

    new_key_bitmap, _ = plan(F.catalog == "C99", loaded)
    assert new_key_bitmap is not None
    assert oid in _oids(new_key_bitmap)


def test_updating_a_unique_key_on_a_reloaded_index_cleans_the_unique_map(
    loaded: ClassIndexes,
) -> None:
    """Given a reloaded index where "C2" belongs to one specimen
    When that specimen's catalog changes to "C99"
    Then the unique map no longer holds "C2"
    And it maps "C99" to that specimen
    """
    oid = loaded.unique["catalog"]["C2"]

    loaded.insert(oid, {"catalog": "C99", "sku": "S2", "locality": "Tsumeb", "grade": 2.0})

    assert loaded.unique["catalog"].get("C2") is None
    assert loaded.unique["catalog"].get("C99") == oid


def test_a_reloaded_index_defers_the_last_values_rebuild(loaded: ClassIndexes) -> None:
    """Given a reloaded index that has seen no write
    When nothing is asked of it
    Then the last-values rebuild is still pending
    And no last values are held
    """
    assert loaded._needs_lv_rebuild is True
    assert loaded._last_values == {}


def test_a_read_does_not_trigger_the_last_values_rebuild(loaded: ClassIndexes) -> None:
    """Given a reloaded index that has seen no write
    When catalog "C1" is planned
    Then the last-values rebuild is still pending
    """
    plan(F.catalog == "C1", loaded)

    assert loaded._needs_lv_rebuild is True


def test_the_first_write_rebuilds_the_last_values(loaded: ClassIndexes) -> None:
    """Given a reloaded index that has seen no write
    When the specimen "C3" is rewritten as "C3b"
    Then the last-values rebuild is done
    """
    oid = loaded.unique["catalog"]["C3"]

    loaded.insert(oid, {"catalog": "C3b", "sku": "S3", "locality": "Tsumeb", "grade": 3.0})

    assert loaded._needs_lv_rebuild is False


def test_the_first_write_un_indexes_the_old_key_correctly(loaded: ClassIndexes) -> None:
    """Given a reloaded index that has seen no write
    When the specimen "C3" is rewritten as "C3b"
    Then catalog "C3" matches nothing
    And catalog "C3b" matches that specimen
    """
    oid = loaded.unique["catalog"]["C3"]

    loaded.insert(oid, {"catalog": "C3b", "sku": "S3", "locality": "Tsumeb", "grade": 3.0})

    old_bitmap, _ = plan(F.catalog == "C3", loaded)
    assert old_bitmap is not None
    new_bitmap, _ = plan(F.catalog == "C3b", loaded)
    assert new_bitmap is not None
    assert _oids(old_bitmap) == set()
    assert oid in _oids(new_bitmap)


def test_removing_a_specimen_from_a_reloaded_index_un_indexes_its_unique_key(
    loaded: ClassIndexes,
) -> None:
    """Given a reloaded index where "C4" belongs to one specimen
    When that specimen is removed
    Then catalog "C4" matches nothing
    And the unique map no longer holds "C4"
    And the extent no longer holds the specimen
    """
    oid = loaded.unique["catalog"]["C4"]

    loaded.remove(oid)

    gone_bitmap, _ = plan(F.catalog == "C4", loaded)
    assert gone_bitmap is not None
    assert _oids(gone_bitmap) == set()
    assert loaded.unique["catalog"].get("C4") is None
    assert oid not in loaded.extent
