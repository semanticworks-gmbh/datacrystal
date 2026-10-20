"""Flat-entity ingest fast-path (#52): a type none of whose fields can hold an
entity reference skips the P1 graph-discovery walk on commit, while ref-bearing
types are unaffected. ``TypeInfo.has_entity_refs`` is the static twin of
``Store._walk_value``'s runtime leaf set, so the skip can never drop a real ref.

The win is a constant-factor ingest saving for flat SOR loads (scalars + string
foreign keys); it is asserted here as an *operation count* (zero field walks),
never wall-clock, per invariant 12.
"""

from __future__ import annotations

from collections import deque
from dataclasses import field
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import type_info


@dc.entity
class Specimen:
    """A flat cabinet row: scalars, a string foreign key, a list of scalars.

    Nothing in it can hold an entity reference.
    """

    code: Annotated[str, dc.Unique]
    name: str
    mohs: float = 0.0
    locality_code: str | None = None  # foreign key kept as a plain str
    tags: list[str] = field(default_factory=list)


@dc.entity
class Shelf:
    """Ref-bearing via a direct entity field."""

    label: Annotated[str, dc.Unique]
    holds: Specimen | None = None


@dc.entity
class Drawer:
    """Ref-bearing via a Lazy handle and a container of entities."""

    name: Annotated[str, dc.Unique]
    primary: dc.Lazy[Specimen] | None = None
    contents: list[Specimen] = field(default_factory=list)


@pytest.fixture
def walk_calls(store: dc.Store, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count the store's `_walk_value` calls; `calls["n"]` stays 0 while discovery is skipped."""
    calls = {"n": 0}
    real = type(store)._walk_value

    def counting(
        self: dc.Store,
        value: object,
        queue: deque[tuple[object, object]],
        container: object = None,
        *,
        eager_position: bool = False,
    ) -> None:
        calls["n"] += 1
        real(self, value, queue, container, eager_position=eager_position)

    monkeypatch.setattr(type(store), "_walk_value", counting)
    return calls


def _flat_specimens(count: int) -> list[Specimen]:
    return [
        Specimen(
            code=f"s{i}", name=f"n{i}", mohs=float(i), locality_code="DE-1", tags=["igneous", "quartz"]
        )
        for i in range(count)
    ]


def test_specimen_has_no_entity_refs() -> None:
    """Given the flat Specimen type, all fields ref-free leaves
    When its type info is read
    Then has_entity_refs is False
    """
    flat_has_refs = type_info(Specimen).has_entity_refs

    assert flat_has_refs is False


def test_shelf_with_a_direct_entity_field_has_entity_refs() -> None:
    """Given the Shelf type with a direct Specimen field
    When its type info is read
    Then has_entity_refs is True
    """
    shelf_has_refs = type_info(Shelf).has_entity_refs

    assert shelf_has_refs is True


def test_drawer_with_a_lazy_handle_and_entity_container_has_entity_refs() -> None:
    """Given the Drawer type with a Lazy handle and a list of Specimen
    When its type info is read
    Then has_entity_refs is True
    """
    drawer_has_refs = type_info(Drawer).has_entity_refs

    assert drawer_has_refs is True


def test_flat_type_walks_no_fields(store: dc.Store, walk_calls: dict[str, int]) -> None:
    """Given 25 flat Specimens stored in a store counting field walks
    When the store commits
    Then no field was walked, neither on store() nor on commit
    """
    list(map(store.store, _flat_specimens(25)))

    store.commit()

    assert walk_calls["n"] == 0


def test_ref_bearing_type_still_walks(store: dc.Store, walk_calls: dict[str, int]) -> None:
    """Given a Shelf holding a Specimen stored in a store counting field walks
    When the store commits
    Then fields were walked to discover the graph
    """
    store.store(Shelf(label="sh", holds=Specimen(code="x", name="X")))

    store.commit()

    assert walk_calls["n"] > 0


def test_flat_entity_keeps_its_string_foreign_key(store: dc.Store) -> None:
    """Given a committed flat Specimen with locality_code "DE-7"
    When it is fetched by its unique code
    Then its locality_code is still "DE-7"
    """
    store.store(Specimen(code="q", name="Quartz", mohs=7.0, locality_code="DE-7", tags=["a", "b"]))
    store.commit()

    got = store.get(Specimen, code="q")

    assert got is not None
    assert got.locality_code == "DE-7"


def test_flat_entity_keeps_its_name_and_tags(store: dc.Store) -> None:
    """Given a committed flat Specimen named Quartz tagged "a" and "b"
    When it is fetched by its unique code
    Then its name is "Quartz"
    And its tags are "a" and "b"
    """
    store.store(Specimen(code="q", name="Quartz", mohs=7.0, locality_code="DE-7", tags=["a", "b"]))
    store.commit()

    got = store.get(Specimen, code="q")

    assert got is not None
    assert got.name == "Quartz"
    assert list(got.tags) == ["a", "b"]


@pytest.fixture
def referenced_specimens(store: dc.Store) -> None:
    """Commit specimens reachable only through a Shelf field, a Lazy handle and a container."""
    store.store(Shelf(label="sh", holds=Specimen(code="inner", name="In")))
    store.store(
        Drawer(
            name="d",
            primary=dc.Lazy.of(Specimen(code="a", name="A")),
            contents=[Specimen(code="b", name="B")],
        )
    )
    store.commit()


@pytest.mark.usefixtures("referenced_specimens")
def test_specimen_reached_through_a_direct_field_is_stored(store: dc.Store) -> None:
    """Given a Shelf holding Specimen "inner" committed
    When Specimen "inner" is fetched
    Then it is named "In"
    """
    inner = store.get(Specimen, code="inner")

    assert inner is not None
    assert inner.name == "In"


@pytest.mark.usefixtures("referenced_specimens")
def test_specimen_reached_through_a_lazy_handle_is_stored(store: dc.Store) -> None:
    """Given a Drawer whose primary is a Lazy of Specimen "a" committed
    When Specimen "a" is fetched
    Then it is named "A"
    """
    primary = store.get(Specimen, code="a")

    assert primary is not None
    assert primary.name == "A"


@pytest.mark.usefixtures("referenced_specimens")
def test_specimen_reached_through_a_container_is_stored(store: dc.Store) -> None:
    """Given a Drawer whose contents list holds Specimen "b" committed
    When Specimen "b" is fetched
    Then it is named "B"
    """
    content = store.get(Specimen, code="b")

    assert content is not None
    assert content.name == "B"


@pytest.mark.usefixtures("referenced_specimens")
def test_shelf_ref_resolves_back_to_its_specimen(store: dc.Store) -> None:
    """Given a Shelf holding Specimen "inner" committed
    When the Shelf is fetched and its holds field read
    Then the held specimen is named "In"
    """
    shelf = store.get(Shelf, label="sh")

    assert shelf is not None
    assert shelf.holds is not None
    assert shelf.holds.name == "In"
