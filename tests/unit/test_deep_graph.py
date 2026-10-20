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
from collections.abc import Callable
from typing import Annotated

import pytest

import datacrystal as dc

StoreFactory = Callable[[], dc.Store]


@dc.entity
class Specimen:
    """A specimen with an eager provenance reference to the specimen it came from."""

    specimen_no: Annotated[str, dc.Unique]
    acquired_from: Specimen | None = None  # EAGER provenance ref → forms a chain


# well past sys.getrecursionlimit() (~1000), and well past the ~6-frames-per-hop
# multiplier of the old recursive read path
DEEP = sys.getrecursionlimit() * 4


def _chain(n: int) -> list[Specimen]:
    """N specimens, each `acquired_from` the next; the last has no provenance."""
    chain = [Specimen(specimen_no=f"S{i}") for i in range(n)]
    for i in range(n - 1):
        chain[i].acquired_from = chain[i + 1]
    return chain


def _hop(node: Specimen, hops: int) -> Specimen:
    """Follow `acquired_from` `hops` times."""
    for _ in range(hops):
        node = node.acquired_from  # pyrefly: ignore[bad-assignment]
    return node


def test_deep_eager_chain_reopens_to_its_last_specimen(store_factory: StoreFactory) -> None:
    """Given a committed provenance chain four times deeper than the recursion limit
    When the store is reopened and the head is followed to the end of the chain
    Then the last specimen is reached
    And it has no provenance
    """
    seeding = store_factory()
    chain = _chain(DEEP)
    seeding.root = {"head": chain[0]}
    seeding.commit()
    seeding.close()

    reopened = store_factory()
    head = reopened.root["head"]
    last = _hop(head, DEEP - 1)

    assert last.specimen_no == f"S{DEEP - 1}"
    assert last.acquired_from is None
    reopened.close()


def test_deep_eager_cycle_returns_to_the_same_head_instance(store_factory: StoreFactory) -> None:
    """Given a committed provenance cycle four times deeper than the recursion limit
    When the store is reopened and the head is followed around the whole cycle
    Then the walk ends on the identical head instance
    """
    seeding = store_factory()
    chain = _chain(DEEP)
    chain[DEEP - 1].acquired_from = chain[0]
    seeding.root = {"head": chain[0]}
    seeding.commit()
    seeding.close()

    reopened = store_factory()
    head = reopened.root["head"]
    after_full_circle = _hop(head, DEEP)

    assert after_full_circle is head
    reopened.close()


def test_deep_eager_diamond_preserves_one_instance_per_oid(store_factory: StoreFactory) -> None:
    """Given two specimens whose provenance both converge on a deep chain's head
    When the store is reopened and both specimens' ancestors are read
    Then both ancestors are the identical live instance
    """
    seeding = store_factory()
    chain = _chain(DEEP)
    left = Specimen(specimen_no="LEFT", acquired_from=chain[0])
    right = Specimen(specimen_no="RIGHT", acquired_from=chain[0])
    seeding.root = {"left": left, "right": right}
    seeding.commit()
    seeding.close()

    reopened = store_factory()
    left_ancestor = reopened.root["left"].acquired_from
    right_ancestor = reopened.root["right"].acquired_from

    assert left_ancestor is right_ancestor
    reopened.close()


def test_dangling_ref_deep_in_chain_raises_dangling_not_recursion(
    store_factory: StoreFactory,
) -> None:
    """Given a committed deep chain whose middle specimen was deleted
    When the store is reopened and the head is read
    Then DanglingRefError is raised
    """
    seeding = store_factory()
    chain = _chain(DEEP)
    seeding.root = {"head": chain[0]}
    seeding.commit()
    seeding.delete(chain[DEEP // 2])
    seeding.commit()
    seeding.close()

    reopened = store_factory()
    with pytest.raises(dc.DanglingRefError):
        _ = reopened.root["head"]
    reopened.close()
