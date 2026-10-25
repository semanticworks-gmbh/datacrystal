"""Additive schema evolution: type lineage, decode-by-name, defaults.

Each changed field shape of a class gets a fresh cid (a new lineage row in
the type dictionary); old records stay decodable through their own shape
forever. On load, fields added to the class are filled from their dataclass
defaults, fields removed from the class are ignored, and a new field WITHOUT
a default refuses loudly. A rename is remove+add: the old values are dropped.

The tests simulate "the code changed between runs" by re-creating an entity
class with the same module/qualname (→ same typename) but different fields.
Dynamic class fabrication is untypeable, so the fabricated classes are `type[Any]`.
"""

from __future__ import annotations

import dataclasses
import operator
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Annotated, Any

import pytest

import datacrystal as dc

REQUIRED = object()
StoreFactory = Callable[[], dc.Store]


def _evolve(**fields: tuple[object, object]) -> type[Any]:
    """(Re)define the canonical 'Evolving' entity class with these fields.

    Values are (annotation, default); default REQUIRED means no default.
    """
    annotations: dict = {}
    namespace: dict = {
        "__module__": __name__,
        "__qualname__": "Evolving",
        "__annotations__": annotations,
    }
    for name, (annotation, default) in fields.items():
        annotations[name] = annotation
        if default is not REQUIRED:
            namespace[name] = default
    return dc.entity(type("Evolving", (), namespace))


def _latitude_of_coords(old: dict) -> float:
    return float(old["coords"].split(",")[0])


def _longitude_of_coords(old: dict) -> float:
    return float(old["coords"].split(",")[1])


def _never_fires(old: dict) -> float:
    return 999.0


def _full_name_of(old: dict) -> str:
    return f"{old['first']} {old['last']}"


@pytest.fixture
def quartz_stored_as_v1(store_factory: StoreFactory) -> None:
    """Store a one-field V1 quartz as the root list, then close the store."""
    v1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    store.root = [v1(name="quartz")]
    store.commit()
    store.close()


def _stored_v1_names(store_factory: StoreFactory, *names: str) -> None:
    """Store one V1 record per name as a standalone entity, then close the store."""
    v1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    for name in names:
        store.store(v1(name=name))
    store.commit()
    store.close()


def _stored_v1_names_as_root(store_factory: StoreFactory, *names: str) -> None:
    """Store one V1 record per name in the root list, then close the store."""
    v1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    store.root = [v1(name=name) for name in names]
    store.commit()
    store.close()


def _lineage_typenames(store: dc.Store) -> list[str]:
    """Return the typename of every lineage row the store holds, a shape's row each."""
    with store.snapshot() as snap:
        return [typename for _cid, typename, _fields in snap.types]


def _names(records: Iterable[Any]) -> list[str]:
    return sorted(record.name for record in records)


def _mohs_values(records: Iterable[Any]) -> list[float | None]:
    return [record.mohs for record in records]


# -- added fields -------------------------------------------------------------


@pytest.mark.usefixtures("quartz_stored_as_v1")
def test_added_field_with_default_reads_its_default_on_an_old_record(
    store_factory: StoreFactory,
) -> None:
    """Given a quartz stored before the class gained a mohs field defaulting to None
    When the store is reopened
    Then the record is an instance of the new shape
    And its name is quartz
    And its mohs is None
    """
    v2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    reopened = store_factory()

    quartz = reopened.root[0]

    assert isinstance(quartz, v2)
    assert quartz.name == "quartz"
    assert quartz.mohs is None
    reopened.close()


@pytest.mark.usefixtures("quartz_stored_as_v1")
def test_assigning_the_added_field_persists(store_factory: StoreFactory) -> None:
    """Given a quartz stored before the class gained a mohs field, reopened
    When mohs is set to 7.0, committed, and the store reopened again
    Then mohs is 7.0
    """
    _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    reopened = store_factory()
    reopened.root[0].mohs = 7.0
    reopened.commit()
    reopened.close()
    third = store_factory()

    mohs = third.root[0].mohs

    assert mohs == 7.0
    third.close()


@pytest.mark.usefixtures("quartz_stored_as_v1")
def test_added_field_with_default_factory_fills_a_tracked_empty_list(
    store_factory: StoreFactory,
) -> None:
    """Given a quartz stored before the class gained a tags list field
    When the store is reopened
    Then the quartz tags are an empty list
    And they are a PersistentList
    """
    _evolve(name=(str, REQUIRED), tags=(list, dataclasses.field(default_factory=list)))
    reopened = store_factory()

    quartz = reopened.root[0]

    assert quartz.tags == []
    assert isinstance(quartz.tags, dc.PersistentList)
    reopened.close()


@pytest.mark.usefixtures("quartz_stored_as_v1")
def test_filled_default_list_is_tracked_like_any_list(store_factory: StoreFactory) -> None:
    """Given a quartz stored before the class gained a tags list field, reopened
    When "fresh" is appended to its tags, committed, and the store reopened again
    Then the tags are ["fresh"]
    """
    _evolve(name=(str, REQUIRED), tags=(list, dataclasses.field(default_factory=list)))
    reopened = store_factory()
    reopened.root[0].tags.append("fresh")
    reopened.commit()
    reopened.close()
    third = store_factory()

    tags = third.root[0].tags

    assert tags == ["fresh"]
    third.close()


@pytest.mark.usefixtures("quartz_stored_as_v1")
def test_added_field_without_default_refuses(store_factory: StoreFactory) -> None:
    """Given a quartz stored before the class gained a required serial field
    When the old record is read
    Then SchemaMismatchError says serial has no default
    """
    _evolve(name=(str, REQUIRED), serial=(int, REQUIRED))
    reopened = store_factory()

    with pytest.raises(dc.SchemaMismatchError, match=r"serial.*no default"):
        _ = reopened.root[0]
    reopened.close()


# -- removed, reordered and renamed fields ---------------------------------


def test_removed_field_keeps_the_name(store_factory: StoreFactory) -> None:
    """Given a quartz stored with name and mohs, and the class later dropped mohs
    When the store is reopened
    Then the name is still quartz
    """
    v2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    store = store_factory()
    store.root = [v2(name="quartz", mohs=7.0)]
    store.commit()
    store.close()
    _evolve(name=(str, REQUIRED))
    reopened = store_factory()

    quartz = reopened.root[0]

    assert quartz.name == "quartz"
    reopened.close()


def test_removed_field_is_ignored(store_factory: StoreFactory) -> None:
    """Given a quartz stored with name and mohs, and the class later dropped mohs
    When the store is reopened
    Then the record has no mohs attribute
    """
    v2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    store = store_factory()
    store.root = [v2(name="quartz", mohs=7.0)]
    store.commit()
    store.close()
    _evolve(name=(str, REQUIRED))
    reopened = store_factory()

    quartz = reopened.root[0]

    assert not hasattr(quartz, "mohs")
    reopened.close()


def test_reordered_fields_map_by_name(store_factory: StoreFactory) -> None:
    """Given a record stored with fields a then b, and the class later declared b then a
    When the store is reopened
    Then a is "alpha"
    And b is 2
    """
    v1 = _evolve(a=(str, REQUIRED), b=(int, REQUIRED))
    store = store_factory()
    store.root = v1(a="alpha", b=2)
    store.commit()
    store.close()
    _evolve(b=(int, REQUIRED), a=(str, REQUIRED))
    reopened = store_factory()

    record = reopened.root

    assert record.a == "alpha"
    assert record.b == 2
    reopened.close()


def test_rename_without_a_marker_drops_the_old_value(store_factory: StoreFactory) -> None:
    """Given a quartz stored with a label, and the class later renamed label to title
    When the store is reopened
    Then the title is None, the old label value being gone
    (documented behavior: a rename is remove+add, explicit migrations are post-v0.1)
    """
    v1 = _evolve(label=(str, REQUIRED))
    store = store_factory()
    store.root = [v1(label="quartz")]
    store.commit()
    store.close()
    _evolve(title=(str | None, None))
    reopened = store_factory()

    title = reopened.root[0].title

    assert title is None
    reopened.close()


# -- added index and unique fields ----------------------------------------


def _add_crystal_system_index() -> type[Any]:
    return _evolve(
        name=(str, REQUIRED), crystal_system=(Annotated[str | None, dc.Index], None)
    )


def test_added_index_field_matches_every_old_record_on_none(store_factory: StoreFactory) -> None:
    """Given quartz and azurite stored before the class gained an indexed crystal_system
    When the store is reopened and crystal_system None is queried
    Then both old records match
    """
    _stored_v1_names_as_root(store_factory, "quartz", "azurite")
    v2 = _add_crystal_system_index()
    reopened = store_factory()

    hits = reopened.query(operator.eq(v2.crystal_system, None))

    assert len(hits) == 2
    reopened.close()


def test_added_index_field_finds_the_record_it_was_assigned_on(
    store_factory: StoreFactory,
) -> None:
    """Given quartz and azurite stored before the class gained an indexed crystal_system
    When quartz is set to trigonal, committed, and the store reopened
    Then querying trigonal returns quartz
    """
    _stored_v1_names_as_root(store_factory, "quartz", "azurite")
    v2 = _add_crystal_system_index()
    reopened = store_factory()
    reopened.root[0].crystal_system = "trigonal"
    reopened.commit()
    reopened.close()
    third = store_factory()

    hits = third.query(v2.crystal_system == "trigonal")

    assert [m.name for m in hits] == ["quartz"]
    third.close()


def test_added_index_field_still_matches_the_unassigned_on_none(
    store_factory: StoreFactory,
) -> None:
    """Given quartz and azurite stored before the class gained an indexed crystal_system
    When quartz is set to trigonal, committed, and the store reopened
    Then querying None matches only the other record
    """
    _stored_v1_names_as_root(store_factory, "quartz", "azurite")
    v2 = _add_crystal_system_index()
    reopened = store_factory()
    reopened.root[0].crystal_system = "trigonal"
    reopened.commit()
    reopened.close()
    third = store_factory()

    hits = third.query(operator.eq(v2.crystal_system, None))

    assert len(hits) == 1
    third.close()


@pytest.mark.usefixtures("quartz_stored_as_v1")
def test_added_unique_field_defaulting_none_finds_nothing_before_assignment(
    store_factory: StoreFactory,
) -> None:
    """Given a quartz stored before the class gained a unique qid defaulting to None
    When the store is reopened and a mineral is fetched by qid Q43010
    Then none is found
    """
    v2 = _evolve(name=(str, REQUIRED), qid=(Annotated[str | None, dc.Unique], None))
    reopened = store_factory()

    found = reopened.get(v2, qid="Q43010")

    assert found is None
    reopened.close()


@pytest.mark.usefixtures("quartz_stored_as_v1")
def test_added_unique_field_is_fetchable_once_assigned(store_factory: StoreFactory) -> None:
    """Given a quartz stored before the class gained a unique qid defaulting to None
    When qid Q43010 is assigned, committed, and the store reopened
    Then fetching by qid Q43010 returns quartz
    """
    v2 = _evolve(name=(str, REQUIRED), qid=(Annotated[str | None, dc.Unique], None))
    reopened = store_factory()
    reopened.root[0].qid = "Q43010"
    reopened.commit()
    reopened.close()
    third = store_factory()

    found = third.get(v2, qid="Q43010")

    assert found is not None
    assert found.name == "quartz"
    third.close()


@pytest.mark.usefixtures("quartz_stored_as_v1")
def test_added_unique_field_with_non_none_default_refuses(store_factory: StoreFactory) -> None:
    """Given a quartz stored before the class gained a unique code defaulting to "X"
    When the store is reopened and a mineral is fetched by code "X"
    Then SchemaMismatchError says the unique field must default to None
    """
    v2 = _evolve(name=(str, REQUIRED), code=(Annotated[str | None, dc.Unique], "X"))
    reopened = store_factory()

    with pytest.raises(dc.SchemaMismatchError, match="must default to None"):
        reopened.get(v2, code="X")
    reopened.close()


# -- the legacy types table ------------------------------------------------


def _legacy_store_directory(tmp_path: Path) -> Path:
    """Create a store directory whose types table still carries UNIQUE(name)."""
    directory = tmp_path / "store"
    directory.mkdir()
    conn = sqlite3.connect(directory / "data.sqlite")
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE types (
            cid INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, fields TEXT NOT NULL);
        CREATE TABLE objects (
            oid INTEGER PRIMARY KEY, cid INTEGER NOT NULL, tid INTEGER NOT NULL,
            payload BLOB NOT NULL, crc INTEGER NOT NULL);
        CREATE INDEX objects_by_cid ON objects (cid);
        INSERT INTO meta VALUES ('format_version', '1');
        """
    )
    conn.close()
    return directory


def test_legacy_unique_types_table_is_migrated(tmp_path: Path) -> None:
    """Given a store created before schema evolution, its types table UNIQUE on name
    And a quartz committed in it under one field shape
    When a class with an added mohs field commits an azurite of the new shape and the store reopens
    Then the root holds azurite and quartz, the lineage rows having shared a name
    """
    directory = _legacy_store_directory(tmp_path)
    v1 = _evolve(name=(str, REQUIRED))
    first = dc.Store.open(directory)
    first.root = [v1(name="quartz")]
    first.commit()
    first.close()
    v2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    second = dc.Store.open(directory)
    second.root.append(v2(name="azurite", mohs=3.5))
    second.commit()
    second.close()

    reopened = dc.Store.open(directory)

    assert _names(reopened.root) == ["azurite", "quartz"]
    reopened.close()


# -- the lineage ---------------------------------------------------------------


@pytest.mark.usefixtures("quartz_stored_as_v1")
def test_a_stored_shape_has_one_lineage_row(store_factory: StoreFactory) -> None:
    """Given a quartz stored under one field shape
    When the store is reopened under a class with an added mohs field
    Then the typename has one lineage row
    """
    _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    reopened = store_factory()

    typenames = _lineage_typenames(reopened)

    assert typenames.count(f"{__name__}:Evolving") == 1
    reopened.close()


@pytest.mark.usefixtures("quartz_stored_as_v1")
def test_a_new_shape_gets_its_own_lineage_row(store_factory: StoreFactory) -> None:
    """Given a quartz stored under one field shape, reopened under a class with a mohs field
    When an azurite of the new shape is committed
    Then the typename has two lineage rows
    """
    v2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    reopened = store_factory()
    reopened.root.append(v2(name="azurite", mohs=3.5))

    reopened.commit()

    typenames = _lineage_typenames(reopened)

    assert typenames.count(f"{__name__}:Evolving") == 2
    reopened.close()


@pytest.mark.usefixtures("quartz_stored_as_v1")
def test_an_old_record_still_decodes_through_its_own_lineage_row(
    store_factory: StoreFactory,
) -> None:
    """Given a quartz of the old shape and a committed azurite of the new shape
    When the root's names are read
    Then they are azurite and quartz
    """
    v2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    reopened = store_factory()
    reopened.root.append(v2(name="azurite", mohs=3.5))
    reopened.commit()

    assert _names(reopened.root) == ["azurite", "quartz"]
    reopened.close()


# -- RenamedFrom -----------------------------------------------------------


def test_renamed_from_binds_the_old_column(store_factory: StoreFactory) -> None:
    """Given a quartz stored with hardness 7.0, and the class later renamed hardness to mohs
    via RenamedFrom
    When the store is reopened
    Then the record is an instance of the new shape
    And its mohs is 7.0, the old hardness value following the rename
    """
    v1 = _evolve(name=(str, REQUIRED), hardness=(float | None, None))
    store = store_factory()
    store.root = [v1(name="quartz", hardness=7.0)]
    store.commit()
    store.close()
    v2 = _evolve(
        name=(str, REQUIRED),
        mohs=(Annotated[float | None, dc.RenamedFrom("hardness")], None),
    )
    reopened = store_factory()

    quartz = reopened.root[0]

    assert isinstance(quartz, v2)
    assert quartz.mohs == 7.0
    reopened.close()


def test_renamed_from_prefers_the_new_name_when_present(store_factory: StoreFactory) -> None:
    """Given an opal stored under the new name mohs 5.5, which also carries RenamedFrom hardness
    When the store is reopened
    Then mohs is 5.5, the new column winning over the old
    """
    v2 = _evolve(
        name=(str, REQUIRED),
        mohs=(Annotated[float | None, dc.RenamedFrom("hardness")], None),
    )
    store = store_factory()
    store.root = [v2(name="opal", mohs=5.5)]
    store.commit()
    store.close()
    reopened = store_factory()

    mohs = reopened.root[0].mohs

    assert mohs == 5.5
    reopened.close()


# -- Glue --------------------------------------------------------------------


def _stored_tsumeb_with_coords(store_factory: StoreFactory) -> None:
    v1 = _evolve(name=(str, REQUIRED), coords=(str, REQUIRED))
    store = store_factory()
    store.root = [v1(name="Tsumeb", coords="48.1,11.5")]
    store.commit()
    store.close()


def test_glue_splits_an_old_field_into_two(store_factory: StoreFactory) -> None:
    """Given a Tsumeb record stored with coords "48.1,11.5", and the class later split coords
    into lat and lon via Glue
    When the store is reopened
    Then the record is an instance of the new shape
    And (lat, lon) is (48.1, 11.5), both derived from the old coords
    """
    _stored_tsumeb_with_coords(store_factory)
    v2 = _evolve(
        name=(str, REQUIRED),
        lat=(Annotated[float, dc.Glue(_latitude_of_coords)], 0.0),
        lon=(Annotated[float, dc.Glue(_longitude_of_coords)], 0.0),
    )
    reopened = store_factory()

    loc = reopened.root[0]

    assert isinstance(loc, v2)
    assert (loc.lat, loc.lon) == (48.1, 11.5)
    reopened.close()


def test_glue_does_not_fire_when_the_field_is_already_present(
    store_factory: StoreFactory,
) -> None:
    """Given an opal stored in the new shape with lat 12.5, whose lat carries a Glue
    When the store is reopened
    Then lat is 12.5, the persisted value winning and the glue never firing
    """
    v2 = _evolve(
        name=(str, REQUIRED),
        lat=(Annotated[float, dc.Glue(_never_fires)], 0.0),
    )
    store = store_factory()
    store.root = [v2(name="opal", lat=12.5)]
    store.commit()
    store.close()
    reopened = store_factory()

    lat = reopened.root[0].lat

    assert lat == 12.5
    reopened.close()


def test_glue_merges_multiple_old_fields(store_factory: StoreFactory) -> None:
    """Given a record stored with first "Marie" and last "Curie", and the class later merged
    them into full_name via Glue, which sees the whole old record
    When the store is reopened
    Then the record is an instance of the new shape
    And its full_name is "Marie Curie"
    """
    v1 = _evolve(first=(str, REQUIRED), last=(str, REQUIRED))
    store = store_factory()
    store.root = [v1(first="Marie", last="Curie")]
    store.commit()
    store.close()
    v2 = _evolve(full_name=(Annotated[str, dc.Glue(_full_name_of)], ""))
    reopened = store_factory()

    merged = reopened.root[0]

    assert isinstance(merged, v2)
    assert merged.full_name == "Marie Curie"
    reopened.close()


def test_glue_applies_at_decode_level_through_pluck(store_factory: StoreFactory) -> None:
    """Given two records stored with coords "1.0,2.0" and "3.0,4.0", and the class later
    derived lat from coords via Glue
    When lat is plucked
    Then the values are 1.0 and 3.0, derived at decode level
    """
    v1 = _evolve(name=(str, REQUIRED), coords=(str, REQUIRED))
    store = store_factory()
    store.store(v1(name="a", coords="1.0,2.0"))
    store.store(v1(name="b", coords="3.0,4.0"))
    store.commit()
    store.close()
    v2 = _evolve(
        name=(str, REQUIRED),
        lat=(Annotated[float, dc.Glue(_latitude_of_coords)], 0.0),
    )
    reopened = store_factory()

    lats = sorted(reopened.pluck(v2, "lat"))

    assert lats == [1.0, 3.0]
    reopened.close()


# -- migrate -----------------------------------------------------------------


def test_migrate_rewrites_every_old_shape_record(store_factory: StoreFactory) -> None:
    """Given quartz, azurite and opal stored under the old shape, the class later gained mohs
    When the store is migrated
    Then three records were rewritten
    """
    _stored_v1_names(store_factory, "quartz", "azurite", "opal")
    _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    store = store_factory()

    rewritten = store.migrate()

    assert rewritten == 3
    store.close()


def test_migrate_is_idempotent(store_factory: StoreFactory) -> None:
    """Given three old-shape records already migrated once
    When the store is migrated a second time
    Then no record is rewritten
    """
    _stored_v1_names(store_factory, "quartz", "azurite", "opal")
    _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    store = store_factory()
    store.migrate()

    rewritten_again = store.migrate()

    assert rewritten_again == 0
    store.close()


def test_migrated_records_keep_their_names(store_factory: StoreFactory) -> None:
    """Given quartz, azurite and opal migrated to the shape with mohs
    When the minerals are queried
    Then their names are azurite, opal and quartz
    """
    _stored_v1_names(store_factory, "quartz", "azurite", "opal")
    v2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    store = store_factory()
    store.migrate()

    names = _names(store.query(v2))

    assert names == ["azurite", "opal", "quartz"]
    store.close()


def test_migrated_records_read_the_added_field_default(store_factory: StoreFactory) -> None:
    """Given quartz, azurite and opal migrated to the shape with mohs defaulting to None
    When the minerals are queried
    Then every mohs is None
    """
    _stored_v1_names(store_factory, "quartz", "azurite", "opal")
    v2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    store = store_factory()
    store.migrate()

    mohs_values = _mohs_values(store.query(v2))

    assert mohs_values == [None, None, None]
    store.close()


def test_migrate_materializes_glue_into_a_real_column(store_factory: StoreFactory) -> None:
    """Given a Tsumeb record stored with coords, and the class later derived lat via Glue
    When the store is migrated
    Then one record was rewritten
    And the queried lat is 48.1
    """
    _stored_tsumeb_with_coords(store_factory)
    v2 = _evolve(
        name=(str, REQUIRED),
        lat=(Annotated[float, dc.Glue(_latitude_of_coords)], 0.0),
    )
    store = store_factory()

    rewritten = store.migrate()

    assert rewritten == 1
    assert store.query(v2)[0].lat == 48.1
    store.close()


def test_a_later_class_without_the_glue_reads_the_migrated_value(
    store_factory: StoreFactory,
) -> None:
    """Given a Tsumeb record migrated while its lat carried a Glue, and the class later made
    lat a plain field defaulting to 0.0
    When the store is reopened and queried
    Then lat is 48.1, the migrated value, not the 0.0 default
    """
    _stored_tsumeb_with_coords(store_factory)
    _evolve(
        name=(str, REQUIRED),
        lat=(Annotated[float, dc.Glue(_latitude_of_coords)], 0.0),
    )
    migrated = store_factory()
    migrated.migrate()
    migrated.close()
    v3 = _evolve(name=(str, REQUIRED), lat=(float, 0.0))
    reopened = store_factory()

    lat = reopened.query(v3)[0].lat

    assert lat == 48.1
    reopened.close()


# -- verify --------------------------------------------------------------------


def test_verify_of_a_clean_store_returns_no_failures(store_factory: StoreFactory) -> None:
    """Given a quartz stored under the shape the class still has
    When the store is verified
    Then no failures are returned
    """
    v1 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    store = store_factory()
    store.store(v1(name="quartz", mohs=7.0))
    store.commit()
    store.close()
    verified = store_factory()

    failures = verified.verify()

    assert failures == []
    verified.close()


def test_verify_names_every_unreadable_record(store_factory: StoreFactory) -> None:
    """Given two records stored before the class gained a required crystal_system without default
    When the store is verified
    Then two failures are named, instead of a crash
    """
    _stored_v1_names(store_factory, "quartz", "azurite")
    _evolve(name=(str, REQUIRED), crystal_system=(str, REQUIRED))
    verified = store_factory()

    failures = verified.verify()

    assert len(failures) == 2
    verified.close()


def test_verify_names_the_evolving_type_for_each_failure(store_factory: StoreFactory) -> None:
    """Given two records stored before the class gained a required crystal_system without default
    When the store is verified
    Then every failure names the Evolving type
    """
    _stored_v1_names(store_factory, "quartz", "azurite")
    _evolve(name=(str, REQUIRED), crystal_system=(str, REQUIRED))
    verified = store_factory()

    failures = verified.verify()

    assert {typename for typename, _ in failures} == {f"{__name__}:Evolving"}
    verified.close()
