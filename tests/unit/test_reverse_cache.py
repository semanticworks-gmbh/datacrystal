"""Reverse-reference index caching (#63): the global ``incoming()`` index is
persisted to the same watermark-stamped sidecar as the forward indexes, so a warm
reopen serves backlinks without re-harvesting every field of every record. Like
the forward cache it is never authoritative — a commit landing before the reverse
index materializes invalidates the cached blob, and ``ensure_reverse()`` rebuilds
from the current records (the #71 contract, applied to the reverse index).
"""

from __future__ import annotations

from typing import Annotated

import datacrystal as dc
import datacrystal._indexes as _idx


@dc.entity
class Node:
    name: Annotated[str, dc.Unique]
    parent: "Node | None" = None


def _seed(path) -> None:
    s = dc.Store.open(path, cache_index=True)
    root = Node(name="root")
    s.store(Node(name="a", parent=root))
    s.store(Node(name="b", parent=root))
    s.commit()
    assert sorted(n.name for n in s.incoming(root)) == ["a", "b"]  # build the reverse index
    s.close()                                                       # → cached on the sidecar


def test_warm_reopen_serves_incoming_from_cache(tmp_path, monkeypatch):
    path = tmp_path / "graph"
    _seed(path)

    harvests: list[int] = []
    real = _idx.harvest_ref_oids
    monkeypatch.setattr(_idx, "harvest_ref_oids",
                        lambda v: harvests.append(1) or real(v))

    s = dc.Store.open(path, cache_index=True)
    root = s.get(Node, name="root")
    assert sorted(n.name for n in s.incoming(root)) == ["a", "b"]   # same backlinks
    assert harvests == []   # served from the cache — NO O(corpus) re-harvest scan
    s.close()


def test_insert_before_incoming_invalidates_reverse_cache(tmp_path):
    path = tmp_path / "graph"
    _seed(path)

    # add a new child BEFORE touching the reverse index — the cached blob is stale
    s = dc.Store.open(path, cache_index=True)
    root = s.get(Node, name="root")
    s.store(Node(name="c", parent=root))
    s.commit()
    # incoming() must reflect the new edge (blob invalidated → rebuilt from records)
    assert sorted(n.name for n in s.incoming(root)) == ["a", "b", "c"]
    s.close()


def test_delete_before_incoming_invalidates_reverse_cache(tmp_path):
    path = tmp_path / "graph"
    _seed(path)

    # delete a referrer BEFORE touching the reverse index — the cached blob (with
    # 'a') must not resurrect it.
    s = dc.Store.open(path, cache_index=True)
    a = s.get(Node, name="a")
    s.delete(a)
    s.commit()
    root = s.get(Node, name="root")
    assert sorted(n.name for n in s.incoming(root)) == ["b"]
    s.close()


def test_incremental_fold_after_cache_load_rebuilds_refs(tmp_path):
    # incoming() loads `_reverse` from the cache (the diff memory is left dirty);
    # a later commit must rebuild that memory from `_reverse` and fold correctly.
    path = tmp_path / "graph"
    _seed(path)
    s = dc.Store.open(path, cache_index=True)
    root = s.get(Node, name="root")
    assert sorted(n.name for n in s.incoming(root)) == ["a", "b"]   # loads _reverse
    s.store(Node(name="c", parent=root))   # add an edge
    s.delete(s.get(Node, name="a"))        # drop an edge
    s.commit()
    assert sorted(n.name for n in s.incoming(root)) == ["b", "c"]   # folded incrementally
    s.close()
    s2 = dc.Store.open(path, cache_index=True)                       # and it persists
    root2 = s2.get(Node, name="root")
    assert sorted(n.name for n in s2.incoming(root2)) == ["b", "c"]
    s2.close()


def test_reverse_cache_round_trips_across_reopens(tmp_path):
    path = tmp_path / "graph"
    _seed(path)
    # two clean warm reopens in a row, both correct (the cache is rewritten each
    # close at the current watermark)
    for _ in range(2):
        s = dc.Store.open(path, cache_index=True)
        root = s.get(Node, name="root")
        assert sorted(n.name for n in s.incoming(root)) == ["a", "b"]
        s.close()
