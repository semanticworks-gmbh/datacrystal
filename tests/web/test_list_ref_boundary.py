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

from __future__ import annotations

import asyncio
from collections.abc import Iterable
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
    """A mineral species."""

    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Collector:
    """A collector with a lazy one-to-many adjacency to minerals."""

    name: Annotated[str, dc.Unique]
    favorites: list[dc.Lazy[Mineral]] = field(default_factory=list)


# --- data-building helpers ------------------------------------------------------


def _schema_exposing(gql_type: object, value: object) -> strawberry.Schema:
    """Build a one-field ``Query`` whose ``root`` returns ``value`` behind ``gql_type``."""

    def root() -> object:
        return value

    root_field: Any = strawberry.field(resolver=root, graphql_type=gql_type, name="root")
    return strawberry.Schema(query=create_type("Query", [root_field]))


def _field_names(gql_type: type) -> set[str]:
    return {f.name for f in gql_type.__strawberry_definition__.fields}  # type: ignore[attr-defined]


def _minerals(first: int, count: int) -> list[Mineral]:
    """Build ``count`` minerals Q<first>.. named M<first>.."""
    return [Mineral(qid=f"Q{i}", name=f"M{i}") for i in range(first, first + count)]


def _collector(name: str, minerals: list[Mineral]) -> Collector:
    """Build a collector whose favorites are lazy handles on ``minerals``."""
    return Collector(name=name, favorites=[dc.Lazy.of(m) for m in minerals])


def _oid(entity: object) -> int:
    """Return the OID of a committed entity."""
    oid = oid_of(entity)
    if oid is None:
        raise ValueError(f"{entity!r} has no OID, it was never committed")
    return oid


def _oids_of(entities: list[Mineral]) -> list[int]:
    return [_oid(entity) for entity in entities]


def _live_minerals(store: dc.Store, count: int) -> list[Mineral]:
    """Read ``count`` committed minerals back from the store by qid."""
    found = [store.get(Mineral, qid=f"Q{i}") for i in range(count)]
    return [mineral for mineral in found if mineral is not None]


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


# === GraphQL: list relation ====================================================


def test_list_ref_reflects_as_a_list_of_object_edges() -> None:
    """Given the Collector reflected to a Strawberry type
    When its schema is printed as SDL
    Then the reflected fields are name and favorites
    And favorites is a non-null list of nullable Mineral edges
    And a Mineral object type is declared
    """
    gql = StrawberryReflector().reflect(Collector)

    sdl = _schema_exposing(gql, EntityView(0, "x:Collector", {})).as_str()

    assert _field_names(gql) == {"name", "favorites"}
    assert "favorites: [Mineral]!" in sdl
    assert "type Mineral {" in sdl


def test_list_ref_resolves_off_a_real_snapshot(store: dc.Store) -> None:
    """Given a collector with three favorite minerals
    When the query reads the collector's favorites through a snapshot
    Then the query returns no errors
    And the data holds the collector with all three favorites in order
    """
    minerals = _minerals(0, 3)
    collector = _collector("C1", minerals)
    store.root = [collector, *minerals]
    store.commit()
    gql = StrawberryReflector().reflect(Collector)

    with store.snapshot() as snap:
        schema = _schema_exposing(gql, snap.get(_oid(collector)))
        query = "{ root { name favorites { qid name } } }"

        result = asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))

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


def test_empty_list_ref_resolves_to_empty_list(store: dc.Store) -> None:
    """Given a collector with no favorites
    When the query reads the collector's favorites
    Then the query returns no errors
    And favorites is an empty list
    """
    collector = Collector(name="C-empty")
    store.root = collector
    store.commit()
    gql = StrawberryReflector().reflect(Collector)

    with store.snapshot() as snap:
        schema = _schema_exposing(gql, snap.get(_oid(collector)))
        query = "{ root { name favorites { qid } } }"

        result = asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))

    assert result.errors is None
    assert result.data == {"root": {"name": "C-empty", "favorites": []}}


def test_empty_list_ref_never_asks_the_loader(store: dc.Store) -> None:
    """Given a collector with no favorites and a spy on get_many
    When the query reads the collector's favorites
    Then get_many was never called
    """
    collector = Collector(name="C-empty")
    store.root = collector
    store.commit()
    gql = StrawberryReflector().reflect(Collector)

    with store.snapshot() as snap:
        batches = _spy_on_get_many(snap)
        schema = _schema_exposing(gql, snap.get(_oid(collector)))
        query = "{ root { name favorites { qid } } }"

        asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))

    assert batches == []


def test_dangling_list_element_resolves_to_null_not_an_error(store: dc.Store) -> None:
    """Given a collector whose second favorite was deleted
    When the query reads the collector's favorites
    Then the query returns no errors
    And the surviving favorite resolves
    And the deleted favorite's slot is null
    """
    alive = Mineral(qid="QA", name="Alive")
    doomed = Mineral(qid="QD", name="Doomed")
    collector = _collector("C-dangle", [alive, doomed])
    store.root = [collector, alive, doomed]
    store.commit()
    store.delete(doomed)
    store.commit()
    gql = StrawberryReflector().reflect(Collector)

    with store.snapshot() as snap:
        schema = _schema_exposing(gql, snap.get(_oid(collector)))
        query = "{ root { name favorites { qid name } } }"

        result = asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))

    assert result.errors is None
    assert result.data == {
        "root": {"name": "C-dangle", "favorites": [{"qid": "QA", "name": "Alive"}, None]}
    }


def test_sibling_lists_batch_into_one_get_many(store: dc.Store) -> None:
    """Given two collectors with two favorites each and a spy on get_many
    When one list query reads both collectors' favorites
    Then get_many ran once with all four edges
    """
    minerals = _minerals(0, 4)
    c1 = _collector("C1", minerals[:2])
    c2 = _collector("C2", minerals[2:])
    store.root = [c1, c2, *minerals]
    store.commit()
    gql = StrawberryReflector().reflect(Collector)

    with store.snapshot() as snap:
        batches = _spy_on_get_many(snap)
        schema = _schema_exposing(list[gql], _views_of(snap, [_oid(c1), _oid(c2)]))
        query = "{ root { name favorites { qid } } }"

        result = asyncio.run(schema.execute(query, context_value=snapshot_context(snap)))

    assert result.errors is None
    assert batches == [4]


# === REST out: entity_model / to_pydantic ======================================


def test_boundary_annotation_is_list_of_int_not_collapsed_int() -> None:
    """Given the Collector's boundary model
    When its JSON schema is read
    Then favorites is an array
    And its items are integers
    """
    schema = entity_model(Collector).model_json_schema()

    fav = schema["properties"]["favorites"]

    assert fav["type"] == "array"
    assert fav["items"] == {"type": "integer"}


def test_to_pydantic_projects_a_list_edge_as_list_of_oids(store: dc.Store) -> None:
    """Given a committed collector with three favorite minerals
    When to_pydantic projects the collector
    Then favorites is the list of the minerals' OIDs
    """
    minerals = _minerals(0, 3)
    collector = _collector("C1", minerals)
    store.root = [collector, *minerals]
    store.commit()

    dto = to_pydantic(collector)

    assert dto.favorites == _oids_of(minerals)  # pyrefly: ignore[missing-attribute]


def test_to_pydantic_projects_an_empty_list_edge_as_empty_list(store: dc.Store) -> None:
    """Given a committed collector with no favorites
    When to_pydantic projects the collector
    Then favorites is an empty list
    """
    collector = Collector(name="C-empty")
    store.root = collector
    store.commit()

    dto = to_pydantic(collector)

    assert dto.favorites == []  # pyrefly: ignore[missing-attribute]


def test_to_pydantic_projects_a_list_edge_off_a_view(store: dc.Store) -> None:
    """Given a committed collector with two favorite minerals read as a snapshot view
    When to_pydantic projects the view
    Then favorites is the list of the minerals' OIDs
    """
    minerals = _minerals(0, 2)
    collector = _collector("C-view", minerals)
    store.root = [collector, *minerals]
    store.commit()

    with store.snapshot() as snap:
        dto = to_pydantic(snap.get(_oid(collector)))

    assert dto.favorites == _oids_of(minerals)  # pyrefly: ignore[missing-attribute]


# === REST in: from_pydantic ====================================================


def test_from_pydantic_keeps_list_oids_without_store(store: dc.Store) -> None:
    """Given a DTO whose favorites hold three mineral OIDs
    When from_pydantic rebuilds the collector without a store
    Then favorites stay the raw OIDs
    """
    minerals = _minerals(0, 3)
    store.root = minerals
    store.commit()
    dto = entity_model(Collector).model_validate(
        {"name": "C1", "favorites": _oids_of(minerals)}
    )

    rebuilt = from_pydantic(dto, Collector)

    assert list(rebuilt.favorites) == _oids_of(minerals)


def test_from_pydantic_resolves_list_oids_with_store(store: dc.Store) -> None:
    """Given a DTO whose favorites hold three committed minerals' OIDs
    When from_pydantic rebuilds the collector with store=
    Then every favorite is a Lazy
    And each Lazy peeks the live mineral, resident without a load
    """
    store.root = _minerals(0, 3)
    store.commit()
    live = _live_minerals(store, 3)
    dto = entity_model(Collector).model_validate({"name": "C2", "favorites": _oids_of(live)})

    rebuilt = from_pydantic(dto, Collector, store=store)

    assert all(isinstance(h, dc.Lazy) for h in rebuilt.favorites)
    assert [h.peek() for h in rebuilt.favorites] == live


def test_resolved_collector_commits_and_reads_back_its_favorites(store: dc.Store) -> None:
    """Given a collector rebuilt with store= from a DTO of three mineral OIDs
    When it is upserted and committed
    Then it earned an OID
    And a fresh get reads its favorites as Q0, Q1, Q2
    """
    store.root = _minerals(0, 3)
    store.commit()
    live = _live_minerals(store, 3)
    dto = entity_model(Collector).model_validate({"name": "C2", "favorites": _oids_of(live)})
    rebuilt = from_pydantic(dto, Collector, store=store)

    store.upsert(rebuilt)
    store.commit()

    fetched = store.get(Collector, name="C2")
    assert oid_of(rebuilt) is not None
    assert fetched is not None
    assert [f.get().qid for f in fetched.favorites] == ["Q0", "Q1", "Q2"]


def test_from_pydantic_resolves_empty_list_with_store(store: dc.Store) -> None:
    """Given a DTO with an empty favorites list and a store holding a mineral
    When from_pydantic rebuilds the collector with store=
    Then favorites is an empty list
    """
    store.root = Mineral(qid="Q0", name="M0")
    store.commit()
    dto = entity_model(Collector).model_validate({"name": "C-empty", "favorites": []})

    rebuilt = from_pydantic(dto, Collector, store=store)

    assert list(rebuilt.favorites) == []


def test_round_trips_through_to_and_from_pydantic(store: dc.Store) -> None:
    """Given a committed collector with two favorite minerals projected by to_pydantic
    When from_pydantic rebuilds it with store=
    Then the favorites resolve to Q0 and Q1
    """
    minerals = _minerals(0, 2)
    collector = _collector("C-rt", minerals)
    store.root = [collector, *minerals]
    store.commit()
    dto = to_pydantic(collector)

    rebuilt = from_pydantic(dto, Collector, store=store)

    assert [h.peek().qid for h in rebuilt.favorites] == ["Q0", "Q1"]
