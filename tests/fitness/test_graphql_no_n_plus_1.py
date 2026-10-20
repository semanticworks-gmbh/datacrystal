"""Fitness gate (#101 / #49 S6c): GraphQL nesting costs O(depth), not O(nodes).

The **non-negotiable** correctness gate for the whole ``datacrystal[web]``
GraphQL tier (#23 perf contract, gate #2). The per-request DataLoader (#100) is
the N+1 killer: a tick's sibling reference resolves coalesce into ONE
:meth:`~datacrystal._snapshot.Snapshot.get_many`, so following N references at a
level is one storage round-trip, not N. The micro-bench can prove the loader
*exists*; only a real nested resolve over the engine proves it actually
**batches**.

The gate runs a nested GraphQL query of depth ``D`` over a tree that fans out
``N`` siblings at every level and asserts the storage backend sees exactly ``D``
batch reads — one ``load_many`` per relation *level* — while ``N**D`` and more
entities are resolved. Pin the **shape**, not the speed (invariant 12 — op
counts, never wall-clock): if ``load_many`` scaled with the node count instead
of the level count, an N+1 regression slipped the loader and this fails the PR
that introduced it.

It reuses the KICKOFF counting-storage harness pattern from
:mod:`tests.fitness.test_scale_shape` (``CountingBackend.load_calls``), counting
at the storage seam the snapshot actually reads through
(:meth:`~datacrystal._snapshot.Snapshot.get_many` →
``_load_missing`` → ``read_view.load_many``) — so the count is the true number
of round-trips the DataLoader produced, not a spy on the loader's own batching.

Mineral-cabinet domain, no external data, fast lane; gated on the ``web`` extra
being installed (``strawberry``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Annotated, BinaryIO, cast

import pytest

pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")

import strawberry
from strawberry.tools import create_type
from strawberry.types.field import StrawberryField

import datacrystal as dc
from datacrystal._entity import oid_of
from datacrystal._snapshot import Snapshot
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import (
    BootInfo,
    CommitBatch,
    StorageReadView,
    StoredBlob,
    StoredRecord,
)
from datacrystal.web import StrawberryReflector, snapshot_context

# --- a self-referential slice of the mineral cabinet --------------------------
#
# Each Mineral weathers to exactly ONE other mineral (an alteration product), so
# a single-reference edge chains arbitrarily deep. The query fans out N siblings
# at the root and follows the single ``weathers_to`` edge D levels down: every
# sibling at a level resolves its edge in the SAME resolver tick, so the level's
# N edges batch into one get_many — the property under test.


@dc.entity
class Mineral:
    """A mineral that weathers to one other mineral and alters to many."""

    qid: Annotated[str, dc.Unique]
    name: str
    weathers_to: dc.Lazy[Mineral] | None = None
    # The #103 list edge: a node alters to MANY products (a fan-out tree). The
    # list-edge gate below proves that following a level's lists costs ONE
    # get_many — O(depth), never O(nodes) — exactly like the scalar edge.
    alters_to: list[dc.Lazy[Mineral]] = field(default_factory=list)


# --- the counting storage harness (KICKOFF pattern, test_scale_shape.py) ------


class _CountingReadView:
    """Wrap a backend read view and count the ``load_many`` round-trips the snapshot issues.

    That is the seam ``Snapshot.get_many`` reads through. Index builds go via ``scan_type`` and
    are deliberately not counted (the documented one-time cost), like the live-store wrapper.
    """

    def __init__(self, inner: StorageReadView, counter: CountingBackend) -> None:
        self._inner = inner
        self._counter = counter

    def boot(self) -> BootInfo:
        return self._inner.boot()

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        out = self._inner.load_many(oids)
        self._counter.load_calls += 1
        self._counter.records_loaded += len(out)
        return out

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        return self._inner.scan_type(cid)

    def load_blob(self, oid: int) -> StoredBlob | None:
        return self._inner.load_blob(oid)

    def open_blob_stream(self, oid: int, on_close: Callable[[], None] | None = None) -> BinaryIO:
        return self._inner.open_blob_stream(oid, on_close)

    def close(self) -> None:
        self._inner.close()


class CountingBackend:
    """Count the batch reads on a backend's read views.

    The KICKOFF 'counting storage wrapper' pattern (``test_scale_shape.py``), pointed at the
    snapshot read path the GraphQL DataLoader drives. ``load_calls`` is the number of
    ``load_many`` round-trips; the N+1 gate asserts it scales with relation LEVELS, never node
    count.
    """

    def __init__(self, inner: MemoryBackend) -> None:
        """Wrap the inner backend with zeroed counters."""
        self._inner = inner
        self.load_calls = 0
        self.records_loaded = 0

    def reset(self) -> None:
        """Zero both counters."""
        self.load_calls = 0
        self.records_loaded = 0

    def boot(self) -> BootInfo:
        """Delegate to the inner backend."""
        return self._inner.boot()

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        """Delegate to the inner backend."""
        return self._inner.load_many(oids)

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        """Delegate to the inner backend."""
        return self._inner.scan_type(cid)

    def load_blob(self, oid: int) -> StoredBlob | None:
        """Delegate to the inner backend."""
        return self._inner.load_blob(oid)

    def apply(self, batch: CommitBatch) -> None:
        """Delegate to the inner backend."""
        return self._inner.apply(batch)

    def read_view(self) -> _CountingReadView:
        """Delegate to the inner backend."""
        return _CountingReadView(self._inner.read_view(), self)

    def close(self) -> None:
        """Delegate to the inner backend."""
        self._inner.close()


# --- the fanned-out tree --------------------------------------------------------


def _build_chain_tree(store: dc.Store, depth: int, fanout: int) -> list[int]:
    """Store ``fanout`` ``weathers_to`` chains of ``depth`` edges and return the chain heads' OIDs.

    Every chain head sits at level 0; following ``weathers_to`` ``depth`` times walks ``depth``
    relation levels. With ``fanout`` heads queried as siblings, each level holds ``fanout``
    nodes whose edges resolve in one tick.
    """
    heads: list[int] = []
    for chain in range(fanout):
        # Build the chain tail-first so each node can reference the next; the
        # last node built is the head (deepest is level ``depth``, head level 0).
        nxt: Mineral | None = None
        head_oid = 0
        for level in range(depth, -1, -1):
            m = Mineral(
                qid=f"Q{chain}-{level}",
                name=f"weather-stage-{level}",
                weathers_to=dc.Lazy.of(nxt) if nxt is not None else None,
            )
            head_oid = store.store(m)  # ascends to the head on the last iteration
            nxt = m
        heads.append(head_oid)
    store.commit()
    return heads


def _schema_exposing_list(gql_type: type, values: Sequence[object]) -> strawberry.Schema:
    """Return a one-field ``Query`` whose ``root`` returns ``values`` behind ``list[gql_type]``.

    That is the #99/#100 consumer pattern.
    """

    def root() -> object:
        return values

    root_field = cast(
        StrawberryField, strawberry.field(resolver=root, graphql_type=list[gql_type], name="root")
    )
    return strawberry.Schema(query=create_type("Query", [root_field]))


def _nested_selection(depth: int) -> str:
    """Return a GraphQL selection following ``weathers_to`` ``depth`` levels deep."""
    inner = "qid name"
    for _ in range(depth):
        inner = f"qid name weathersTo {{ {inner} }}"
    return f"{{ root {{ {inner} }} }}"


@dataclass
class Tree:
    """A snapshot over a counting backend and the level-0 nodes a query starts from."""

    backend: CountingBackend
    snapshot: Snapshot
    roots: Sequence[object]


@pytest.fixture
def open_chain_tree() -> Iterator[Callable[[int, int], Tree]]:
    """Yield a function that opens a ``fanout``-sibling, ``depth``-edge chain tree.

    The roots are loaded and any index build is paid before the counters reset, so a count
    isolates exactly the reference-following reads. Stores and snapshots close at teardown.
    """
    with ExitStack() as cleanup:

        def open_tree(depth: int, fanout: int) -> Tree:
            backend = CountingBackend(MemoryBackend())
            store = dc.Store._from_backend(backend)
            cleanup.callback(store.close)
            heads = _build_chain_tree(store, depth=depth, fanout=fanout)
            snapshot = cleanup.enter_context(store.snapshot())
            roots = [snapshot.get(oid) for oid in heads]
            backend.reset()
            return Tree(backend, snapshot, roots)

        yield open_tree


# --- the gate -----------------------------------------------------------------


def test_nested_graphql_issues_one_batch_read_per_level(
    open_chain_tree: Callable[[int, int], Tree],
) -> None:
    """Given 6 sibling chains of 4 weathers_to edges, 24 followed edges in all
    When a depth-4 nested GraphQL query resolves them
    Then the storage backend sees exactly 4 load_many calls, one per relation level

    The non-negotiable N+1 gate. Without the per-request DataLoader each of the 24 followed
    edges would be its own store read.
    """
    tree = open_chain_tree(4, 6)
    schema = _schema_exposing_list(StrawberryReflector().reflect(Mineral), tree.roots)

    result = asyncio.run(
        schema.execute(_nested_selection(4), context_value=snapshot_context(tree.snapshot))
    )

    assert result.errors is None, result.errors
    assert tree.backend.load_calls == 4, (
        f"N+1 REGRESSION: {tree.backend.load_calls} get_many batches for a depth-4 query, "
        "expected 4 (one per relation LEVEL). The per-request DataLoader is no longer "
        "coalescing sibling resolves into one Snapshot.get_many; reads scale with NODE COUNT."
    )


def test_nested_graphql_batches_stay_below_the_followed_edges(
    open_chain_tree: Callable[[int, int], Tree],
) -> None:
    """Given 6 sibling chains of 4 weathers_to edges, 24 followed edges in all
    When a depth-4 nested GraphQL query resolves them
    Then fewer load_many calls than 24 were issued, O(depth) and never O(nodes)
    """
    tree = open_chain_tree(4, 6)
    schema = _schema_exposing_list(StrawberryReflector().reflect(Mineral), tree.roots)

    asyncio.run(
        schema.execute(_nested_selection(4), context_value=snapshot_context(tree.snapshot))
    )

    assert tree.backend.load_calls < 24


def test_nested_graphql_serves_every_followed_edge_from_the_batches(
    open_chain_tree: Callable[[int, int], Tree],
) -> None:
    """Given 6 sibling chains of 4 weathers_to edges, 24 followed edges in all
    When a depth-4 nested GraphQL query resolves them
    Then the batches loaded 24 records, one per followed edge
    """
    tree = open_chain_tree(4, 6)
    schema = _schema_exposing_list(StrawberryReflector().reflect(Mineral), tree.roots)

    asyncio.run(
        schema.execute(_nested_selection(4), context_value=snapshot_context(tree.snapshot))
    )

    assert tree.backend.records_loaded == 24


@pytest.mark.parametrize("fanout", [4, 16])
def test_batch_count_does_not_grow_with_the_fanout(
    open_chain_tree: Callable[[int, int], Tree], fanout: int
) -> None:
    """Given sibling chains of 3 edges at a fanout of 4 or 16, 4x apart
    When a depth-3 nested GraphQL query resolves them
    Then the backend sees exactly 3 load_many calls at either fanout

    Only the haystack (fanout) grows; the read count must not.
    """
    tree = open_chain_tree(3, fanout)
    schema = _schema_exposing_list(StrawberryReflector().reflect(Mineral), tree.roots)

    result = asyncio.run(
        schema.execute(_nested_selection(3), context_value=snapshot_context(tree.snapshot))
    )

    assert result.errors is None, result.errors
    assert tree.backend.load_calls == 3, (
        f"batch count must stay 3 (one per level) at fanout {fanout}; "
        "the DataLoader batched per node, not per level (N+1)"
    )


# --- the list-edge gate (#103) ------------------------------------------------
#
# The scalar gate above proves O(depth) for a single-ref edge; this one proves
# the SAME property for a list-valued ``alters_to`` edge. A node alters to
# ``fanout`` children, so level L holds ``fanout**L`` nodes — a fan-out TREE, not
# parallel chains. Following the ``alters_to`` list ``depth`` levels deep, every
# node at a level resolves its child list in the SAME resolver tick: the loader
# coalesces all of a level's lists into ONE get_many. So D batch reads serve a
# tree of ``fanout + fanout**2 + …`` followed edges — O(depth), never O(nodes),
# at the list level. A regression to per-list batching would make load_calls
# scale with the node count and fail this PR.


def _build_alteration_tree(store: dc.Store, depth: int, fanout: int) -> int:
    """Store a fan-out ``alters_to`` tree ``depth`` levels deep and return the root OID.

    Every node has ``fanout`` children. It is built leaves-first so each parent can reference
    its already-stored children as ``Lazy`` adjacency.
    """
    counter = [0]

    def build(level: int) -> Mineral:
        i = counter[0]
        counter[0] += 1
        children: list[dc.Lazy[Mineral]] = []
        if level < depth:
            children = [dc.Lazy.of(build(level + 1)) for _ in range(fanout)]
        node = Mineral(qid=f"Q{i}", name=f"alt-{level}-{i}", alters_to=children)
        store.store(node)
        return node

    root = build(0)
    store.commit()
    return cast(int, oid_of(root))


def _list_nested_selection(depth: int) -> str:
    """Return a GraphQL selection following the ``alters_to`` LIST edge ``depth`` deep."""
    inner = "qid name"
    for _ in range(depth):
        inner = f"qid name altersTo {{ {inner} }}"
    return f"{{ root {{ {inner} }} }}"


@pytest.fixture
def alteration_tree() -> Iterator[Tree]:
    """Yield a snapshot over a depth-3, fanout-3 ``alters_to`` tree with only the root loaded.

    The tree has 3 + 9 + 27 = 39 followed edges across 3 levels.
    """
    backend = CountingBackend(MemoryBackend())
    store = dc.Store._from_backend(backend)
    root_oid = _build_alteration_tree(store, depth=3, fanout=3)
    with store.snapshot() as snapshot:
        root_view = snapshot.get(root_oid)
        backend.reset()
        yield Tree(backend, snapshot, [root_view])
    store.close()


def test_list_edge_nesting_issues_one_batch_read_per_level(alteration_tree: Tree) -> None:
    """Given a fan-out-3 tree of alters_to lists, 39 followed edges across 3 levels
    When a depth-3 nested GraphQL query follows the list edge
    Then the backend sees exactly 3 load_many calls, one per relation level

    The #103 list-edge N+1 gate: the per-request DataLoader coalesces every list at a level
    into one batch; per-list batching would scale reads with NODE COUNT.
    """
    schema = _schema_exposing_list(StrawberryReflector().reflect(Mineral), alteration_tree.roots)

    result = asyncio.run(
        schema.execute(
            _list_nested_selection(3), context_value=snapshot_context(alteration_tree.snapshot)
        )
    )

    assert result.errors is None, result.errors
    assert alteration_tree.backend.load_calls == 3, (
        f"LIST-EDGE N+1 REGRESSION: {alteration_tree.backend.load_calls} get_many batches for a "
        "depth-3 query over a fan-out-3 tree, expected 3 (one per relation LEVEL)."
    )


def test_list_edge_batches_stay_below_the_followed_edges(alteration_tree: Tree) -> None:
    """Given a fan-out-3 tree of alters_to lists, 39 followed edges across 3 levels
    When a depth-3 nested GraphQL query follows the list edge
    Then fewer load_many calls than 39 were issued, O(depth) and never O(nodes)
    """
    schema = _schema_exposing_list(StrawberryReflector().reflect(Mineral), alteration_tree.roots)

    asyncio.run(
        schema.execute(
            _list_nested_selection(3), context_value=snapshot_context(alteration_tree.snapshot)
        )
    )

    assert alteration_tree.backend.load_calls < 39


def test_list_edge_serves_every_followed_edge_from_the_batches(alteration_tree: Tree) -> None:
    """Given a fan-out-3 tree of alters_to lists, 39 followed edges across 3 levels
    When a depth-3 nested GraphQL query follows the list edge
    Then the batches loaded 39 records, one per followed edge
    """
    schema = _schema_exposing_list(StrawberryReflector().reflect(Mineral), alteration_tree.roots)

    asyncio.run(
        schema.execute(
            _list_nested_selection(3), context_value=snapshot_context(alteration_tree.snapshot)
        )
    )

    assert alteration_tree.backend.records_loaded == 39
