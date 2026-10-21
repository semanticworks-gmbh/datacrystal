"""Reverse-reference index caching (#63).

The global ``incoming()`` index is persisted to the same watermark-stamped sidecar as the
forward indexes, so a warm reopen serves backlinks without re-harvesting every field of
every record. Like the forward cache it is never authoritative: a commit landing before
the reverse index materializes invalidates the cached blob, and ``ensure_reverse()``
rebuilds from the current records (the #71 contract, applied to the reverse index).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import pytest

import datacrystal as dc
import datacrystal._indexes as _idx
from tests.conftest import Locality, Mineral


@dc.entity
class Tag:
    """A museum tag holding a direct (non-Lazy) reference to a mineral."""

    text: Annotated[str, dc.Unique]
    mineral: Mineral | None = None


def _texts(tags: list[Tag]) -> list[str]:
    return sorted(t.text for t in tags)


def _names(nodes: list[Mineral]) -> list[str]:
    return sorted(n.name for n in nodes)


def _seed(path: Path) -> None:
    """Commit locality root with minerals a and b typed there; cache the reverse index."""
    s = dc.Store.open(path, cache_index=True)
    root = Locality(qid="L-root", name="root")
    s.store(Mineral(qid="a", name="a", type_locality=dc.Lazy.of(root)))
    s.store(Mineral(qid="b", name="b", type_locality=dc.Lazy.of(root)))
    s.commit()
    s.incoming(root)
    s.close()


def _counting_harvest(calls: list[int]) -> Callable[[list], object]:
    real = _idx.harvest_ref_oids

    def counting(value: list) -> object:
        calls.append(1)
        return real(value)

    return counting


def test_warm_reopen_serves_incoming_from_cache(tmp_path: Path) -> None:
    """Given a store whose reverse index was cached on close
    When it is reopened and the backlinks of root are asked for
    Then they are a and b
    """
    _seed(tmp_path / "graph")
    s = dc.Store.open(tmp_path / "graph", cache_index=True)
    root = s.get(Locality, qid="L-root")
    assert root is not None

    names = _names(s.incoming(root))

    assert names == ["a", "b"]
    s.close()


def test_warm_reopen_does_not_reharvest_the_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a store whose reverse index was cached on close
    When it is reopened and the backlinks of root are asked for
    Then no record is harvested again
    """
    _seed(tmp_path / "graph")
    harvests: list[int] = []
    monkeypatch.setattr(_idx, "harvest_ref_oids", _counting_harvest(harvests))
    s = dc.Store.open(tmp_path / "graph", cache_index=True)
    root = s.get(Locality, qid="L-root")
    assert root is not None

    s.incoming(root)

    assert harvests == []
    s.close()


def test_insert_before_incoming_invalidates_reverse_cache(tmp_path: Path) -> None:
    """Given a cached reverse index and a new mineral c committed before incoming() is called
    When the backlinks of root are asked for
    Then they are a, b and c
    """
    _seed(tmp_path / "graph")
    s = dc.Store.open(tmp_path / "graph", cache_index=True)
    root = s.get(Locality, qid="L-root")
    assert root is not None
    s.store(Mineral(qid="c", name="c", type_locality=dc.Lazy.of(root)))
    s.commit()

    names = _names(s.incoming(root))

    assert names == ["a", "b", "c"]
    s.close()


def test_delete_before_incoming_invalidates_reverse_cache(tmp_path: Path) -> None:
    """Given a cached reverse index and mineral a deleted before incoming() is called
    When the backlinks of root are asked for
    Then only b is left
    """
    _seed(tmp_path / "graph")
    s = dc.Store.open(tmp_path / "graph", cache_index=True)
    a = s.get(Mineral, qid="a")
    s.delete(a)
    s.commit()
    root = s.get(Locality, qid="L-root")
    assert root is not None

    names = _names(s.incoming(root))

    assert names == ["b"]
    s.close()


def test_incremental_fold_after_cache_load_rebuilds_refs(tmp_path: Path) -> None:
    """Given a reverse index loaded from the cache, then mineral c added and mineral a deleted
    When the backlinks of root are asked for after the commit
    Then they are b and c
    """
    _seed(tmp_path / "graph")
    s = dc.Store.open(tmp_path / "graph", cache_index=True)
    root = s.get(Locality, qid="L-root")
    assert root is not None
    s.incoming(root)
    s.store(Mineral(qid="c", name="c", type_locality=dc.Lazy.of(root)))
    s.delete(s.get(Mineral, qid="a"))
    s.commit()

    names = _names(s.incoming(root))

    assert names == ["b", "c"]
    s.close()


def test_incremental_fold_persists_across_reopen(tmp_path: Path) -> None:
    """Given a reverse index loaded from the cache, then mineral c added, a deleted, closed
    When the store is reopened and the backlinks of root are asked for
    Then they are b and c
    """
    _seed(tmp_path / "graph")
    s = dc.Store.open(tmp_path / "graph", cache_index=True)
    root = s.get(Locality, qid="L-root")
    assert root is not None
    s.incoming(root)
    s.store(Mineral(qid="c", name="c", type_locality=dc.Lazy.of(root)))
    s.delete(s.get(Mineral, qid="a"))
    s.commit()
    s.close()
    reopened = dc.Store.open(tmp_path / "graph", cache_index=True)
    reopened_root = reopened.get(Locality, qid="L-root")
    assert reopened_root is not None

    names = _names(reopened.incoming(reopened_root))

    assert names == ["b", "c"]
    reopened.close()


def test_reverse_cache_survives_a_second_warm_reopen(tmp_path: Path) -> None:
    """Given a cached reverse index that one clean warm reopen already used and rewrote on close
    When the store is reopened a second time and the backlinks of root are asked for
    Then they are a and b
    """
    _seed(tmp_path / "graph")
    first = dc.Store.open(tmp_path / "graph", cache_index=True)
    first_root = first.get(Locality, qid="L-root")
    assert first_root is not None
    first.incoming(first_root)
    first.close()
    second = dc.Store.open(tmp_path / "graph", cache_index=True)
    root = second.get(Locality, qid="L-root")
    assert root is not None

    names = _names(second.incoming(root))

    assert names == ["a", "b"]
    second.close()


def test_incremental_fold_follows_a_direct_entity_ref_added_after_cache_load(
    tmp_path: Path,
) -> None:
    """Given a cached reverse index with tag t1 pointing straight at mineral azurite
    When the store is reopened, the index is loaded, and tag t2 pointing at azurite is committed
    Then the backlinks of azurite are t1 and t2
    """
    seed = dc.Store.open(tmp_path / "graph", cache_index=True)
    azurite = Mineral(qid="Q1", name="azurite")
    seed.store(Tag(text="t1", mineral=azurite))
    seed.commit()
    seed.incoming(azurite)
    seed.close()
    s = dc.Store.open(tmp_path / "graph", cache_index=True)
    reopened_azurite = s.get(Mineral, qid="Q1")
    assert reopened_azurite is not None
    s.incoming(reopened_azurite)
    s.store(Tag(text="t2", mineral=reopened_azurite))
    s.commit()

    texts = _texts(s.incoming(reopened_azurite))

    assert texts == ["t1", "t2"]
    s.close()
