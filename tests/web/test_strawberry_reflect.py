"""datacrystal[web] GraphQL — reflect a mineral-cabinet ``@entity`` into a
Strawberry type, then resolve it off a frozen snapshot view (#99 / #49 S6a).

The reflection rules come from the engine's ``TypeInfo`` (shared with the REST
side via ``web._reflect``), so these tests assert two things at once: the
generated GraphQL type carries exactly the entity's fields with the right core
types, *and* a scalar resolves through Strawberry's default ``getattr`` resolver
straight off a real :class:`~datacrystal._snapshot.EntityView` — no Pydantic, no
copy, no live entity crossing the edge. Entity-reference fields are typed object
edges whose value is still a raw :class:`~datacrystal._snapshot.Ref` here (the
relation resolver is #100).
"""

from __future__ import annotations

from dataclasses import field
from typing import Annotated, Any

import pytest

pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")

import strawberry
from strawberry.tools import create_type

import datacrystal as dc
from datacrystal._snapshot import EntityView, Ref
from datacrystal.web import StrawberryReflector, reflect, reflect_strawberry_type


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
    crystal_system: Annotated[str | None, dc.Index] = None
    mohs: float | None = None
    discovery_year: int | None = None
    abundant: bool = False
    tags: list[str] = field(default_factory=list)
    type_locality: dc.Lazy[Locality] | None = None


@dc.entity(frozen=True)
class LogEntry:
    note: str
    kind: Annotated[str, dc.Index] = "misc"


# A reference cycle: Cave ↔ Crystal. Module-scoped so the forward-ref annotation
# resolves (function-local entities can't see each other's names — the engine
# falls back to a lazy NameError path get_type_hints can't drive here).
@dc.entity
class Cave:
    qid: Annotated[str, dc.Unique]
    name: str
    flagship: "dc.Lazy[Crystal] | None" = None


@dc.entity
class Crystal:
    qid: Annotated[str, dc.Unique]
    name: str
    home: dc.Lazy[Cave] | None = None


def _field_names(gql_type: type) -> set[str]:
    return {f.name for f in gql_type.__strawberry_definition__.fields}


def _schema_exposing(gql_type: type, value: Any) -> strawberry.Schema:
    """A one-field ``Query`` whose ``root`` field returns ``value`` behind the
    reflected ``gql_type`` — the consumer pattern (``strawberry.field`` with an
    explicit ``graphql_type`` + ``create_type``) that mounts a reflected type
    under this repo's ``from __future__ import annotations``."""

    def root() -> object:
        return value

    root_field: Any = strawberry.field(resolver=root, graphql_type=gql_type, name="root")
    return strawberry.Schema(query=create_type("Query", [root_field]))


# --- reflection shape ---------------------------------------------------------


def test_reflects_scalar_fields_with_engine_core_types() -> None:
    """Every scalar field on the entity (incl. list-of-scalar) lands on the
    GraphQL type, named off the entity's own field names and typed off the
    engine's TypeInfo — no hand-written schema class. The GraphQL type name is
    the bare class name."""
    gql = reflect_strawberry_type(Mineral)

    assert gql.__strawberry_definition__.name == "Mineral"
    # StrawberryField carries the Python field name (the camelCase GraphQL name
    # is applied by the schema's NameConverter — asserted on the SDL below).
    assert {
        "qid",
        "name",
        "crystal_system",
        "mohs",
        "discovery_year",
        "abundant",
        "tags",
        "type_locality",
    } == _field_names(gql)

    # On the wire the scalar leaves carry their GraphQL scalar types.
    sdl = _schema_exposing(gql, EntityView(0, "x:Mineral", {})).as_str()
    assert "crystalSystem: String" in sdl  # str | None → nullable String
    assert "mohs: Float" in sdl
    assert "discoveryYear: Int" in sdl
    assert "abundant: Boolean!" in sdl
    assert "tags: [String!]!" in sdl  # list[str] → non-null list of non-null str


def test_reference_field_is_an_object_edge_to_the_referent_type() -> None:
    """``type_locality`` (a ``Lazy[Locality] | None`` reference) becomes a
    GraphQL object field whose type is Locality's reflected type — the relation
    resolver that turns the Ref into a Locality view is #100, not here."""
    reflector = StrawberryReflector()
    mineral_gql = reflector.reflect(Mineral)

    view = EntityView(0, "x:Mineral", {})
    sdl = _schema_exposing(mineral_gql, view).as_str()
    # The edge is declared, typed Locality, nullable (an absent edge is valid).
    assert "typeLocality: Locality" in sdl
    assert "type Locality {" in sdl
    # Same referent → one cached GraphQL type, never duplicated.
    assert reflector.reflect(Locality) is reflector.reflect(Locality)


def test_mutually_referential_entities_reflect_into_one_type_each() -> None:
    """A reference cycle (``Cave`` ↔ ``Crystal``) reflects into two mutually-
    referential GraphQL types — the registry caches each type before recursing
    into its referents, so the cycle terminates and neither type is built twice
    (Strawberry rejects two types sharing a GraphQL name)."""
    reflector = StrawberryReflector()
    crystal_gql = reflector.reflect(Crystal)

    sdl = _schema_exposing(crystal_gql, EntityView(0, "x:Crystal", {})).as_str()
    assert "home: Cave" in sdl  # Crystal → Cave edge
    assert "flagship: Crystal" in sdl  # Cave → Crystal edge (the back-reference)
    # One GraphQL type per entity, reused from the cache.
    assert reflector.reflect(Cave) is reflector.reflect(Cave)
    assert reflector.reflect(Crystal) is crystal_gql


def test_unmappable_fields_are_skipped_not_faked() -> None:
    """A field GraphQL has no native shape for (a bare ``list`` with no element
    type) is left off the type rather than mapped to a lie."""

    @dc.entity
    class Bag:
        qid: Annotated[str, dc.Unique]
        name: str
        contents: list = field(default_factory=list)  # bare list — no element type

    gql = reflect_strawberry_type(Bag)
    assert _field_names(gql) == {"qid", "name"}


# --- frozen entities ----------------------------------------------------------


def test_frozen_entity_reflects_like_any_other() -> None:
    """A ``@entity(frozen=True)`` append-only record reflects from the same
    TypeInfo path — frozen-ness is an engine write-policy, not a schema shape."""
    info, _ = reflect(LogEntry)
    assert info.frozen is True

    gql = reflect_strawberry_type(LogEntry)
    assert _field_names(gql) == {"note", "kind"}


def test_defaults_are_factories_not_values() -> None:
    """``TypeInfo.defaults`` maps name → zero-arg factory; the reflector must
    treat a defaulted field as present, never call a raw value. ``kind="misc"``
    is a default — the field still reflects, and the factory reproduces it."""
    info, descriptors = reflect(LogEntry)
    kind = next(d for d in descriptors if d.name == "kind")
    assert kind.has_default is True
    assert callable(info.defaults["kind"])
    assert info.defaults["kind"]() == "misc"


# --- resolving off a real snapshot view ---------------------------------------


def test_scalar_resolves_off_a_real_snapshot_entity_view(store_factory) -> None:
    """End-to-end: commit a Mineral, snapshot it, and resolve scalars through
    the GraphQL schema directly off the frozen EntityView — Strawberry's default
    ``getattr`` resolver hits ``EntityView.__getattr__``; no Pydantic, no copy,
    no live entity."""
    store = store_factory()
    try:
        quartz = Mineral(
            qid="Q5283",
            name="Quartz",
            crystal_system="trigonal",
            mohs=7.0,
            discovery_year=1800,
            abundant=True,
            tags=["silicate", "common"],
        )
        oid = store.store(quartz)
        store.commit()

        snap = store.snapshot()
        view = snap.get(oid)
        assert isinstance(view, EntityView)  # a frozen view, not the live entity

        schema = _schema_exposing(reflect_strawberry_type(Mineral), view)
        result = schema.execute_sync(
            "{ root { qid name crystalSystem mohs discoveryYear abundant tags } }"
        )
        assert result.errors is None
        assert result.data == {
            "root": {
                "qid": "Q5283",
                "name": "Quartz",
                "crystalSystem": "trigonal",
                "mohs": 7.0,
                "discoveryYear": 1800,
                "abundant": True,
                "tags": ["silicate", "common"],
            }
        }
    finally:
        store.close()


def test_reference_field_yields_the_raw_ref_token_pre_resolver(store_factory) -> None:
    """Until #100 wires the relation resolver, the reference field's default
    resolver returns the raw :class:`Ref` token sitting in the view — proving the
    edge is unresolved here, not hand-hydrated."""
    store = store_factory()
    try:
        loc = Locality(qid="L1", name="St Gotthard", country="CH")
        quartz = Mineral(qid="Q1", name="Quartz", type_locality=dc.Lazy.of(loc))
        store.store(quartz)
        oid = store.store(quartz)
        store.commit()

        view = store.snapshot().get(oid)
        # The frozen view carries a Ref token for the edge, not a Locality view.
        assert isinstance(view.type_locality, Ref)
    finally:
        store.close()
