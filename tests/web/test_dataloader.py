"""datacrystal[web] GraphQL — the per-request DataLoader, the N+1 killer (#100 / #49 S6b).

The relation resolver (#100) turns a reference field's raw
:class:`~datacrystal._snapshot.Ref` (the resolver-less #99 behaviour) into the
referenced :class:`~datacrystal._snapshot.EntityView` by routing the OID through
a **per-request** :class:`~datacrystal.web.SnapshotLoader`. These tests pin the
four contract properties from the story:

* a 2-level nested query over the mineral cabinet resolves correct data off the
  snapshot (the headline integration test);
* a tick's N sibling references batch into **one**
  :meth:`~datacrystal._snapshot.Snapshot.get_many` (tick-coalescing, the N+1
  killer) — asserted by spying on the snapshot;
* request scoping is **built, not defaulted**: the loader is ``cache=False`` and
  fresh per request — tested *against* the vendored ``cache=True`` lifetime cache
  that would otherwise leak reads across watermarks;
* a dangling/deleted reference resolves to GraphQL ``null`` (rides
  ``get_many``'s None-on-miss, ADR-003), never a 500.

The schema is mounted the same way #99's tests mount a reflected type: a one-field
``Query`` whose ``root`` returns a frozen view (or list of views), executed
**async** (``schema.execute``) because the relation resolver and DataLoader are
async, with ``context_value=snapshot_context(snap)`` carrying the per-request
loader.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import pytest

pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")

import strawberry
from strawberry.dataloader import DataLoader
from strawberry.tools import create_type

import datacrystal as dc
from datacrystal._snapshot import EntityView, Snapshot
from datacrystal.web import (
    LOADER_CONTEXT_KEY,
    SnapshotLoader,
    StrawberryReflector,
    snapshot_context,
)


# --- a self-contained slice of the mineral cabinet (reference + frozen) -------


@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str
    country: Annotated[str | None, dc.Index] = None


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None


def _schema_exposing(gql_type: Any, value: Any) -> strawberry.Schema:
    """A one-field ``Query`` whose ``root`` returns ``value`` behind ``gql_type``
    — the #99 consumer pattern (``strawberry.field`` + ``create_type``)."""

    def root() -> object:
        return value

    root_field: Any = strawberry.field(resolver=root, graphql_type=gql_type, name="root")
    return strawberry.Schema(query=create_type("Query", [root_field]))


def _execute(schema: strawberry.Schema, query: str, snap: Snapshot) -> Any:
    """Run ``query`` async (the resolver is async) with a fresh per-request
    context carrying a :class:`SnapshotLoader` over ``snap``."""
    return asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))


# --- the headline integration test --------------------------------------------


def test_two_level_nested_query_resolves_off_the_snapshot(store_factory) -> None:
    """A 2-level nested GraphQL query (Mineral → type_locality → Locality fields)
    returns correct data resolved entirely off a frozen snapshot — the reference
    edge is hydrated through the per-request DataLoader, not hand-walked."""
    store = store_factory()
    try:
        gotthard = Locality(qid="L1", name="St Gotthard", country="CH")
        quartz = Mineral(
            qid="Q1", name="Quartz", mohs=7.0, type_locality=dc.Lazy.of(gotthard)
        )
        oid = store.store(quartz)
        store.commit()

        gql = StrawberryReflector().reflect(Mineral)
        with store.snapshot() as snap:
            view = snap.get(oid)
            assert isinstance(view, EntityView)
            schema = _schema_exposing(gql, view)
            result = _execute(
                schema,
                "{ root { qid name mohs typeLocality { qid name country } } }",
                snap,
            )
            assert result.errors is None
            assert result.data == {
                "root": {
                    "qid": "Q1",
                    "name": "Quartz",
                    "mohs": 7.0,
                    "typeLocality": {
                        "qid": "L1",
                        "name": "St Gotthard",
                        "country": "CH",
                    },
                }
            }
    finally:
        store.close()


# --- the N+1 killer: tick-coalescing into one get_many ------------------------


def test_sibling_refs_batch_into_one_get_many(store_factory) -> None:
    """N minerals each pointing at the same locality, queried in one list field,
    produce exactly **one** ``Snapshot.get_many`` of N OIDs — Strawberry's
    ``call_soon`` tick-coalescing (``dataloader.py:248``) collapses the sibling
    ``.load()`` calls into a single batch. This is the N+1 killer: without the
    loader each edge would be its own store read."""
    store = store_factory()
    try:
        loc = Locality(qid="L1", name="St Gotthard", country="CH")
        oids = [
            store.store(
                Mineral(qid=f"Q{i}", name=f"M{i}", type_locality=dc.Lazy.of(loc))
            )
            for i in range(5)
        ]
        store.commit()

        gql = StrawberryReflector().reflect(Mineral)
        with store.snapshot() as snap:
            views = [snap.get(o) for o in oids]
            # Spy on get_many to record each batch's key count.
            batches: list[int] = []
            real_get_many = snap.get_many

            def spy(refs: Any) -> Any:
                keys = list(refs)
                batches.append(len(keys))
                return real_get_many(keys)

            snap.get_many = spy  # type: ignore[method-assign]

            schema = _schema_exposing(list[gql], views)
            result = _execute(schema, "{ root { qid typeLocality { qid name } } }", snap)

            assert result.errors is None
            # Every mineral resolved its locality...
            assert all(
                row["typeLocality"] == {"qid": "L1", "name": "St Gotthard"}
                for row in result.data["root"]
            )
            # ...in exactly one get_many of all five sibling OIDs (not 5 reads).
            assert batches == [5]
    finally:
        store.close()


# --- request scoping is BUILT, not defaulted ----------------------------------


def test_loader_is_built_with_cache_false_not_the_vendored_default(store_factory) -> None:
    """:class:`SnapshotLoader` constructs its DataLoader with ``cache=False`` —
    explicitly overriding the vendored ``cache=True`` default
    (``dataloader.py:139``). The default is a *lifetime* cache; we want a loader
    whose only scope is the request, so caching is off and a fresh loader is
    built per request (:func:`snapshot_context`)."""
    store = store_factory()
    try:
        store.store(Locality(qid="L1", name="St Gotthard"))
        store.commit()
        with store.snapshot() as snap:
            snap_loader = SnapshotLoader(snap)
            assert snap_loader.loader.cache is False
            # The default constructor, by contrast, would cache for a lifetime.
            assert DataLoader(load_fn=snap.get_many).cache is True  # type: ignore[arg-type]
    finally:
        store.close()


def test_default_cache_true_is_a_lifetime_cache_per_oid(store_factory) -> None:
    """The reason ``cache=False`` is load-bearing: the vendored default
    ``cache=True`` is a *lifetime* OID→future cache (``dataloader.py:164``). Two
    ``.load(oid)`` calls in separate ticks against a ``cache=True`` loader hit the
    cached future and call ``load_fn`` **once** — the cache outlives the tick. If
    such a loader were reused across requests it would serve request 1's
    snapshot read to request 2 (a cross-watermark leak), which is exactly why
    :class:`SnapshotLoader` turns caching off and is built fresh per request
    (the next test). Here we pin the lifetime-cache behaviour itself."""
    store = store_factory()
    try:
        oid = store.store(Locality(qid="L1", name="St Gotthard"))
        store.commit()
        with store.snapshot() as snap:
            calls: list[int] = []

            async def counting_load(oids: list[int]) -> list[EntityView | None]:
                calls.append(len(oids))
                return snap.get_many(oids)

            cached: DataLoader[int, EntityView | None] = DataLoader(
                load_fn=counting_load, cache=True
            )

            async def two_ticks() -> tuple[Any, Any]:
                first = await cached.load(oid)  # tick 1 → load_fn
                second = await cached.load(oid)  # tick 2 → served from the cache
                return first, second

            first, second = asyncio.run(two_ticks())
            assert first is not None and second is not None
            assert first.name == second.name == "St Gotthard"
            # The lifetime cache served the second read without a second load_fn.
            assert calls == [1]
    finally:
        store.close()


def test_per_request_loaders_do_not_leak_across_snapshots(store_factory) -> None:
    """The contract :class:`SnapshotLoader` builds: a loader per request, each
    over its own snapshot, ``cache=False`` — so a read on one watermark never
    bleeds into another. Two snapshots taken either side of a commit each resolve
    the SAME OID to their OWN watermark's value through their OWN loader."""
    store = store_factory()
    try:
        loc = Locality(qid="L1", name="Old name")
        oid = store.store(loc)
        store.commit()
        snap_before = store.snapshot()

        loc.name = "New name"
        store.commit()
        snap_after = store.snapshot()

        try:

            async def read(snap: Snapshot) -> EntityView | None:
                # A fresh per-request loader over this snapshot (cache=False).
                loader = snapshot_context(snap)[LOADER_CONTEXT_KEY]
                assert isinstance(loader, SnapshotLoader)
                return await loader.load(oid)

            before = asyncio.run(read(snap_before))
            after = asyncio.run(read(snap_after))
            assert before is not None and after is not None
            # Each loader pinned its own watermark — no cross-snapshot leak.
            assert before.name == "Old name"
            assert after.name == "New name"
        finally:
            snap_before.close()
            snap_after.close()
    finally:
        store.close()


# --- dangling reference resolves to GraphQL null, never a 500 -----------------


def test_dangling_reference_resolves_to_null_not_an_error(store_factory) -> None:
    """A reference whose target was deleted (v0.x unchecked deletes, ADR-003)
    rides ``get_many``'s None-on-miss: the field resolves to GraphQL ``null`` with
    no errors — never a 500. The owning Mineral still resolves its own scalars."""
    store = store_factory()
    try:
        gotthard = Locality(qid="L1", name="St Gotthard")
        quartz = Mineral(qid="Q1", name="Quartz", type_locality=dc.Lazy.of(gotthard))
        oid = store.store(quartz)
        store.store(gotthard)
        store.commit()
        store.delete(gotthard)  # the edge now dangles
        store.commit()

        gql = StrawberryReflector().reflect(Mineral)
        with store.snapshot() as snap:
            view = snap.get(oid)
            schema = _schema_exposing(gql, view)
            result = _execute(schema, "{ root { qid name typeLocality { qid } } }", snap)
            assert result.errors is None
            assert result.data == {
                "root": {"qid": "Q1", "name": "Quartz", "typeLocality": None}
            }
    finally:
        store.close()


def test_absent_reference_resolves_to_null_without_touching_the_loader(
    store_factory,
) -> None:
    """A ``None`` edge (no reference at all) short-circuits to ``null`` without a
    DataLoader round-trip — the resolver returns ``None`` before calling
    ``load``, so an unset edge costs no store read."""
    store = store_factory()
    try:
        oid = store.store(Mineral(qid="Q1", name="Quartz"))  # no type_locality
        store.commit()

        gql = StrawberryReflector().reflect(Mineral)
        with store.snapshot() as snap:
            view = snap.get(oid)
            batches: list[int] = []
            real_get_many = snap.get_many

            def spy(refs: Any) -> Any:
                keys = list(refs)
                batches.append(len(keys))
                return real_get_many(keys)

            snap.get_many = spy  # type: ignore[method-assign]

            schema = _schema_exposing(gql, view)
            result = _execute(schema, "{ root { qid typeLocality { qid } } }", snap)
            assert result.errors is None
            assert result.data == {"root": {"qid": "Q1", "typeLocality": None}}
            # No edge to follow → the loader was never asked.
            assert batches == []
    finally:
        store.close()


def test_relation_resolver_without_loader_context_fails_loudly() -> None:
    """A request executed without a per-request loader on the context (forgot
    ``snapshot_context``) raises a clear error pointing at the fix, rather than
    silently N+1-ing or returning wrong data."""
    gql = StrawberryReflector().reflect(Mineral)
    view = EntityView(0, "x:Mineral", {"qid": "Q1", "name": "Quartz", "type_locality": 1})
    schema = _schema_exposing(gql, view)
    result = asyncio.run(
        schema.execute("{ root { typeLocality { qid } } }", context_value={})
    )
    assert result.errors is not None
    assert "snapshot_context" in str(result.errors[0])
