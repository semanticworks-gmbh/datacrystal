"""Index cache serialization (ADR-005 / #12).

A cabinet reopened on its index cache answers queries as the session that built the indexes did,
keeps its incremental memory so further commits still un-index, and refuses a cache
whose index markers no longer match the live class (never authoritative).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import pytest

import datacrystal as dc
import datacrystal._indexes as _idx
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


def _pebble(color: object) -> type:
    """(Re)define the 'Pebble' entity with a unique name and a color of the given annotation."""
    namespace: dict = {
        "__module__": __name__,
        "__qualname__": "Pebble",
        "__annotations__": {"name": Annotated[str, dc.Unique], "color": color},
        "color": None,
    }
    return dc.entity(type("Pebble", (), namespace))


def _counting_build_class_indexes(calls: list[int]) -> Callable[..., object]:
    real: Callable[..., object] = _idx.build_class_indexes

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)

    return counting


@pytest.fixture
def cabinet_of_six_rocks(tmp_path: Path) -> Path:
    """Return a closed on-disk cabinet of rocks r0..r5 (odd ones red, the rest blue), uncached."""
    path = tmp_path / "cabinet"
    s = dc.Store.open(path, cache_index=True)
    for i in range(6):
        color = "red" if i % 2 else "blue"
        s.store(Rock(name=f"r{i}", hardness=float(i), color=color, tags=["igneous", f"g{i % 3}"]))
    s.commit()
    s.close()
    return path


@pytest.fixture
def cached_cabinet_of_six_rocks(cabinet_of_six_rocks: Path) -> Path:
    """Return the six-rock cabinet after a session that built and cached every index."""
    s = dc.Store.open(cabinet_of_six_rocks, cache_index=True)
    s.query(F.color == "red")
    s.query(F.hardness >= 0.0)
    s.query(F.name == "r0")
    s.query(F.tags.contains("igneous"))
    s.close()
    return cabinet_of_six_rocks


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
    oid_a = s.store(a)
    s.store(Rock(name="b"))
    s.commit()
    s.count(Rock)
    s.close()
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


def test_reload_keeps_the_extent(cached_cabinet_of_six_rocks: Path) -> None:
    """Given six committed rocks whose indexes were cached on close
    When the cabinet is reopened and the rocks are counted
    Then the count is six
    """
    reopened = dc.Store.open(cached_cabinet_of_six_rocks, cache_index=True)

    counted = reopened.count(Rock)

    assert counted == 6
    reopened.close()


@pytest.mark.parametrize(
    ("field_name", "method", "argument", "expected"),
    [
        ("color", "__eq__", "red", ["r1", "r3", "r5"]),
        ("hardness", "__ge__", 3.0, ["r3", "r4", "r5"]),
        ("hardness", "__lt__", 2.0, ["r0", "r1"]),
        ("name", "__eq__", "r4", ["r4"]),
        ("tags", "contains", "igneous", ["r0", "r1", "r2", "r3", "r4", "r5"]),
        ("tags", "contains", "g1", ["r1", "r4"]),
    ],
    ids=["color-eq", "hardness-ge", "hardness-lt", "name-eq", "tags-igneous", "tags-g1"],
)
def test_reload_answers_each_query_like_the_original(
    cabinet_of_six_rocks: Path, field_name: str, method: str, argument: object, expected: list[str]
) -> None:
    """Given six committed rocks, the first session's indexes built from scratch and cached on close
    When the cabinet is reopened and the same condition is queried
    Then the reloaded indexes answer with the same rocks as the first session
    And those are the expected rocks
    """
    condition = getattr(getattr(F, field_name), method)(argument)
    first = dc.Store.open(cabinet_of_six_rocks, cache_index=True)
    built_answer = _names(first.query(condition))
    first.close()
    reopened = dc.Store.open(cabinet_of_six_rocks, cache_index=True)

    reloaded_answer = _names(reopened.query(condition))

    assert reloaded_answer == built_answer
    assert reloaded_answer == expected
    reopened.close()


def _reopened_with_r1(path: Path) -> tuple[dc.Store, Rock]:
    """Reopen the cabinet on its cache and return the store with rock r1."""
    reopened = dc.Store.open(path, cache_index=True)
    r1 = reopened.get(Rock, name="r1")
    assert r1 is not None
    return reopened, r1


def test_updating_a_loaded_index_un_indexes_the_old_color(
    cached_cabinet_of_six_rocks: Path,
) -> None:
    """Given a reloaded index where rock r1 is red
    When r1 is re-indexed as green
    Then r1 is no longer among the red rocks
    """
    reopened, r1 = _reopened_with_r1(cached_cabinet_of_six_rocks)
    r1.color = "green"
    reopened.commit()

    red = reopened.query(F.color == "red")

    assert _names(red) == ["r3", "r5"]
    reopened.close()


def test_updating_a_loaded_index_indexes_the_new_color(
    cached_cabinet_of_six_rocks: Path,
) -> None:
    """Given a reloaded index where rock r1 is red
    When r1 is re-indexed as green
    Then r1 is among the green rocks
    """
    reopened, r1 = _reopened_with_r1(cached_cabinet_of_six_rocks)
    r1.color = "green"
    reopened.commit()

    green = reopened.query(F.color == "green")

    assert _names(green) == ["r1"]
    reopened.close()


def test_updating_a_loaded_index_updates_the_sorted_run(
    cached_cabinet_of_six_rocks: Path,
) -> None:
    """Given a reloaded index where rock r1 has hardness 1.0
    When r1 is re-indexed with hardness 99.0
    Then r1 is among the rocks of hardness at least 99.0
    """
    reopened, r1 = _reopened_with_r1(cached_cabinet_of_six_rocks)
    r1.hardness = 99.0
    reopened.commit()

    hardest = reopened.query(F.hardness >= 99.0)

    assert _names(hardest) == ["r1"]
    reopened.close()


def test_updating_a_loaded_index_re_indexes_the_tags(
    cached_cabinet_of_six_rocks: Path,
) -> None:
    """Given a reloaded index where rock r1 is tagged igneous and g1
    When r1 is re-indexed with the tags ["meta"]
    Then r1 is among the rocks tagged meta
    """
    reopened, r1 = _reopened_with_r1(cached_cabinet_of_six_rocks)
    r1.tags = ["meta"]
    reopened.commit()

    meta = reopened.query(F.tags.contains("meta"))

    assert _names(meta) == ["r1"]
    reopened.close()


def test_cache_is_refused_when_the_index_markers_changed(tmp_path: Path) -> None:
    """Given a cache written while a pebble's color was indexed
    When the cabinet is reopened under a same-named class whose color is no longer indexed
    Then the plan for color == "red" is not indexed, the cache refused and the index rebuilt
    """
    indexed = _pebble(Annotated[str | None, dc.Index])
    first = dc.Store.open(tmp_path / "cabinet", cache_index=True)
    first.store(indexed(name="p1", color="red"))
    first.commit()
    first.query(dc.fields(indexed).color == "red")
    first.close()
    plain = _pebble(str | None)
    reopened = dc.Store.open(tmp_path / "cabinet", cache_index=True)

    query_plan = reopened.explain(dc.fields(plain).color == "red")

    assert not query_plan.indexed
    reopened.close()


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
