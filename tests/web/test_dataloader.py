"""datacrystal[web] GraphQL — the per-request DataLoader, the N+1 killer (#100 / #49 S6b).

The relation resolver (#100) turns a reference field's raw
:class:`~datacrystal.Ref` (the resolver-less #99 behaviour) into the
referenced :class:`~datacrystal.EntityView` by routing the OID through
a **per-request** :class:`~datacrystal.web.SnapshotLoader`. These tests pin the
four contract properties from the story:

* a 2-level nested query over the mineral cabinet resolves correct data off the
  snapshot (the headline integration test);
* a tick's N sibling references batch into **one**
  :meth:`~datacrystal.Snapshot.get_many` (tick-coalescing, the N+1
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
from collections.abc import Iterable
from typing import Annotated, Any

import pytest

pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")

import strawberry
from strawberry.dataloader import DataLoader
from strawberry.tools import create_type

import datacrystal as dc
from datacrystal import EntityView, Snapshot
from datacrystal.web import (
    LOADER_CONTEXT_KEY,
    SnapshotLoader,
    StrawberryReflector,
    snapshot_context,
)

# --- a self-contained slice of the mineral cabinet (reference + frozen) -------


@dc.entity
class Locality:
    """Where a mineral was first found."""

    qid: Annotated[str, dc.Unique]
    name: str
    country: Annotated[str | None, dc.Index] = None


@dc.entity
class Mineral:
    """A mineral species, optionally pointing at its type locality."""

    qid: Annotated[str, dc.Unique]
    name: str
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None


# --- data-building helpers ------------------------------------------------------


def _schema_exposing(gql_type: object, value: object) -> strawberry.Schema:
    """Build a one-field ``Query`` whose ``root`` returns ``value`` behind ``gql_type``."""

    def root() -> object:
        return value

    root_field: Any = strawberry.field(resolver=root, graphql_type=gql_type, name="root")
    return strawberry.Schema(query=create_type("Query", [root_field]))


def _five_minerals_sharing_a_locality(store: dc.Store) -> list[int]:
    """Commit five minerals pointing at one locality and return their OIDs."""
    locality = Locality(qid="L1", name="St Gotthard", country="CH")
    oids = [
        store.store(Mineral(qid=f"Q{i}", name=f"M{i}", type_locality=dc.Lazy.of(locality)))
        for i in range(5)
    ]
    store.commit()
    return oids


def _views_of(snap: Snapshot, oids: list[int]) -> list[Any]:
    return [snap.get(oid) for oid in oids]


def _spy_on_get_many(snap: Snapshot) -> list[int]:
    """Make ``snap.get_many`` record the key count of each batch into the returned list."""
    batches: list[int] = []
    real_get_many = snap.get_many

    def spy(refs: Iterable[int]) -> list[EntityView | None]:
        keys = list(refs)
        batches.append(len(keys))
        return real_get_many(keys)

    snap.get_many = spy  # type: ignore[method-assign]
    return batches


def _loader_of(snap: Snapshot) -> SnapshotLoader:
    """Return the fresh per-request loader ``snapshot_context`` builds over ``snap``."""
    loader = snapshot_context(snap)[LOADER_CONTEXT_KEY]
    if not isinstance(loader, SnapshotLoader):
        raise TypeError(f"snapshot_context carries {type(loader)}, not a SnapshotLoader")
    return loader


# --- the headline integration test --------------------------------------------


def test_two_level_nested_query_resolves_off_the_snapshot(store: dc.Store) -> None:
    """Given a quartz pointing at its type locality St Gotthard
    When the nested query reads the mineral and its type locality through a snapshot
    Then the query returns no errors
    And the data holds the mineral with its locality hydrated through the loader
    """
    gotthard = Locality(qid="L1", name="St Gotthard", country="CH")
    quartz = Mineral(qid="Q1", name="Quartz", mohs=7.0, type_locality=dc.Lazy.of(gotthard))
    oid = store.store(quartz)
    store.commit()
    gql = StrawberryReflector().reflect(Mineral)

    with store.snapshot() as snap:
        view = snap.get(oid)
        assert isinstance(view, EntityView)
        schema = _schema_exposing(gql, view)
        query = "{ root { qid name mohs typeLocality { qid name country } } }"

        result = asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))

    assert result.errors is None
    assert result.data == {
        "root": {
            "qid": "Q1",
            "name": "Quartz",
            "mohs": 7.0,
            "typeLocality": {"qid": "L1", "name": "St Gotthard", "country": "CH"},
        }
    }


# --- the N+1 killer: tick-coalescing into one get_many ------------------------


def test_sibling_refs_resolve_their_shared_locality(store: dc.Store) -> None:
    """Given five minerals pointing at the same locality
    When one list query reads each mineral's type locality
    Then every mineral resolves the shared locality
    """
    oids = _five_minerals_sharing_a_locality(store)
    gql = StrawberryReflector().reflect(Mineral)

    with store.snapshot() as snap:
        schema = _schema_exposing(list[gql], _views_of(snap, oids))
        query = "{ root { qid typeLocality { qid name } } }"

        result = asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))

    assert result.errors is None
    assert result.data == {
        "root": [
            {"qid": f"Q{i}", "typeLocality": {"qid": "L1", "name": "St Gotthard"}}
            for i in range(5)
        ]
    }


def test_sibling_refs_batch_into_one_get_many(store: dc.Store) -> None:
    """Given five minerals pointing at the same locality and a spy on get_many
    When one list query reads each mineral's type locality
    Then get_many ran once with all five sibling OIDs
    """
    oids = _five_minerals_sharing_a_locality(store)
    gql = StrawberryReflector().reflect(Mineral)

    with store.snapshot() as snap:
        batches = _spy_on_get_many(snap)
        schema = _schema_exposing(list[gql], _views_of(snap, oids))
        query = "{ root { qid typeLocality { qid name } } }"

        result = asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))

    assert result.errors is None
    assert batches == [5]


# --- request scoping is BUILT, not defaulted ----------------------------------


def test_loader_is_built_with_cache_false(store: dc.Store) -> None:
    """Given a snapshot of a committed locality
    When a SnapshotLoader is built over it
    Then its DataLoader has the cache switched off
    """
    store.store(Locality(qid="L1", name="St Gotthard"))
    store.commit()

    with store.snapshot() as snap:
        snap_loader = SnapshotLoader(snap)

        assert snap_loader.loader.cache is False


def test_vendored_dataloader_caches_by_default(store: dc.Store) -> None:
    """Given a snapshot of a committed locality
    When a DataLoader is built over it with the vendored defaults
    Then its cache is switched on
    """
    store.store(Locality(qid="L1", name="St Gotthard"))
    store.commit()

    with store.snapshot() as snap:
        default_loader = DataLoader(load_fn=snap.get_many)  # type: ignore[arg-type]

        assert default_loader.cache is True


def test_default_cache_true_is_a_lifetime_cache_per_oid(store: dc.Store) -> None:
    """Given a cache=True DataLoader that counts its load_fn calls
    When the same OID is loaded in two separate ticks
    Then load_fn ran once, for one key
    And both loads returned the same locality
    """
    oid = store.store(Locality(qid="L1", name="St Gotthard"))
    store.commit()

    with store.snapshot() as snap:
        calls: list[int] = []

        async def counting_load(oids: list[int]) -> list[EntityView | None]:
            calls.append(len(oids))
            return snap.get_many(oids)

        cached: DataLoader[int, EntityView | None] = DataLoader(load_fn=counting_load, cache=True)

        async def two_ticks() -> tuple[Any, Any]:
            first = await cached.load(oid)
            second = await cached.load(oid)
            return first, second

        first, second = asyncio.run(two_ticks())

    assert calls == [1]
    assert first is not None
    assert second is not None
    assert first.name == "St Gotthard"
    assert second.name == "St Gotthard"


def test_per_request_loaders_do_not_leak_across_snapshots(store: dc.Store) -> None:
    """Given a snapshot taken before a rename and another taken after it
    When each snapshot's own loader loads the same OID
    Then the loader over the first snapshot reads the old name
    And the loader over the second snapshot reads the new name
    """
    locality = Locality(qid="L1", name="Old name")
    oid = store.store(locality)
    store.commit()

    with store.snapshot() as snap_before:
        locality.name = "New name"
        store.commit()
        with store.snapshot() as snap_after:
            before = asyncio.run(_loader_of(snap_before).load(oid))
            after = asyncio.run(_loader_of(snap_after).load(oid))

    assert before is not None
    assert after is not None
    assert before.name == "Old name"
    assert after.name == "New name"


# --- dangling reference resolves to GraphQL null, never a 500 -----------------


def test_dangling_reference_resolves_to_null_not_an_error(store: dc.Store) -> None:
    """Given a mineral whose type locality was deleted after the reference was stored
    When the nested query reads the mineral and its type locality
    Then the query returns no errors
    And the data holds the mineral's scalars with a null type locality
    """
    gotthard = Locality(qid="L1", name="St Gotthard")
    quartz = Mineral(qid="Q1", name="Quartz", type_locality=dc.Lazy.of(gotthard))
    oid = store.store(quartz)
    store.store(gotthard)
    store.commit()
    store.delete(gotthard)
    store.commit()
    gql = StrawberryReflector().reflect(Mineral)

    with store.snapshot() as snap:
        schema = _schema_exposing(gql, snap.get(oid))
        query = "{ root { qid name typeLocality { qid } } }"

        result = asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))

    assert result.errors is None
    assert result.data == {"root": {"qid": "Q1", "name": "Quartz", "typeLocality": None}}


def test_absent_reference_resolves_to_null(store: dc.Store) -> None:
    """Given a mineral with no type locality
    When the query reads the mineral and its type locality
    Then the query returns no errors
    And the data holds a null type locality
    """
    oid = store.store(Mineral(qid="Q1", name="Quartz"))
    store.commit()
    gql = StrawberryReflector().reflect(Mineral)

    with store.snapshot() as snap:
        schema = _schema_exposing(gql, snap.get(oid))
        query = "{ root { qid typeLocality { qid } } }"

        result = asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))

    assert result.errors is None
    assert result.data == {"root": {"qid": "Q1", "typeLocality": None}}


def test_absent_reference_never_asks_the_loader(store: dc.Store) -> None:
    """Given a mineral with no type locality and a spy on get_many
    When the query reads the mineral and its type locality
    Then get_many was never called
    """
    oid = store.store(Mineral(qid="Q1", name="Quartz"))
    store.commit()
    gql = StrawberryReflector().reflect(Mineral)

    with store.snapshot() as snap:
        batches = _spy_on_get_many(snap)
        schema = _schema_exposing(gql, snap.get(oid))
        query = "{ root { qid typeLocality { qid } } }"

        asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))

    assert batches == []


def test_relation_resolver_without_loader_context_fails_loudly() -> None:
    """Given a mineral view and a request without snapshot_context
    When the query reads the type locality
    Then the query reports an error
    And the error names snapshot_context
    """
    gql = StrawberryReflector().reflect(Mineral)
    view = EntityView(0, "x:Mineral", {"qid": "Q1", "name": "Quartz", "type_locality": 1})
    schema = _schema_exposing(gql, view)

    result = asyncio.run(
        schema.execute("{ root { typeLocality { qid } } }", context_value={})
    )

    assert result.errors is not None
    assert "snapshot_context" in str(result.errors[0])
