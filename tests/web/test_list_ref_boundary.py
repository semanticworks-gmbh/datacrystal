"""datacrystal[web] — list-valued reference fields across the web boundary (#103).

The Sprint-12 centerpiece: a ``list[Lazy[T]]`` adjacency (the #30 multi-valued
edge) crosses BOTH the REST and GraphQL boundary as a **list of edges**, so a
knowledge-graph API can expose one-to-many relationships instead of collapsing
them to a single scalar edge. These tests pin all three seams over the canonical
mineral cabinet, mirroring the single-ref web tests:

* **GraphQL list relation** — a list-of-ref field reflects ``list[Target!]`` and
  resolves through the per-request DataLoader (one ``Snapshot.get_many`` per
  tick, never one per list — the N+1 property the fitness gate hardens);
* **REST out** — ``entity_model``'s boundary annotation is ``list[int]`` (not a
  collapsed ``int``); ``to_pydantic`` projects the edge as a list of edge OIDs;
* **REST in** — ``from_pydantic`` routes a ``list[int]`` cell through ref
  resolution (one ``store.get_many``, each rewrapped as ``Lazy.of``); with no
  store the raw OID list is preserved.

Edge cases (the story's acceptance): an empty list → ``[]``; a dangling element
OID → GraphQL ``null`` (ADR-003 unchecked deletes). The entity shape mirrors the
existing list-adjacency fixtures ``Route.stops`` / ``Collector.favorites``.

The web extra ships ``pydantic`` + ``strawberry``; importorskip so the bare suite
stays green without it (mirrors ``tests/extras/`` for the fts/arrow extras).
"""

# A ``create_model`` DTO is a dynamically built type, so pyright cannot see its
# reflected fields (``dto.qid``) — the same untypeable situation the magic-query
# tests carry a pragma for.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
from dataclasses import field
from typing import Annotated, Any

import pytest

pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")
pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")

import strawberry
from strawberry.tools import create_type

import datacrystal as dc
from datacrystal._entity import oid_of
from datacrystal._snapshot import EntityView, Snapshot
from datacrystal.web import (
    StrawberryReflector,
    entity_model,
    from_pydantic,
    snapshot_context,
    to_pydantic,
)


# --- a list-adjacency slice of the mineral cabinet (mirrors Collector.favorites) ---


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Collector:
    name: Annotated[str, dc.Unique]
    favorites: list[dc.Lazy[Mineral]] = field(default_factory=list)  # lazy adjacency


def _schema_exposing(gql_type: Any, value: Any) -> strawberry.Schema:
    """A one-field ``Query`` whose ``root`` returns ``value`` behind ``gql_type``
    — the #99 consumer pattern (``strawberry.field`` + ``create_type``)."""

    def root() -> object:
        return value

    root_field: Any = strawberry.field(resolver=root, graphql_type=gql_type, name="root")
    return strawberry.Schema(query=create_type("Query", [root_field]))


def _execute(schema: strawberry.Schema, query: str, snap: Snapshot) -> Any:
    return asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))


def _field_names(gql_type: type) -> set[str]:
    return {f.name for f in gql_type.__strawberry_definition__.fields}


# === GraphQL: list relation ====================================================


def test_list_ref_reflects_as_a_list_of_object_edges() -> None:
    """``favorites`` (a ``list[Lazy[Mineral]]``) becomes a GraphQL ``list`` of
    Mineral edges — NOT a single collapsed Mineral object. The list is non-null
    (empty → ``[]``); elements are nullable so a dangling edge can be ``null``."""
    reflector = StrawberryReflector()
    gql = reflector.reflect(Collector)
    assert _field_names(gql) == {"name", "favorites"}

    sdl = _schema_exposing(gql, EntityView(0, "x:Collector", {})).as_str()
    # A list of (nullable) Mineral edges, the list itself non-null.
    assert "favorites: [Mineral]!" in sdl
    assert "type Mineral {" in sdl


def test_list_ref_resolves_off_a_real_snapshot(store_factory) -> None:
    """End-to-end: a Collector with three favorite minerals resolves the whole
    adjacency list off a frozen snapshot through the DataLoader."""
    store = store_factory()
    try:
        ms = [store.store(Mineral(qid=f"Q{i}", name=f"M{i}")) for i in range(3)]
        store.commit()
        minerals = [store.get_many([o])[0] for o in ms]
        col = Collector(name="C1", favorites=[dc.Lazy.of(m) for m in minerals])
        oid = store.store(col)
        store.commit()

        gql = StrawberryReflector().reflect(Collector)
        with store.snapshot() as snap:
            view = snap.get(oid)
            schema = _schema_exposing(gql, view)
            result = _execute(schema, "{ root { name favorites { qid name } } }", snap)
            assert result.errors is None
            assert result.data == {
                "root": {
                    "name": "C1",
                    "favorites": [
                        {"qid": "Q0", "name": "M0"},
                        {"qid": "Q1", "name": "M1"},
                        {"qid": "Q2", "name": "M2"},
                    ],
                }
            }
    finally:
        store.close()


def test_empty_list_ref_resolves_to_empty_list_without_a_load(store_factory) -> None:
    """An empty adjacency list resolves to ``[]`` and never touches the loader —
    no edges to follow, no store read."""
    store = store_factory()
    try:
        oid = store.store(Collector(name="C-empty"))  # favorites=[]
        store.commit()

        gql = StrawberryReflector().reflect(Collector)
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
            result = _execute(schema, "{ root { name favorites { qid } } }", snap)
            assert result.errors is None
            assert result.data == {"root": {"name": "C-empty", "favorites": []}}
            assert batches == []  # no edge → loader never asked
    finally:
        store.close()


def test_dangling_list_element_resolves_to_null_not_an_error(store_factory) -> None:
    """A deleted favorite (v0.x unchecked deletes, ADR-003) rides ``get_many``'s
    None-on-miss: that slot is GraphQL ``null``, the surviving edges resolve, and
    there is no 500 — the list edge inherits the scalar edge's dangling tolerance."""
    store = store_factory()
    try:
        a = Mineral(qid="QA", name="Alive")
        d = Mineral(qid="QD", name="Doomed")
        store.store(a)
        store.store(d)
        col = Collector(name="C-dangle", favorites=[dc.Lazy.of(a), dc.Lazy.of(d)])
        oid = store.store(col)
        store.commit()
        store.delete(d)  # the second edge now dangles
        store.commit()

        gql = StrawberryReflector().reflect(Collector)
        with store.snapshot() as snap:
            view = snap.get(oid)
            schema = _schema_exposing(gql, view)
            result = _execute(schema, "{ root { name favorites { qid name } } }", snap)
            assert result.errors is None
            assert result.data == {
                "root": {
                    "name": "C-dangle",
                    "favorites": [{"qid": "QA", "name": "Alive"}, None],
                }
            }
    finally:
        store.close()


def test_sibling_lists_batch_into_one_get_many(store_factory) -> None:
    """Two collectors each with a list of favorites, queried as siblings in one
    list field, resolve EVERY edge across BOTH lists in a single
    ``Snapshot.get_many`` — the per-request DataLoader coalesces across lists, so
    nesting costs O(depth), never O(nodes). This is the N+1 killer at list level."""
    store = store_factory()
    try:
        ms = [store.store(Mineral(qid=f"Q{i}", name=f"M{i}")) for i in range(4)]
        store.commit()
        minerals = [store.get_many([o])[0] for o in ms]
        c1 = Collector(name="C1", favorites=[dc.Lazy.of(minerals[0]), dc.Lazy.of(minerals[1])])
        c2 = Collector(name="C2", favorites=[dc.Lazy.of(minerals[2]), dc.Lazy.of(minerals[3])])
        oids = [store.store(c1), store.store(c2)]
        store.commit()

        gql = StrawberryReflector().reflect(Collector)
        with store.snapshot() as snap:
            views = [snap.get(o) for o in oids]
            batches: list[int] = []
            real_get_many = snap.get_many

            def spy(refs: Any) -> Any:
                keys = list(refs)
                batches.append(len(keys))
                return real_get_many(keys)

            snap.get_many = spy  # type: ignore[method-assign]

            schema = _schema_exposing(list[gql], views)
            result = _execute(schema, "{ root { name favorites { qid } } }", snap)
            assert result.errors is None
            # Four edges across two sibling lists in ONE get_many — not one per list.
            assert batches == [4]
    finally:
        store.close()


# === REST out: entity_model / to_pydantic ======================================


def test_boundary_annotation_is_list_of_int_not_collapsed_int() -> None:
    """``favorites`` crosses the REST edge as ``list[int]`` (a list of edge OIDs),
    not a single collapsed ``int`` — the request DTO transports the whole
    one-to-many relationship."""
    schema = entity_model(Collector).model_json_schema()
    fav = schema["properties"]["favorites"]
    assert fav["type"] == "array"
    assert fav["items"] == {"type": "integer"}


def test_to_pydantic_projects_a_list_edge_as_list_of_oids(store_factory) -> None:
    """A committed Collector projects its adjacency to a ``list[int]`` of the
    favorite minerals' OIDs (refs cross as OIDs, never live objects)."""
    store = store_factory()
    try:
        ms = [Mineral(qid=f"Q{i}", name=f"M{i}") for i in range(3)]
        col = Collector(name="C1", favorites=[dc.Lazy.of(m) for m in ms])
        store.root = [col, *ms]
        store.commit()

        dto = to_pydantic(col)
        assert dto.favorites == [oid_of(m) for m in ms]
    finally:
        store.close()


def test_to_pydantic_projects_an_empty_list_edge_as_empty_list(store_factory) -> None:
    store = store_factory()
    try:
        col = Collector(name="C-empty")
        store.root = col
        store.commit()
        dto = to_pydantic(col)
        assert dto.favorites == []
    finally:
        store.close()


def test_to_pydantic_projects_a_list_edge_off_a_view(store_factory) -> None:
    """The snapshot-view input path projects the same ``list[int]`` (the view's
    tuple of Ref tokens decays to a list of OIDs)."""
    store = store_factory()
    try:
        ms = [Mineral(qid=f"Q{i}", name=f"M{i}") for i in range(2)]
        col = Collector(name="C-view", favorites=[dc.Lazy.of(m) for m in ms])
        store.root = [col, *ms]
        store.commit()
        with store.snapshot() as snap:
            view = snap.get(oid_of(col))
            dto = to_pydantic(view)
        assert dto.favorites == [oid_of(m) for m in ms]
    finally:
        store.close()


# === REST in: from_pydantic ====================================================


def test_from_pydantic_keeps_list_oids_without_store(store_factory) -> None:
    """No store passed: a ``list[int]`` edge stays a list of raw OIDs — the honest
    no-hydrate default that never silently touches storage."""
    store = store_factory()
    try:
        ms = [Mineral(qid=f"Q{i}", name=f"M{i}") for i in range(3)]
        store.root = ms
        store.commit()
        oids = [oid_of(m) for m in ms]

        dto = entity_model(Collector).model_validate({"name": "C1", "favorites": oids})
        rebuilt = from_pydantic(dto, Collector)
        assert list(rebuilt.favorites) == oids  # raw OIDs preserved
    finally:
        store.close()


def test_from_pydantic_resolves_list_oids_with_store(store_factory) -> None:
    """With ``store=``: every OID in the list resolves to its live Mineral on the
    owner thread and is rewrapped as ``Lazy.of`` (the lazy-adjacency field shape)
    — and the reconstructed Collector commits cleanly."""
    store = store_factory()
    try:
        ms = [Mineral(qid=f"Q{i}", name=f"M{i}") for i in range(3)]
        store.root = ms
        store.commit()
        live = [store.get(Mineral, qid=f"Q{i}") for i in range(3)]
        oids = [oid_of(m) for m in live]

        dto = entity_model(Collector).model_validate({"name": "C2", "favorites": oids})
        rebuilt = from_pydantic(dto, Collector, store=store)
        assert all(isinstance(h, dc.Lazy) for h in rebuilt.favorites)
        # Each handle resolved to the live entity by identity, resident (no load).
        assert [h.peek() for h in rebuilt.favorites] == live

        store.upsert(rebuilt)
        store.commit()
        assert oid_of(rebuilt) is not None
        fetched = store.get(Collector, name="C2")
        assert [f.get().qid for f in fetched.favorites] == ["Q0", "Q1", "Q2"]
    finally:
        store.close()


def test_from_pydantic_resolves_empty_list_with_store(store_factory) -> None:
    """An empty list edge resolves to an empty list even with a store — no OIDs to
    fetch, no spurious round-trip."""
    store = store_factory()
    try:
        store.root = Mineral(qid="Q0", name="M0")  # something to commit
        store.commit()
        dto = entity_model(Collector).model_validate({"name": "C-empty", "favorites": []})
        rebuilt = from_pydantic(dto, Collector, store=store)
        assert list(rebuilt.favorites) == []
    finally:
        store.close()


def test_round_trips_through_to_and_from_pydantic(store_factory) -> None:
    """The full REST round-trip: project a committed Collector to a DTO, rebuild a
    STATE_NEW Collector, resolve its edges against the store — the adjacency
    survives end to end."""
    store = store_factory()
    try:
        ms = [Mineral(qid=f"Q{i}", name=f"M{i}") for i in range(2)]
        col = Collector(name="C-rt", favorites=[dc.Lazy.of(m) for m in ms])
        store.root = [col, *ms]
        store.commit()

        dto = to_pydantic(col)
        rebuilt = from_pydantic(dto, Collector, store=store)
        assert [h.peek().qid for h in rebuilt.favorites] == ["Q0", "Q1"]
    finally:
        store.close()
