"""Additive schema evolution: type lineage, decode-by-name, defaults.

Each changed field shape of a class gets a fresh cid (a new lineage row in
the type dictionary); old records stay decodable through their own shape
forever. On load, fields added to the class are filled from their dataclass
defaults, fields removed from the class are ignored, and a new field WITHOUT
a default refuses loudly. A rename is remove+add: the old values are dropped.

The tests simulate "the code changed between runs" by re-creating an entity
class with the same module/qualname (→ same typename) but different fields.
Dynamic class fabrication is untypeable; the per-file pyright relaxations
below exist only for that.
"""
# pyright: reportCallIssue=false, reportArgumentType=false, reportAttributeAccessIssue=false
# pyright: reportFunctionMemberAccess=false

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import type_info

REQUIRED = object()


def _evolve(**fields):
    """(Re)define the canonical 'Evolving' entity class with these fields.

    Values are (annotation, default); default REQUIRED means no default.
    """
    annotations = {}
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


def test_add_field_with_default(store_factory):
    V1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    store.root = [V1(name="quartz")]
    store.commit()
    store.close()

    V2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    reopened = store_factory()
    quartz = reopened.root[0]
    assert isinstance(quartz, V2)
    assert quartz.name == "quartz"
    assert quartz.mohs is None
    quartz.mohs = 7.0
    reopened.commit()
    reopened.close()

    third = store_factory()
    assert third.root[0].mohs == 7.0
    third.close()


def test_add_field_with_default_factory(store_factory):
    V1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    store.root = [V1(name="quartz")]
    store.commit()
    store.close()

    _evolve(name=(str, REQUIRED),
            tags=(list, dataclasses.field(default_factory=list)))
    reopened = store_factory()
    quartz = reopened.root[0]
    assert quartz.tags == []
    assert isinstance(quartz.tags, dc.PersistentList)
    quartz.tags.append("fresh")  # the filled default is tracked like any list
    reopened.commit()
    reopened.close()

    third = store_factory()
    assert third.root[0].tags == ["fresh"]
    third.close()


def test_add_field_without_default_refuses(store_factory):
    V1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    store.root = [V1(name="quartz")]
    store.commit()
    store.close()

    _evolve(name=(str, REQUIRED), serial=(int, REQUIRED))
    reopened = store_factory()
    with pytest.raises(dc.SchemaMismatchError, match="serial.*no default"):
        _ = reopened.root[0]
    reopened.close()


def test_removed_field_is_ignored(store_factory):
    V2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    store = store_factory()
    store.root = [V2(name="quartz", mohs=7.0)]
    store.commit()
    store.close()

    _evolve(name=(str, REQUIRED))
    reopened = store_factory()
    quartz = reopened.root[0]
    assert quartz.name == "quartz"
    assert not hasattr(quartz, "mohs")
    reopened.close()


def test_reordered_fields_map_by_name(store_factory):
    _ = _evolve(a=(str, REQUIRED), b=(int, REQUIRED))
    store = store_factory()
    store.root = _evolve(a=(str, REQUIRED), b=(int, REQUIRED))(a="alpha", b=2)
    store.commit()
    store.close()

    _evolve(b=(int, REQUIRED), a=(str, REQUIRED))
    reopened = store_factory()
    assert reopened.root.a == "alpha"
    assert reopened.root.b == 2
    reopened.close()


def test_rename_is_remove_plus_add(store_factory):
    """Documented behavior: a rename drops the old values and fills the new
    name from its default. Explicit migrations are post-v0.1."""
    V1 = _evolve(label=(str, REQUIRED))
    store = store_factory()
    store.root = [V1(label="quartz")]
    store.commit()
    store.close()

    _evolve(title=(str | None, None))
    reopened = store_factory()
    assert reopened.root[0].title is None  # old 'label' value is gone
    reopened.close()


def test_added_index_field_is_queryable_across_lineage(store_factory):
    V1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    store.root = [V1(name="quartz"), V1(name="azurite")]
    store.commit()
    store.close()

    V2 = _evolve(name=(str, REQUIRED),
                 crystal_system=(Annotated[str | None, dc.Index], None))
    reopened = store_factory()
    assert len(reopened.query(V2.crystal_system == None)) == 2  # noqa: E711
    quartz = next(m for m in reopened.root if m.name == "quartz")
    quartz.crystal_system = "trigonal"
    reopened.commit()
    reopened.close()

    third = store_factory()
    hits = third.query(V2.crystal_system == "trigonal")
    assert [m.name for m in hits] == ["quartz"]
    assert len(third.query(V2.crystal_system == None)) == 1  # noqa: E711
    third.close()


def test_added_unique_field_defaulting_none(store_factory):
    V1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    store.root = [V1(name="quartz")]
    store.commit()
    store.close()

    V2 = _evolve(name=(str, REQUIRED),
                 qid=(Annotated[str | None, dc.Unique], None))
    reopened = store_factory()
    assert reopened.get(V2, qid="Q43010") is None
    reopened.root[0].qid = "Q43010"
    reopened.commit()
    reopened.close()

    third = store_factory()
    assert third.get(V2, qid="Q43010").name == "quartz"
    third.close()


def test_added_unique_field_with_non_none_default_refuses(store_factory):
    V1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    store.root = [V1(name="quartz")]
    store.commit()
    store.close()

    V2 = _evolve(name=(str, REQUIRED),
                 code=(Annotated[str | None, dc.Unique], "X"))
    reopened = store_factory()
    with pytest.raises(dc.SchemaMismatchError, match="must default to None"):
        reopened.get(V2, code="X")
    reopened.close()


def test_legacy_unique_types_table_is_migrated(tmp_path):
    """Stores created before schema evolution carry UNIQUE(types.name);
    boot() must rebuild the table so lineage rows can share a name."""
    import sqlite3

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

    store = dc.Store.open(directory)
    sql = store._backend._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='types'"
    ).fetchone()[0]
    assert "UNIQUE" not in sql
    store.close()


def test_each_shape_gets_its_own_lineage_row(store_factory):
    V1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    store.root = [V1(name="quartz")]
    store.commit()
    store.close()

    V2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    reopened = store_factory()
    typename = f"{__name__}:Evolving"
    assert len(reopened._cids_by_typename[typename]) == 1
    reopened.root.append(V2(name="azurite", mohs=3.5))
    reopened.commit()
    assert len(reopened._cids_by_typename[typename]) == 2
    # the old record still decodes through its own row
    names = sorted(m.name for m in reopened.root)
    assert names == ["azurite", "quartz"]
    reopened.close()


def test_renamed_from_binds_old_column(store_factory):
    # V1 persists `hardness`; V2 renames it to `mohs` via RenamedFrom. The old
    # record decodes with the old value under the new name — additive, no
    # rewrite (#26 (a)). A rename was "remove+add" (lost data) until this marker.
    V1 = _evolve(name=(str, REQUIRED), hardness=(float | None, None))
    store = store_factory()
    store.root = [V1(name="quartz", hardness=7.0)]
    store.commit()
    store.close()

    V2 = _evolve(
        name=(str, REQUIRED),
        mohs=(Annotated[float | None, dc.RenamedFrom("hardness")], None),
    )
    reopened = store_factory()
    quartz = reopened.root[0]
    assert isinstance(quartz, V2)
    assert quartz.mohs == 7.0  # the old `hardness` value followed the rename
    reopened.close()


def test_renamed_from_prefers_new_name_when_present(store_factory):
    # Once data is written under the new name, RenamedFrom is a no-op: the new
    # column wins over the old (correct precedence for an already-migrated store).
    V2 = _evolve(
        name=(str, REQUIRED),
        mohs=(Annotated[float | None, dc.RenamedFrom("hardness")], None),
    )
    store = store_factory()
    store.root = [V2(name="opal", mohs=5.5)]
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.root[0].mohs == 5.5
    reopened.close()


def test_glue_splits_old_field_into_two(store_factory):
    # #26 (b): V1 persists `coords="lat,lon"`; V2 splits it into lat/lon via Glue,
    # which derives each absent field from the old record (additive, no rewrite).
    V1 = _evolve(name=(str, REQUIRED), coords=(str, REQUIRED))
    store = store_factory()
    store.root = [V1(name="Tsumeb", coords="48.1,11.5")]
    store.commit()
    store.close()

    V2 = _evolve(
        name=(str, REQUIRED),
        lat=(Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[0]))], 0.0),
        lon=(Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[1]))], 0.0),
    )
    reopened = store_factory()
    loc = reopened.root[0]
    assert isinstance(loc, V2)
    assert (loc.lat, loc.lon) == (48.1, 11.5)  # both derived from old `coords`
    reopened.close()


def test_glue_no_op_when_field_already_present(store_factory):
    # Once data is written in the new shape, glue does NOT fire — the persisted
    # value wins (correct for an already-migrated record).
    V2 = _evolve(
        name=(str, REQUIRED),
        lat=(Annotated[float, dc.Glue(lambda old: 999.0)], 0.0),
    )
    store = store_factory()
    store.root = [V2(name="opal", lat=12.5)]
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.root[0].lat == 12.5  # not 999.0 — glue never fired
    reopened.close()


def test_glue_merges_multiple_old_fields(store_factory):
    # Glue sees the WHOLE old record, so it merges as well as splits.
    V1 = _evolve(first=(str, REQUIRED), last=(str, REQUIRED))
    store = store_factory()
    store.root = [V1(first="Marie", last="Curie")]
    store.commit()
    store.close()

    V2 = _evolve(
        full_name=(Annotated[str, dc.Glue(lambda old: f"{old['first']} {old['last']}")], ""),
    )
    reopened = store_factory()
    merged = reopened.root[0]
    assert isinstance(merged, V2)
    assert merged.full_name == "Marie Curie"
    reopened.close()


def test_glue_applies_at_decode_level(store_factory):
    # Glue works through pluck() (decode-level), same scope as RenamedFrom.
    V1 = _evolve(name=(str, REQUIRED), coords=(str, REQUIRED))
    store = store_factory()
    store.store(V1(name="a", coords="1.0,2.0"))
    store.store(V1(name="b", coords="3.0,4.0"))
    store.commit()
    store.close()

    V2 = _evolve(
        name=(str, REQUIRED),
        lat=(Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[0]))], 0.0),
    )
    reopened = store_factory()
    assert sorted(reopened.pluck(V2, "lat")) == [1.0, 3.0]  # derived at decode level
    reopened.close()


def test_migrate_rewrites_old_records_to_newest_shape(store_factory):
    # #26 (c): migrate() re-encodes every stale-cid record under the current shape.
    V1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    for nm in ("quartz", "azurite", "opal"):
        store.store(V1(name=nm))
    store.commit()
    store.close()

    V2 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    s = store_factory()
    assert s.migrate() == 3          # all three old-shape records rewritten
    assert s.migrate() == 0          # idempotent — nothing stale on a second run
    assert sorted(m.name for m in s.query(V2)) == ["azurite", "opal", "quartz"]
    assert all(m.mohs is None for m in s.query(V2))  # the added field reads its default
    s.close()


def test_migrate_materializes_glue_into_a_real_column(store_factory):
    # migrate() turns a read-time Glue derivation into a persisted column — so a
    # LATER class without the glue still reads the value (not the bare default).
    V1 = _evolve(name=(str, REQUIRED), coords=(str, REQUIRED))
    store = store_factory()
    store.store(V1(name="Tsumeb", coords="48.1,11.5"))
    store.commit()
    store.close()

    V2 = _evolve(
        name=(str, REQUIRED),
        lat=(Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[0]))], 0.0),
    )
    s = store_factory()
    assert s.migrate() == 1  # the old `coords` record rewritten with lat materialized
    assert s.query(V2)[0].lat == 48.1
    s.close()

    V3 = _evolve(name=(str, REQUIRED), lat=(float, 0.0))  # NO glue — plain field
    reopened = store_factory()
    assert reopened.query(V3)[0].lat == 48.1  # the migrated value, not the 0.0 default
    reopened.close()


def test_verify_clean_store_returns_no_failures(store_factory):
    V1 = _evolve(name=(str, REQUIRED), mohs=(float | None, None))
    store = store_factory()
    store.store(V1(name="quartz", mohs=7.0))
    store.commit()
    store.close()

    s = store_factory()  # same shape → everything decodes cleanly
    assert s.verify() == []
    s.close()


def test_verify_names_unreadable_records(store_factory):
    # A field added WITHOUT a default (and no Glue) cannot decode old records —
    # verify() names exactly those (typename, oid) pairs instead of crashing.
    V1 = _evolve(name=(str, REQUIRED))
    store = store_factory()
    store.store(V1(name="quartz"))
    store.store(V1(name="azurite"))
    store.commit()
    store.close()

    V2 = _evolve(name=(str, REQUIRED), crystal_system=(str, REQUIRED))  # required, no default
    s = store_factory()
    failures = s.verify()
    assert len(failures) == 2                              # both old records named
    assert {tn for tn, _ in failures} == {type_info(V2).typename}  # all the Evolving type
    s.close()
