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

from typing import Annotated

import msgspec.msgpack

import datacrystal as dc
from datacrystal._entity import type_info
from datacrystal._indexes import ClassIndexes, plan


@dc.entity
class Specimen:
    catalog: Annotated[str, dc.Unique]                # pure Unique → NO eq postings
    sku: Annotated[str, dc.Unique, dc.Index]          # Unique + Index → keeps eq
    locality: Annotated[str | None, dc.Index] = None
    grade: Annotated[float, dc.SortedIndex] = 0.0


F = dc.fields(Specimen)


def _built(store) -> ClassIndexes:
    for i in range(6):
        store.store(Specimen(catalog=f"C{i}", sku=f"S{i}",
                             locality="Tsumeb" if i % 2 else "Broken Hill",
                             grade=float(i)))
    store.commit()
    return store._index.ensure(type_info(Specimen))  # pyright: ignore[reportPrivateUsage]


def _oids(cond, ci: ClassIndexes) -> set[int]:
    bm, resid = plan(cond, ci)
    assert bm is not None and resid is None, cond   # index-answered, no residual scan
    return set(bm)


def test_pure_unique_field_has_no_eq_postings(store):
    ci = _built(store)
    assert "catalog" not in ci.eq          # the win: no single-element bitmaps
    assert "catalog" in ci.unique          # answered from the flat value→oid map
    assert "sku" in ci.eq and "sku" in ci.unique   # Unique+Index keeps eq


def test_unique_equality_answered_from_index(store):
    ci = _built(store)
    assert len(_oids(F.catalog == "C3", ci)) == 1
    assert len(_oids(F.catalog == "absent", ci)) == 0
    assert len(_oids(F.catalog.in_(["C1", "C4", "nope"]), ci)) == 2
    assert len(_oids(F.catalog.startswith("C"), ci)) == 6
    assert len(_oids(F.catalog.contains("3"), ci)) == 1


def test_query_get_and_explain_agree_for_unique(store):
    _built(store)
    assert store.get(Specimen, catalog="C2").sku == "S2"
    got = store.query(F.catalog == "C2")
    assert len(got) == 1 and got[0].sku == "S2"
    assert store.explain(F.catalog == "C2").indexed is True   # NOT a full-extent scan
    assert store.count(F.catalog == "C2") == 1


def test_unique_composes_in_and_or(store):
    ci = _built(store)
    both = _oids((F.catalog == "C1") & (F.locality == "Tsumeb"), ci)
    assert len(both) == 1                   # C1 (i=1, odd) has locality Tsumeb
    miss = _oids((F.catalog == "C0") & (F.locality == "Tsumeb"), ci)
    assert len(miss) == 0                   # C0 (i=0, even) is Broken Hill
    either = _oids((F.catalog == "C0") | (F.catalog == "C2"), ci)
    assert len(either) == 2


def _reload(ci: ClassIndexes) -> ClassIndexes | None:
    blob = msgspec.msgpack.decode(msgspec.msgpack.encode(ci.dump()))
    return ClassIndexes.load(blob, type_info(Specimen))


def test_cache_omits_unique_eq_and_preserves_queries(store):
    ci = _built(store)
    dumped = ci.dump()
    eq_fields = [f for f, _ in dumped["eq"]]
    assert "catalog" not in eq_fields       # the cache carries NO bitmaps for it
    assert "sku" in eq_fields
    loaded = _reload(ci)
    assert loaded is not None
    assert set(loaded.extent) == set(ci.extent)
    for cond in [F.catalog == "C3", F.catalog.in_(["C0", "C5"]),
                 F.catalog.startswith("C"), F.catalog.contains("4"),
                 F.sku == "S2", F.grade >= 3.0, F.locality == "Tsumeb"]:
        assert _oids(cond, ci) == _oids(cond, loaded), cond


def test_loaded_index_un_indexes_unique_on_update(store):
    ci = _built(store)
    loaded = _reload(ci)
    assert loaded is not None
    oid = next(iter(_oids(F.catalog == "C2", loaded)))
    # update the unique key — the reconstructed _last_values must drop the OLD key
    loaded.insert(oid, {"catalog": "C99", "sku": "S2",
                        "locality": "Tsumeb", "grade": 2.0})
    assert _oids(F.catalog == "C2", loaded) == set()        # old key un-indexed
    assert oid in _oids(F.catalog == "C99", loaded)         # new key indexed
    assert loaded.unique["catalog"].get("C2") is None       # unique map cleaned
    assert loaded.unique["catalog"].get("C99") == oid


def test_last_values_reconstruction_deferred_to_first_write(store):
    ci = _built(store)
    loaded = _reload(ci)
    assert loaded is not None
    # a read-only reopen does NOT pay the O(corpus) _last_values walk...
    assert loaded._needs_lv_rebuild is True       # pyright: ignore[reportPrivateUsage]
    assert loaded._last_values == {}              # pyright: ignore[reportPrivateUsage]
    _ = _oids(F.catalog == "C1", loaded)          # ...and a read doesn't trigger it
    assert loaded._needs_lv_rebuild is True       # pyright: ignore[reportPrivateUsage]
    # the first write rebuilds it, then un-indexes the old key correctly
    oid = next(iter(_oids(F.catalog == "C3", loaded)))
    loaded.insert(oid, {"catalog": "C3b", "sku": "S3", "locality": "Tsumeb", "grade": 3.0})
    assert loaded._needs_lv_rebuild is False      # pyright: ignore[reportPrivateUsage]
    assert _oids(F.catalog == "C3", loaded) == set()
    assert oid in _oids(F.catalog == "C3b", loaded)


def test_loaded_index_un_indexes_unique_on_delete(store):
    ci = _built(store)
    loaded = _reload(ci)
    assert loaded is not None
    oid = next(iter(_oids(F.catalog == "C4", loaded)))
    loaded.remove(oid)
    assert _oids(F.catalog == "C4", loaded) == set()        # gone from the map
    assert loaded.unique["catalog"].get("C4") is None
    assert oid not in loaded.extent
