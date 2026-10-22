"""Index cache serialization (ADR-005 / #12).

A built ClassIndexes round-trips through dump()/msgpack/load() with identical query answers,
reconstructs its incremental memory so further commits still un-index, and refuses a cache
whose index markers no longer match the live class (never authoritative).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import msgspec.msgpack
import pytest

import datacrystal as dc
import datacrystal._indexes as _idx
from datacrystal._entity import oid_of, type_info
from datacrystal._indexes import ClassIndexes, plan
from datacrystal._records import decode_scalar_tree, encode_scalar_tree


@dc.entity
class Rock:
    """A cabinet rock with a hardness, a colour and tags."""

    name: Annotated[str, dc.Unique]
    hardness: Annotated[float, dc.SortedIndex] = 0.0
    color: Annotated[str | None, dc.Index] = None
    tags: Annotated[list[str], dc.Index] = ()  # type: ignore[assignment]


@dc.entity
class Find:
    """A numbered field find with the moment it was made."""

    seq: Annotated[int, dc.Unique]
    at: Annotated[datetime | None, dc.SortedIndex] = None


F = dc.fields(Rock)
DF = dc.fields(Find)
TIMEZONES = {"naive": None, "aware": UTC}
KINDS = pytest.mark.parametrize("kind", ["naive", "aware"])


def _names(rocks: Iterable[Rock]) -> list[str]:
    return sorted(rock.name for rock in rocks)


def _seqs(finds: Iterable[Find]) -> list[int]:
    return [find.seq for find in finds]


def _reload(ci: ClassIndexes) -> ClassIndexes | None:
    blob = msgspec.msgpack.decode(msgspec.msgpack.encode(ci.dump()))
    return ClassIndexes.load(blob, type_info(Rock))


def _counting_build_class_indexes(calls: list[int]) -> Callable[..., object]:
    real: Callable[..., object] = _idx.build_class_indexes

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)

    return counting


@pytest.fixture
def built(store: dc.Store) -> ClassIndexes:
    """Return the class indexes of six committed rocks r0..r5 (odd ones red, the rest blue)."""
    for i in range(6):
        color = "red" if i % 2 else "blue"
        store.store(Rock(name=f"r{i}", hardness=float(i), color=color, tags=["igneous", f"g{i % 3}"]))
    store.commit()
    return store._index.ensure(type_info(Rock))


@pytest.fixture
def loaded(built: ClassIndexes) -> ClassIndexes:
    """Return the same indexes after a cache dump and reload."""
    reloaded = _reload(built)
    if reloaded is None:
        pytest.fail("the dumped cache did not load back")
    return reloaded


def _stock_five_rocks(store: dc.Store) -> None:
    for i in range(5):
        store.store(Rock(name=f"r{i}", hardness=float(i), color="red" if i % 2 else "blue"))


@pytest.fixture
def cabinet_of_five_rocks(tmp_path: Path) -> Path:
    """Return a closed on-disk cabinet of rocks r0..r4 whose hardness index was cached."""
    path = tmp_path / "cabinet"
    s = dc.Store.open(path, cache_index=True)
    _stock_five_rocks(s)
    s.commit()
    s.count(F.hardness >= 2.0)
    s.close()
    return path


@pytest.fixture
def cabinet_of_rocks_a_and_b(tmp_path: Path) -> tuple[Path, int]:
    """Return a closed on-disk cabinet of rocks a and b with its index cached, and a's oid."""
    path = tmp_path / "cabinet"
    s = dc.Store.open(path, cache_index=True)
    a = Rock(name="a")
    s.store(a)
    s.store(Rock(name="b"))
    s.commit()
    oid_a = oid_of(a)
    s.count(Rock)
    s.close()
    if oid_a is None:
        pytest.fail("the committed rock has no oid")
    return path, oid_a


@pytest.fixture
def cabinet_of_dated_finds(tmp_path: Path, kind: str) -> Path:
    """Return a closed on-disk cabinet of five dated finds and one undated, index cached."""
    path = tmp_path / "cabinet"
    base = datetime(2021, 1, 1, 9, 0, tzinfo=TIMEZONES[kind])
    s = dc.Store.open(path, cache_index=True)
    for i in range(5):
        s.store(Find(seq=i, at=base + timedelta(days=i)))
    s.store(Find(seq=99, at=None))
    s.commit()
    s.count(DF.at >= base + timedelta(days=2))
    s.close()
    return path


def test_reload_keeps_the_extent(built: ClassIndexes, loaded: ClassIndexes) -> None:
    """Given six committed rocks and their indexes reloaded from a dumped cache
    When the extents are compared
    Then the reloaded extent equals the original
    """
    assert set(loaded.extent) == set(built.extent)


@pytest.mark.parametrize(
    ("field_name", "method", "argument"),
    [
        ("color", "__eq__", "red"),
        ("hardness", "__ge__", 3.0),
        ("hardness", "__lt__", 2.0),
        ("name", "__eq__", "r4"),
        ("tags", "contains", "igneous"),
        ("tags", "contains", "g1"),
    ],
    ids=["color-eq", "hardness-ge", "hardness-lt", "name-eq", "tags-igneous", "tags-g1"],
)
def test_reload_answers_each_query_like_the_original(
    built: ClassIndexes, loaded: ClassIndexes, field_name: str, method: str, argument: object
) -> None:
    """Given six committed rocks and their indexes reloaded from a dumped cache
    When the same condition is planned on both
    Then the reloaded indexes match the same rocks as the original
    """
    condition = getattr(getattr(F, field_name), method)(argument)

    original, _ = plan(condition, built)
    reloaded, _ = plan(condition, loaded)

    assert original is not None
    assert reloaded is not None
    assert set(reloaded) == set(original)


def test_updating_a_loaded_index_un_indexes_the_old_color(loaded: ClassIndexes) -> None:
    """Given a reloaded index where rock r1 is red
    When r1 is re-indexed as green
    Then r1 is no longer among the red rocks
    """
    oid = loaded.unique["name"]["r1"]

    loaded.insert(oid, {"name": "rX", "hardness": 99.0, "color": "green", "tags": ["meta"]})

    red, _ = plan(F.color == "red", loaded)
    assert red is not None
    assert oid not in set(red)


def test_updating_a_loaded_index_indexes_the_new_color(loaded: ClassIndexes) -> None:
    """Given a reloaded index where rock r1 is red
    When r1 is re-indexed as green
    Then r1 is among the green rocks
    """
    oid = loaded.unique["name"]["r1"]

    loaded.insert(oid, {"name": "rX", "hardness": 99.0, "color": "green", "tags": ["meta"]})

    green, _ = plan(F.color == "green", loaded)
    assert green is not None
    assert oid in set(green)


def test_updating_a_loaded_index_updates_the_sorted_run(loaded: ClassIndexes) -> None:
    """Given a reloaded index where rock r1 has hardness 1.0
    When r1 is re-indexed with hardness 99.0
    Then r1 is among the rocks of hardness at least 99.0
    """
    oid = loaded.unique["name"]["r1"]

    loaded.insert(oid, {"name": "rX", "hardness": 99.0, "color": "green", "tags": ["meta"]})

    hardest, _ = plan(F.hardness >= 99.0, loaded)
    assert hardest is not None
    assert oid in set(hardest)


def test_updating_a_loaded_index_re_indexes_the_tags(loaded: ClassIndexes) -> None:
    """Given a reloaded index where rock r1 is tagged igneous and g1
    When r1 is re-indexed with the tags ["meta"]
    Then r1 is among the rocks tagged meta
    """
    oid = loaded.unique["name"]["r1"]

    loaded.insert(oid, {"name": "rX", "hardness": 99.0, "color": "green", "tags": ["meta"]})

    meta, _ = plan(F.tags.contains("meta"), loaded)
    assert meta is not None
    assert oid in set(meta)


def test_cache_is_refused_when_the_index_markers_changed(built: ClassIndexes) -> None:
    """Given a dumped cache of Rock, and a same-shaped class whose color is no longer indexed
    When the cache is loaded for that class
    Then the load is refused, so the index is rebuilt
    """
    blob = msgspec.msgpack.decode(msgspec.msgpack.encode(built.dump()))

    @dc.entity
    class Rock2:
        name: Annotated[str, dc.Unique]
        hardness: Annotated[float, dc.SortedIndex] = 0.0
        color: str | None = None  # plain, no dc.Index
        tags: Annotated[list[str], dc.Index] = ()  # type: ignore[assignment]

    refused = ClassIndexes.load(blob, type_info(Rock2))

    assert refused is None


def test_closing_a_store_writes_the_index_cache(tmp_path: Path) -> None:
    """Given a cabinet whose hardness index was built in a session
    When the store was closed
    Then an index.cache file is on disk
    """
    path = tmp_path / "cabinet"
    s = dc.Store.open(path, cache_index=True)
    _stock_five_rocks(s)
    s.commit()
    s.count(F.hardness >= 2.0)

    s.close()

    assert (path / "index.cache").exists()


def test_reopen_serves_a_range_count_from_the_cache(cabinet_of_five_rocks: Path) -> None:
    """Given a cabinet of rocks r0..r4 whose index was cached on close
    When it is reopened and rocks of hardness at least 2.0 are counted
    Then the count is 3
    """
    reopened = dc.Store.open(cabinet_of_five_rocks, cache_index=True)

    counted = reopened.count(F.hardness >= 2.0)

    assert counted == 3
    reopened.close()


def test_reopen_serves_a_color_query_from_the_cache(cabinet_of_five_rocks: Path) -> None:
    """Given a cabinet of rocks r0..r4 whose index was cached on close
    When it is reopened and the red rocks are queried
    Then they are r1 and r3
    """
    reopened = dc.Store.open(cabinet_of_five_rocks, cache_index=True)

    red = reopened.query(F.color == "red")

    assert _names(red) == ["r1", "r3"]
    reopened.close()


def test_reopen_does_not_rebuild_the_indexes(
    cabinet_of_five_rocks: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a cabinet of rocks r0..r4 whose index was cached on close
    When it is reopened and rocks of hardness at least 2.0 are counted
    Then no class index is built again
    """
    built: list[int] = []
    monkeypatch.setattr(_idx, "build_class_indexes", _counting_build_class_indexes(built))
    reopened = dc.Store.open(cabinet_of_five_rocks, cache_index=True)

    reopened.count(F.hardness >= 2.0)

    assert built == []
    reopened.close()


def test_a_delete_before_the_index_is_built_does_not_resurrect_the_rock_in_count(
    cabinet_of_rocks_a_and_b: tuple[Path, int],
) -> None:
    """Given a cached cabinet of rocks a and b, reopened, with a deleted by oid before any query
    When the rocks are counted
    Then the count is 1, not served from the stale cache blob (#71)
    """
    path, oid_a = cabinet_of_rocks_a_and_b
    s = dc.Store.open(path, cache_index=True)
    s.delete(s.get_many([oid_a])[0])
    s.commit()

    counted = s.count(Rock)

    assert counted == 1
    s.close()


def test_a_delete_before_the_index_is_built_does_not_resurrect_the_rock_in_a_query(
    cabinet_of_rocks_a_and_b: tuple[Path, int],
) -> None:
    """Given a cached cabinet of rocks a and b, reopened, with a deleted by oid before any query
    When the rocks are queried
    Then only b is returned, without a DanglingRefError (#71)
    """
    path, oid_a = cabinet_of_rocks_a_and_b
    s = dc.Store.open(path, cache_index=True)
    s.delete(s.get_many([oid_a])[0])
    s.commit()

    rocks = s.query(Rock)

    assert _names(rocks) == ["b"]
    s.close()


def test_a_delete_before_the_index_is_built_holds_in_the_next_session(
    cabinet_of_rocks_a_and_b: tuple[Path, int],
) -> None:
    """Given a cached cabinet of rocks a and b, a deleted by oid before any query, then closed
    When the store is reopened and the rocks are queried
    Then only b is returned, and the count is 1
    """
    path, oid_a = cabinet_of_rocks_a_and_b
    s = dc.Store.open(path, cache_index=True)
    s.delete(s.get_many([oid_a])[0])
    s.commit()
    s.close()
    next_session = dc.Store.open(path, cache_index=True)

    rocks = next_session.query(Rock)

    assert _names(rocks) == ["b"]
    assert next_session.count(Rock) == 1
    next_session.close()


# --- datetime SortedIndex keys survive the cache codec (#106) -----------------


@KINDS
def test_datetime_keys_survive_the_cache_codec_roundtrip(kind: str) -> None:
    """Given three consecutive datetimes, naive or aware, as sorted-run keys
    When they pass through the cache's scalar-tree codec
    Then the keys equal the originals
    And each key is a datetime, not a str (#106)
    """
    base = datetime(2021, 1, 1, 9, 0, tzinfo=TIMEZONES[kind])
    days = [base, base + timedelta(days=1), base + timedelta(days=2)]

    back = decode_scalar_tree(encode_scalar_tree({"classes": {"x": days}}))

    assert back["classes"]["x"] == days
    assert all(isinstance(k, datetime) for k in back["classes"]["x"])


@KINDS
def test_datetime_range_survives_reopen_via_cache(
    cabinet_of_dated_finds: Path, kind: str
) -> None:
    """Given a cabinet of five dated finds whose datetime index was cached on close
    When it is reopened and finds at or after day 2 are queried
    Then they are the finds 2, 3 and 4
    """
    reopened = dc.Store.open(cabinet_of_dated_finds, cache_index=True)
    cut = datetime(2021, 1, 3, 9, 0, tzinfo=TIMEZONES[kind])

    finds = reopened.query(DF.at >= cut)

    assert sorted(_seqs(finds)) == [2, 3, 4]
    reopened.close()


@KINDS
def test_datetime_order_survives_reopen_via_cache(
    cabinet_of_dated_finds: Path, kind: str
) -> None:
    """Given a cabinet of five dated finds and one undated, datetime index cached on close
    When it is reopened and the finds are queried ordered by time descending
    Then they come as 4, 3, 2, 1, 0 and the undated 99 last
    """
    reopened = dc.Store.open(cabinet_of_dated_finds, cache_index=True)

    finds = reopened.query(Find, order_by=(DF.at, "desc"))

    assert _seqs(finds) == [4, 3, 2, 1, 0, 99]
    reopened.close()


@KINDS
def test_datetime_index_is_reconstructed_from_the_cache_not_rebuilt(
    cabinet_of_dated_finds: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a cabinet of five dated finds whose datetime index was cached on close
    When it is reopened and finds at or after day 2 are queried
    Then no class index is built again
    """
    built: list[int] = []
    monkeypatch.setattr(_idx, "build_class_indexes", _counting_build_class_indexes(built))
    reopened = dc.Store.open(cabinet_of_dated_finds, cache_index=True)

    reopened.query(DF.at >= datetime(2021, 1, 3, 9, 0, tzinfo=TIMEZONES[kind]))

    assert built == []
    reopened.close()
