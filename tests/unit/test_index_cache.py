"""Index cache serialization (ADR-005 / #12): a built ClassIndexes round-trips
through dump()/msgpack/load() with identical query answers, reconstructs its
incremental memory so further commits still un-index, and refuses a cache whose
index markers no longer match the live class (never authoritative)."""

from __future__ import annotations

from typing import Annotated

import msgspec.msgpack

import datacrystal as dc
from datacrystal._entity import type_info
from datacrystal._indexes import ClassIndexes, plan


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
    s = dc.Store.open(path)
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
    s2 = dc.Store.open(path)
    assert s2.count(F.hardness >= 2.0) == 3   # same answer
    assert sorted(r.name for r in s2.query(F.color == "red")) == ["r1", "r3"]
    assert built == []                         # served from the cache — NO O(corpus) rebuild
    s2.close()
