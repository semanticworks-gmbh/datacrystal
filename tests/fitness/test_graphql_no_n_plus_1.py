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
from typing import Annotated, Any, Callable, Iterator

import pytest

pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")

import strawberry
from strawberry.tools import create_type

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import BootInfo, CommitBatch, StoredBlob, StoredRecord
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
    qid: Annotated[str, dc.Unique]
    name: str
    weathers_to: dc.Lazy["Mineral"] | None = None


# --- the counting storage harness (KICKOFF pattern, test_scale_shape.py) ------


class _CountingReadView:
    """Wraps a backend read view, counting the ``load_many`` round-trips the
    snapshot issues — the seam ``Snapshot.get_many`` reads through. Index
    builds go via ``scan_type`` and are deliberately not counted (the documented
    one-time cost), exactly like the live-store counting wrapper."""

    def __init__(self, inner: Any, counter: "CountingBackend") -> None:
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

    def open_blob_stream(
        self, oid: int, on_close: Callable[[], None] | None = None
    ) -> Any:
        return self._inner.open_blob_stream(oid, on_close)

    def close(self) -> None:
        self._inner.close()


class CountingBackend:
    """Storage wrapper counting batch reads on its read views — the KICKOFF
    'counting storage wrapper' pattern (``test_scale_shape.py``), pointed at the
    snapshot read path the GraphQL DataLoader drives. ``load_calls`` is the
    number of ``load_many`` round-trips; the N+1 gate asserts it scales with
    relation LEVELS, never node count."""

    def __init__(self, inner: MemoryBackend) -> None:
        self._inner = inner
        self.load_calls = 0
        self.records_loaded = 0

    def reset(self) -> None:
        self.load_calls = 0
        self.records_loaded = 0

    def boot(self) -> BootInfo:
        return self._inner.boot()

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        return self._inner.load_many(oids)

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        return self._inner.scan_type(cid)

    def load_blob(self, oid: int) -> StoredBlob | None:
        return self._inner.load_blob(oid)

    def apply(self, batch: CommitBatch) -> None:
        return self._inner.apply(batch)

    def read_view(self) -> _CountingReadView:
        return _CountingReadView(self._inner.read_view(), self)

    def close(self) -> None:
        self._inner.close()


# --- the fanned-out tree --------------------------------------------------------


def _build_chain_tree(store: dc.Store, depth: int, fanout: int) -> list[int]:
    """Store ``fanout`` independent ``weathers_to`` chains, each ``depth`` edges
    long, and return the OIDs of the ``fanout`` chain heads (the query's roots).

    Every chain head sits at level 0; following ``weathers_to`` ``depth`` times
    walks ``depth`` relation levels. With ``fanout`` heads queried as siblings,
    each level holds ``fanout`` nodes whose edges resolve in one tick."""
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


def _schema_exposing_list(gql_type: Any, values: list[Any]) -> strawberry.Schema:
    """A one-field ``Query`` whose ``root`` returns ``values`` behind
    ``list[gql_type]`` — the #99/#100 consumer pattern."""

    def root() -> object:
        return values

    root_field: Any = strawberry.field(
        resolver=root, graphql_type=list[gql_type], name="root"
    )
    return strawberry.Schema(query=create_type("Query", [root_field]))


def _nested_selection(depth: int) -> str:
    """A GraphQL selection following ``weathers_to`` ``depth`` levels deep."""
    inner = "qid name"
    for _ in range(depth):
        inner = f"qid name weathersTo {{ {inner} }}"
    return f"{{ root {{ {inner} }} }}"


# --- the gate -----------------------------------------------------------------


def test_nested_graphql_does_o_depth_reads_not_o_nodes() -> None:
    """The non-negotiable N+1 gate: a depth-``D`` query over a tree with ``N``
    siblings per level issues exactly ``D`` batch reads — ``load_many`` scales
    with relation LEVELS, not the ``N``-node breadth. Without the per-request
    DataLoader each of the ``N*D`` followed edges would be its own store read."""
    depth, fanout = 4, 6  # 6 chains, each 4 edges deep → 24 followed edges
    backend = CountingBackend(MemoryBackend())
    store = dc.Store._from_backend(backend)
    try:
        heads = _build_chain_tree(store, depth=depth, fanout=fanout)

        gql = StrawberryReflector().reflect(Mineral)
        with store.snapshot() as snap:
            roots = [snap.get(oid) for oid in heads]  # materialize level 0

            # Reset AFTER the roots are loaded and any index build is paid, so
            # the count isolates exactly the reference-following reads.
            backend.reset()

            schema = _schema_exposing_list(gql, roots)
            result = asyncio.run(
                schema.execute(
                    _nested_selection(depth),
                    context_value=snapshot_context(snap),
                )
            )

        assert result.errors is None, result.errors

        followed_edges = fanout * depth  # every edge actually resolved
        assert backend.load_calls == depth, (
            f"N+1 REGRESSION: a depth-{depth} GraphQL query following "
            f"{followed_edges} reference edges across {fanout} siblings per "
            f"level issued {backend.load_calls} get_many batches — expected "
            f"exactly {depth} (one per relation LEVEL). The per-request "
            "DataLoader is no longer coalescing sibling resolves into one "
            "Snapshot.get_many; reads scale with NODE COUNT, not depth."
        )
        # The whole point of O(depth): the batch count is far below the node
        # count it would be if each edge were its own read.
        assert backend.load_calls < followed_edges, (
            f"load_many calls ({backend.load_calls}) must stay below the "
            f"{followed_edges} edges followed — O(depth), never O(nodes)"
        )
        # Every followed edge was served by those D batches.
        assert backend.records_loaded == followed_edges
    finally:
        store.close()


def test_n_plus_1_batching_holds_as_fanout_grows() -> None:
    """The batch count is invariant to breadth: 4× the siblings per level still
    costs ``D`` batches. This is the same-extent twin of the scale-shape gate —
    only the haystack (fanout) grows; the read count must not."""
    depth = 3
    counts: list[int] = []
    for fanout in (4, 16):  # 4× apart, like test_scale_shape's two extents
        backend = CountingBackend(MemoryBackend())
        store = dc.Store._from_backend(backend)
        try:
            heads = _build_chain_tree(store, depth=depth, fanout=fanout)
            gql = StrawberryReflector().reflect(Mineral)
            with store.snapshot() as snap:
                roots = [snap.get(oid) for oid in heads]
                backend.reset()
                schema = _schema_exposing_list(gql, roots)
                result = asyncio.run(
                    schema.execute(
                        _nested_selection(depth),
                        context_value=snapshot_context(snap),
                    )
                )
            assert result.errors is None, result.errors
            counts.append(backend.load_calls)
        finally:
            store.close()

    assert counts == [depth, depth], (
        f"batch count must stay {depth} (one per level) as fanout grows 4×, "
        f"got {counts} — the DataLoader batched per node, not per level (N+1)"
    )
