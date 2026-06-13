"""Deep / cyclic EAGER reference graphs reopen without RecursionError (#29).

The write path (`_register_graph`) walks the graph iteratively with a deque; the
read path must too. A provenance chain (a specimen `acquired_from` a previous
specimen) deeper than Python's recursion limit commits fine, then crashes on the
first read at reopen — the asymmetry this fixes. Found in the graph-workload
probe; the EVAL-STRATEGY portfolio (GLEIF / deps.dev / GH Archive) confirms it is
the gate to the navigational graph persona, not a corner case.

Parametrized over both backends via store_factory.
"""

from __future__ import annotations

import sys
from typing import Annotated

import pytest

import datacrystal as dc


@dc.entity
class Specimen:
    specimen_no: Annotated[str, dc.Unique]
    acquired_from: Specimen | None = None  # EAGER provenance ref → forms a chain


# well past sys.getrecursionlimit() (~1000), and well past the ~6-frames-per-hop
# multiplier of the old recursive read path
DEEP = sys.getrecursionlimit() * 4


def _chain(n: int) -> list[Specimen]:
    chain = [Specimen(specimen_no=f"S{i}") for i in range(n)]
    for i in range(n - 1):
        chain[i].acquired_from = chain[i + 1]
    return chain


def test_deep_eager_chain_reopens_without_recursionerror(store_factory):
    s = store_factory()
    chain = _chain(DEEP)
    s.root = {"head": chain[0]}
    s.commit()  # the write path already handles this depth
    s.close()

    reopened = store_factory()
    node = reopened.root["head"]  # materializes the WHOLE eager chain at once
    depth = 0
    while node.acquired_from is not None:
        node = node.acquired_from
        depth += 1
    assert depth == DEEP - 1
    assert node.specimen_no == f"S{DEEP - 1}"
    reopened.close()


def test_deep_eager_cycle_terminates_and_keeps_identity(store_factory):
    s = store_factory()
    chain = _chain(DEEP)
    chain[DEEP - 1].acquired_from = chain[0]  # close the cycle
    s.root = {"head": chain[0]}
    s.commit()
    s.close()

    reopened = store_factory()
    head = reopened.root["head"]  # must not RecursionError nor hang
    node = head
    for _ in range(DEEP):  # walking exactly DEEP hops around the cycle...
        node = node.acquired_from
    assert node is head  # ...returns to the SAME head instance (cycle + identity)
    reopened.close()


def test_deep_eager_diamond_preserves_one_instance_per_oid(store_factory):
    # Two provenance paths converge on a deep ancestor; both must yield the same
    # live instance (identity across the iterative load).
    s = store_factory()
    chain = _chain(DEEP)
    left = Specimen(specimen_no="LEFT", acquired_from=chain[0])
    right = Specimen(specimen_no="RIGHT", acquired_from=chain[0])
    s.root = {"left": left, "right": right}
    s.commit()
    s.close()

    reopened = store_factory()
    assert reopened.root["left"].acquired_from is reopened.root["right"].acquired_from
    reopened.close()


def test_dangling_ref_deep_in_chain_raises_dangling_not_recursion(store_factory):
    s = store_factory()
    chain = _chain(DEEP)
    s.root = {"head": chain[0]}
    s.commit()
    s.delete(chain[DEEP // 2])  # ADR-003 unchecked delete, mid-chain → dangling
    s.commit()
    s.close()

    reopened = store_factory()
    with pytest.raises(dc.DanglingRefError):
        _ = reopened.root["head"]  # iterative load reaches the hole (not a crash)
    reopened.close()
