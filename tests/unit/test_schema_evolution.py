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
