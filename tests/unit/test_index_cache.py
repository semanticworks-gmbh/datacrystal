"""Index cache serialization (ADR-005 / #12): a built ClassIndexes round-trips
through dump()/msgpack/load() with identical query answers, reconstructs its
incremental memory so further commits still un-index, and refuses a cache whose
index markers no longer match the live class (never authoritative)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

import msgspec.msgpack
import pytest

import datacrystal as dc
from datacrystal._entity import oid_of, type_info
from datacrystal._indexes import ClassIndexes, plan
from datacrystal._records import decode_scalar_tree, encode_scalar_tree


@dc.entity
class Rock:
    name: Annotated[str, dc.Unique]
    hardness: Annotated[float, dc.SortedIndex] = 0.0
    color: Annotated[str | None, dc.Index] = None
    tags: Annotated[list[str], dc.Index] = ()  # type: ignore[assignment]


F = dc.fields(Rock)


def _built_index(store) -> ClassIndexes:
    rocks = [
        Rock(name=f"r{i}", hardness=float(i),
             color="red" if i % 2 else "blue", tags=["igneous", f"g{i % 3}"])
        for i in range(6)
    ]
    for r in rocks:
        store.store(r)
    store.commit()
    return store._index.ensure(type_info(Rock))  # pyright: ignore[reportPrivateUsage]


def _oids(cond, ci: ClassIndexes) -> set[int]:
    bm, _ = plan(cond, ci)
    assert bm is not None
    return set(bm)


def _reload(ci: ClassIndexes) -> ClassIndexes | None:
    # exactly what the cache does: dump → msgpack bytes → decode → load
    blob = msgspec.msgpack.decode(msgspec.msgpack.encode(ci.dump()))
    return ClassIndexes.load(blob, type_info(Rock))


def test_roundtrip_preserves_every_query(store):
    ci = _built_index(store)
    loaded = _reload(ci)
    assert loaded is not None
    assert set(loaded.extent) == set(ci.extent)
    for cond in [F.color == "red", F.hardness >= 3.0, F.hardness < 2.0,
                 F.name == "r4", F.tags.contains("igneous"), F.tags.contains("g1")]:
        orig, _ = plan(cond, ci)
        back, _ = plan(cond, loaded)
        assert orig is not None and back is not None
        assert set(orig) == set(back), cond


def test_loaded_index_still_un_indexes_on_update(store):
    ci = _built_index(store)
    loaded = _reload(ci)
    assert loaded is not None
    oid = next(iter(_oids(F.color == "red", loaded)))  # an oid with color="red"
    # re-index it (an update) — the reconstructed _last_values must drop the old key
    loaded.insert(oid, {"name": "rX", "hardness": 99.0, "color": "green", "tags": ["meta"]})
    assert oid not in _oids(F.color == "red", loaded)     # old key un-indexed
    assert oid in _oids(F.color == "green", loaded)       # new key indexed
    assert oid in _oids(F.hardness >= 99.0, loaded)       # sorted run updated
    assert oid in _oids(F.tags.contains("meta"), loaded)  # list re-indexed


def test_cache_refused_when_markers_changed(store):
    ci = _built_index(store)
    blob = msgspec.msgpack.decode(msgspec.msgpack.encode(ci.dump()))

    # a same-named class whose index surface differs (color no longer indexed)
    @dc.entity
    class Rock2:
        name: Annotated[str, dc.Unique]
        hardness: Annotated[float, dc.SortedIndex] = 0.0
        color: str | None = None  # plain — no dc.Index
        tags: Annotated[list[str], dc.Index] = ()  # type: ignore[assignment]

    assert ClassIndexes.load(blob, type_info(Rock2)) is None  # config mismatch → rebuild


def test_reopen_loads_from_cache_without_rebuilding(tmp_path, monkeypatch):
    import datacrystal._indexes as _idx

    path = tmp_path / "cabinet"
    s = dc.Store.open(path, cache_index=True)
    for i in range(5):
        s.store(Rock(name=f"r{i}", hardness=float(i), color="red" if i % 2 else "blue"))
    s.commit()
    assert s.count(F.hardness >= 2.0) == 3   # builds the index this session
    s.close()
    assert (path / "index.cache").exists()   # written on close, stamped at the watermark

    built: list[int] = []
    real = _idx.build_class_indexes
    monkeypatch.setattr(_idx, "build_class_indexes",
                        lambda *a, **k: built.append(1) or real(*a, **k))
    s2 = dc.Store.open(path, cache_index=True)
    assert s2.count(F.hardness >= 2.0) == 3   # same answer
    assert sorted(r.name for r in s2.query(F.color == "red")) == ["r1", "r3"]
    assert built == []                         # served from the cache — NO O(corpus) rebuild
    s2.close()


def test_commit_before_build_invalidates_stale_cache_blob(tmp_path):
    """#71: a delete (or insert) committed BEFORE a class's index is built in a
    session must invalidate the boot-loaded cache blob, so ensure() rebuilds from
    the now-current records instead of loading a pre-commit blob — which would
    otherwise resurrect a deleted OID (and raise DanglingRefError on hydration)."""
    path = tmp_path / "cabinet"
    s = dc.Store.open(path, cache_index=True)
    a, b = Rock(name="a"), Rock(name="b")
    s.store(a)
    s.store(b)
    s.commit()
    oid_a = oid_of(a)
    assert s.count(Rock) == 2     # builds the index this session
    s.close()                     # writes index.cache (has a, b) at this watermark

    # reopen: the cache blob is loaded at boot; delete `a` by raw OID WITHOUT a
    # query, so the index is never built and apply_deletes can't fold the delete.
    s = dc.Store.open(path, cache_index=True)
    a2 = s.get_many([oid_a])[0]   # hydrate by OID — does not build the secondary index
    s.delete(a2)
    s.commit()
    assert s.count(Rock) == 1                                  # NOT served the stale blob
    assert sorted(r.name for r in s.query(Rock)) == ["b"]      # 'a' gone, no DanglingRefError
    s.close()

    s3 = dc.Store.open(path, cache_index=True)                 # next session also correct
    assert s3.count(Rock) == 1
    assert sorted(r.name for r in s3.query(Rock)) == ["b"]
    s3.close()


# --- datetime SortedIndex keys survive the cache codec (#106) -----------------


@dc.entity
class Dated:
    seq: Annotated[int, dc.Unique]
    at: Annotated[datetime | None, dc.SortedIndex] = None


DF = dc.fields(Dated)


@pytest.mark.parametrize("tz", [None, timezone.utc], ids=["naive", "aware"])
def test_datetime_keys_survive_cache_codec_roundtrip(tz):
    # The cache codec must route datetime keys through the record ext codes
    # (encode_scalar_tree/decode_scalar_tree, #106): msgspec's DEFAULT msgpack
    # encoding silently turns a *naive* datetime into a bare ISO str, which would
    # corrupt the sorted run (a later range bisect of a datetime vs str crashes).
    base = datetime(2021, 1, 1, 9, 0, tzinfo=tz)
    blob = {"classes": {"x": [base + timedelta(days=i) for i in range(3)]}}
    back = decode_scalar_tree(encode_scalar_tree(blob))
    keys = back["classes"]["x"]
    assert keys == [base + timedelta(days=i) for i in range(3)]
    assert all(isinstance(k, datetime) for k in keys)  # NOT decoded back as str


@pytest.mark.parametrize("tz", [None, timezone.utc], ids=["naive", "aware"])
def test_datetime_sorted_index_range_survives_reopen_via_cache(tmp_path, monkeypatch, tz):
    import datacrystal._indexes as _idx

    path = tmp_path / "cabinet"
    base = datetime(2021, 1, 1, 9, 0, tzinfo=tz)
    s = dc.Store.open(path, cache_index=True)
    for i in range(5):
        s.store(Dated(seq=i, at=base + timedelta(days=i)))
    s.store(Dated(seq=99, at=None))
    s.commit()
    assert s.count(DF.at >= base + timedelta(days=2)) == 3  # builds + caches the index
    s.close()

    built: list[int] = []
    real = _idx.build_class_indexes
    monkeypatch.setattr(_idx, "build_class_indexes",
                        lambda *a, **k: built.append(1) or real(*a, **k))
    s2 = dc.Store.open(path, cache_index=True)
    cut = base + timedelta(days=2)
    assert sorted(e.seq for e in s2.query(DF.at >= cut)) == [2, 3, 4]  # range intact
    assert [e.seq for e in s2.query(Dated, order_by=(DF.at, "desc"))] == \
        [4, 3, 2, 1, 0, 99]                                            # order intact
    assert built == []  # served from the cache — the datetime run reconstructed, not rebuilt
    s2.close()
