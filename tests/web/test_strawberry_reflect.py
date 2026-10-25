"""datacrystal[web] GraphQL — reflect a mineral-cabinet ``@entity`` into a
Strawberry type, then resolve it off a frozen snapshot view (#99 / #49 S6a).

The reflection rules come from the engine's ``TypeInfo`` (shared with the REST
side via ``web._reflect``), so these tests assert two things at once: the
generated GraphQL type carries exactly the entity's fields with the right core
types, *and* a scalar resolves through Strawberry's default ``getattr`` resolver
straight off a real :class:`~datacrystal.EntityView` — no Pydantic, no
copy, no live entity crossing the edge. Entity-reference fields are typed object
edges whose value is still a raw :class:`~datacrystal.Ref` here (the
relation resolver is #100).
"""

from __future__ import annotations

from dataclasses import field
from datetime import UTC, datetime
from typing import Annotated, Any

import pytest

pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")

import strawberry
from strawberry.tools import create_type

import datacrystal as dc
from datacrystal import EntityView, Ref
from datacrystal.web import StrawberryReflector, reflect, reflect_strawberry_type

# --- a self-contained slice of the mineral cabinet (reference + frozen) -------


@dc.entity
class Locality:
    """Where a mineral was first found."""

    qid: Annotated[str, dc.Unique]
    name: str
    country: Annotated[str | None, dc.Index] = None


@dc.entity
class Mineral:
    """A mineral species with scalar, list and reference fields."""

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
    """A frozen cabinet log line."""

    note: str
    kind: Annotated[str, dc.Index] = "misc"


# A reference cycle: Cave and Crystal. Module-scoped so the forward-ref annotation
# resolves (function-local entities can't see each other's names).
@dc.entity
class Cave:
    """A cave pointing at its flagship crystal."""

    qid: Annotated[str, dc.Unique]
    name: str
    flagship: dc.Lazy[Crystal] | None = None


@dc.entity
class Crystal:
    """A crystal pointing back at its home cave."""

    qid: Annotated[str, dc.Unique]
    name: str
    home: dc.Lazy[Cave] | None = None


@dc.entity
class Bag:
    """An entity with a bare list field that GraphQL has no shape for."""

    qid: Annotated[str, dc.Unique]
    name: str
    contents: list = field(default_factory=list)


@dc.entity(frozen=True)
class CatalogEvent:
    """A mineral-cabinet acquisition event with a SortedIndex datetime key.

    Admitting datetime to the engine's indexable-scalar gate (#106) makes
    ``_is_scalar_field`` return True for ``at``, so it is now reflected (it was
    previously dropped as an unmappable ``None``).
    """

    seq: Annotated[int, dc.Unique]
    kind: Annotated[str, dc.Index] = "acquire"
    at: Annotated[datetime | None, dc.SortedIndex] = None


def _field_names(gql_type: type) -> set[str]:
    return {f.name for f in gql_type.__strawberry_definition__.fields}


def _schema_exposing(gql_type: type, value: object) -> strawberry.Schema:
    """Build a one-field ``Query`` whose ``root`` field returns ``value`` behind ``gql_type``.

    This is the consumer pattern (``strawberry.field`` with an explicit
    ``graphql_type`` + ``create_type``) that mounts a reflected type under this
    repo's ``from __future__ import annotations``.
    """

    def root() -> object:
        return value

    root_field: Any = strawberry.field(resolver=root, graphql_type=gql_type, name="root")
    return strawberry.Schema(query=create_type("Query", [root_field]))


def _sdl_of(gql_type: type, entity_name: str) -> str:
    """Print the SDL of a schema exposing an empty view of ``entity_name`` behind ``gql_type``."""
    return _schema_exposing(gql_type, EntityView(0, f"x:{entity_name}", {})).as_str()


# --- reflection shape ---------------------------------------------------------


def test_reflected_type_is_named_for_the_bare_class() -> None:
    """Given the Mineral entity
    When it is reflected to a Strawberry type
    Then the GraphQL type is named Mineral
    """
    gql = reflect_strawberry_type(Mineral)

    assert gql.__strawberry_definition__.name == "Mineral"


def test_reflects_exactly_the_entitys_fields() -> None:
    """Given the Mineral entity with scalar, list and reference fields
    When it is reflected to a Strawberry type
    Then the type carries exactly the entity's field names
    """
    gql = reflect_strawberry_type(Mineral)

    assert _field_names(gql) == {
        "qid",
        "name",
        "crystal_system",
        "mohs",
        "discovery_year",
        "abundant",
        "tags",
        "type_locality",
    }


@pytest.mark.parametrize(
    "declaration",
    [
        "crystalSystem: String",
        "mohs: Float",
        "discoveryYear: Int",
        "abundant: Boolean!",
        "tags: [String!]!",
    ],
)
def test_scalar_leaves_carry_their_graphql_scalar_types(declaration: str) -> None:
    """Given the Mineral entity reflected to a Strawberry type
    When the schema is printed as SDL
    Then the SDL declares the scalar field with its GraphQL type
    """
    gql = reflect_strawberry_type(Mineral)

    sdl = _sdl_of(gql, "Mineral")

    assert declaration in sdl


@pytest.mark.parametrize("declaration", ["typeLocality: Locality", "type Locality {"])
def test_reference_field_is_an_object_edge_to_the_referent_type(declaration: str) -> None:
    """Given the Mineral entity whose type_locality references Locality
    When the schema is printed as SDL
    Then the SDL declares the nullable Locality edge and the Locality object type
    """
    mineral_gql = StrawberryReflector().reflect(Mineral)

    sdl = _sdl_of(mineral_gql, "Mineral")

    assert declaration in sdl


def test_same_referent_reflects_to_one_cached_type() -> None:
    """Given a reflector that reflected Mineral and so its referent Locality
    When Locality is reflected twice
    Then both calls return the same GraphQL type
    """
    reflector = StrawberryReflector()
    reflector.reflect(Mineral)

    first = reflector.reflect(Locality)
    second = reflector.reflect(Locality)

    assert first is second


@pytest.mark.parametrize("declaration", ["home: Cave", "flagship: Crystal"])
def test_mutually_referential_entities_reflect_into_mutual_edges(declaration: str) -> None:
    """Given the reference cycle Cave and Crystal
    When Crystal is reflected and the schema is printed as SDL
    Then the SDL declares both the Crystal to Cave edge and the Cave to Crystal back-reference
    """
    crystal_gql = StrawberryReflector().reflect(Crystal)

    sdl = _sdl_of(crystal_gql, "Crystal")

    assert declaration in sdl


def test_mutually_referential_entities_reflect_into_one_type_each() -> None:
    """Given the reference cycle Cave and Crystal reflected through one reflector
    When each is reflected again
    Then each returns its cached GraphQL type, never a second type of the same name
    """
    reflector = StrawberryReflector()
    crystal_gql = reflector.reflect(Crystal)

    cave_first = reflector.reflect(Cave)
    cave_second = reflector.reflect(Cave)

    assert cave_first is cave_second
    assert reflector.reflect(Crystal) is crystal_gql


def test_unmappable_fields_are_skipped_not_faked() -> None:
    """Given an entity with a bare list field that has no element type
    When it is reflected to a Strawberry type
    Then the type carries only qid and name
    """
    gql = reflect_strawberry_type(Bag)

    assert _field_names(gql) == {"qid", "name"}


# --- frozen entities ----------------------------------------------------------


def test_frozen_entity_reflects_as_frozen_type_info() -> None:
    """Given the frozen LogEntry entity
    When it is reflected to its TypeInfo
    Then the TypeInfo is frozen
    """
    info, _ = reflect(LogEntry)

    assert info.frozen is True


def test_frozen_entity_reflects_like_any_other() -> None:
    """Given the frozen LogEntry entity
    When it is reflected to a Strawberry type
    Then the type carries its note and kind fields
    """
    gql = reflect_strawberry_type(LogEntry)

    assert _field_names(gql) == {"note", "kind"}


def test_defaults_are_factories_not_values() -> None:
    """Given the LogEntry entity whose kind defaults to misc
    When it is reflected to its TypeInfo and descriptors
    Then the kind descriptor has a default
    And the TypeInfo default for kind is callable
    And calling that factory reproduces misc
    """
    info, descriptors = reflect(LogEntry)

    assert [d.has_default for d in descriptors if d.name == "kind"] == [True]
    assert callable(info.defaults["kind"])
    assert info.defaults["kind"]() == "misc"


# --- resolving off a real snapshot view ---------------------------------------


def test_scalar_resolves_off_a_real_snapshot_entity_view(store: dc.Store) -> None:
    """Given a committed quartz read as a frozen snapshot EntityView
    When the GraphQL query reads its scalars
    Then the snapshot gave a frozen view, not the live entity
    And the query returns no errors
    And the data holds every scalar as committed
    """
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

    with store.snapshot() as snap:
        view = snap.get(oid)
        assert isinstance(view, EntityView)
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


def test_reference_field_yields_the_raw_ref_token_pre_resolver(store: dc.Store) -> None:
    """Given a committed quartz whose type locality is a Lazy reference
    When its snapshot view is read
    Then the view's type_locality is the raw Ref token, not a Locality view
    """
    loc = Locality(qid="L1", name="St Gotthard", country="CH")
    quartz = Mineral(qid="Q1", name="Quartz", type_locality=dc.Lazy.of(loc))
    oid = store.store(quartz)
    store.commit()

    with store.snapshot() as snap:
        view = snap.get(oid)

        assert isinstance(view.type_locality, Ref)  # pyrefly: ignore[missing-attribute]


# --- datetime SortedIndex field reflected via GraphQL (#106) ------------------


def test_datetime_sorted_index_field_is_reflected() -> None:
    """Given the CatalogEvent entity with a SortedIndex datetime field
    When it is reflected to a Strawberry type
    Then the type carries seq, kind and at, with at not dropped
    """
    gql = reflect_strawberry_type(CatalogEvent)

    assert _field_names(gql) == {"seq", "kind", "at"}


@pytest.mark.parametrize("declaration", ["at: DateTime", "seq: Int!", "kind: String!"])
def test_datetime_field_leaves_its_neighbours_unchanged_in_the_sdl(declaration: str) -> None:
    """Given the CatalogEvent entity reflected to a Strawberry type
    When the schema is printed as SDL
    Then the SDL declares the datetime as a nullable DateTime scalar
    And the int key and the str index field keep their types
    """
    gql = reflect_strawberry_type(CatalogEvent)

    sdl = _sdl_of(gql, "CatalogEvent")

    assert declaration in sdl


def test_datetime_field_serializes_off_the_view() -> None:
    """Given a CatalogEvent view whose at is a timezone-aware datetime
    When the GraphQL query reads seq, kind and at
    Then the query returns no errors
    And the data holds the datetime as an ISO string
    """
    at = datetime(2021, 6, 1, 12, 0, tzinfo=UTC)
    gql = reflect_strawberry_type(CatalogEvent)
    view = EntityView(0, "x:CatalogEvent", {"seq": 7, "kind": "acquire", "at": at})

    result = _schema_exposing(gql, view).execute_sync("{ root { seq kind at } }")

    assert result.errors is None
    assert result.data == {
        "root": {"seq": 7, "kind": "acquire", "at": "2021-06-01T12:00:00+00:00"}
    }


def test_datetime_field_resolves_none_off_the_view() -> None:
    """Given a CatalogEvent view whose at is None
    When the GraphQL query reads at
    Then the query returns no errors
    And at is GraphQL null
    """
    gql = reflect_strawberry_type(CatalogEvent)
    view = EntityView(0, "x:CatalogEvent", {"seq": 1, "kind": "acquire", "at": None})

    result = _schema_exposing(gql, view).execute_sync("{ root { at } }")

    assert result.errors is None
    assert result.data == {"root": {"at": None}}
