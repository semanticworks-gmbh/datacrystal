"""datacrystal[web] — ``entity_model`` reflects an ``@entity`` into a Pydantic model.

Sprint 9 REST boundary (#96 / #49 spike S2, build plan #23): ``entity_model`` is
pure reflection over the engine's ``TypeInfo`` — no store, no instance — so these
tests assert the *shape* of the generated model through ``.model_json_schema()``
(field set, required-vs-optional, the OID mapping for refs, the marker metadata,
and the frozen config). Reflection goes through the engine, never through
class-attribute access (which returns a query ``FieldExpr``), so the canonical
mineral-cabinet domain is exercised here exactly as a FastAPI route would.

The web extra ships ``pydantic``; importorskip so the bare suite stays green
without it (mirrors ``tests/extras/`` for the fts/arrow extras).
"""

from __future__ import annotations

from dataclasses import field
from typing import Annotated, Literal

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")

import pydantic

import datacrystal as dc
from datacrystal.web import entity_model, from_pydantic

MINERAL_FIELDS = [
    "qid",
    "name",
    "crystal_system",
    "mohs",
    "type_locality",
    "discovered_in",
    "tags",
]


@dc.entity
class Locality:
    """A place a mineral was found."""

    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Mineral:
    """A mineral species with scalar, reference and list fields."""

    qid: Annotated[str, dc.Unique]
    name: Annotated[str, dc.FullText]
    crystal_system: Annotated[str | None, dc.Index] = None
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None
    discovered_in: Locality | None = None
    tags: list[str] = field(default_factory=list)


@dc.entity(frozen=True)
class LogEntry:
    """A frozen cabinet log line."""

    note: str
    kind: Annotated[str, dc.Index] = "misc"


@dc.entity
class Specimen:
    """A specimen whose locality reference is required."""

    qid: Annotated[str, dc.Unique]
    locality: dc.Lazy[Locality]


@dc.entity(protected=True)
class CuratedContact:
    """A protected contact whose security labels are lib-managed columns."""

    name: Annotated[str, dc.Unique]
    org: str = ""


class Plain:
    """A class that is not an entity."""


def test_entity_model_returns_a_pydantic_model() -> None:
    """Given the Mineral entity
    When entity_model reflects it
    Then the result is a class
    And it subclasses pydantic.BaseModel
    And it is named Mineral
    """
    model = entity_model(Mineral)

    assert isinstance(model, type)
    assert issubclass(model, pydantic.BaseModel)
    assert model.__name__ == "Mineral"


def test_entity_model_is_cached_per_class() -> None:
    """Given the Mineral entity
    When entity_model reflects it twice
    Then both calls return the same model object
    """
    first = entity_model(Mineral)
    second = entity_model(Mineral)

    assert first is second


def test_distinct_classes_get_distinct_models() -> None:
    """Given the Mineral and Locality entities
    When entity_model reflects each
    Then the two models are different objects
    """
    mineral_model = entity_model(Mineral)
    locality_model = entity_model(Locality)

    assert mineral_model is not locality_model


def test_field_order_follows_typeinfo() -> None:
    """Given the Mineral entity
    When entity_model reflects it
    Then the model's fields follow the persisted schema order
    """
    model = entity_model(Mineral)

    assert list(model.model_fields) == MINERAL_FIELDS


def test_mineral_json_schema_has_the_expected_field_set() -> None:
    """Given a multi-field mineral with scalars, a default, references and a container
    When its JSON schema is generated
    Then the properties are exactly the Mineral fields
    """
    schema = entity_model(Mineral).model_json_schema()

    assert set(schema["properties"]) == set(MINERAL_FIELDS)


def test_only_fields_without_a_default_are_required() -> None:
    """Given a mineral whose qid and name have no default
    When its JSON schema is generated
    Then exactly qid and name are required
    """
    schema = entity_model(Mineral).model_json_schema()

    assert set(schema["required"]) == {"qid", "name"}


def test_scalar_core_types_survive_verbatim() -> None:
    """Given a mineral with string qid and name
    When its JSON schema is generated
    Then qid is a string
    And name is a string
    """
    props = entity_model(Mineral).model_json_schema()["properties"]

    assert props["qid"]["type"] == "string"
    assert props["name"]["type"] == "string"


def test_defaulted_scalars_are_optional_with_the_called_default() -> None:
    """Given a mineral whose crystal system and hardness default to None
    When its JSON schema is generated
    Then the crystal system default is None, the factory called
    And the hardness default is None
    And the hardness accepts a number
    """
    props = entity_model(Mineral).model_json_schema()["properties"]

    assert props["crystal_system"]["default"] is None
    assert props["mohs"]["default"] is None
    assert {"type": "number"} in props["mohs"]["anyOf"]


def test_list_of_scalars_stays_a_typed_array_with_an_empty_default() -> None:
    """Given a mineral with a list[str] tags field
    When its JSON schema is generated
    Then tags is an array
    And its items are strings
    And its default is an empty list
    """
    props = entity_model(Mineral).model_json_schema()["properties"]

    assert props["tags"]["type"] == "array"
    assert props["tags"]["items"] == {"type": "string"}
    assert props["tags"]["default"] == []


@pytest.mark.parametrize("ref", ["type_locality", "discovered_in"])
def test_optional_reference_fields_cross_as_oid_or_null(ref: str) -> None:
    """Given an optional Lazy reference and an optional directly-typed entity field
    When the Mineral JSON schema is generated
    Then the reference accepts an integer OID
    And it accepts null
    And it defaults to None
    """
    props = entity_model(Mineral).model_json_schema()["properties"]

    assert {"type": "integer"} in props[ref]["anyOf"]
    assert {"type": "null"} in props[ref]["anyOf"]
    assert props[ref]["default"] is None


def test_required_oid_when_ref_not_optional() -> None:
    """Given a Specimen whose locality reference is not optional
    When its JSON schema is generated
    Then the locality is an integer
    And it has no default
    And it is required
    """
    schema = entity_model(Specimen).model_json_schema()

    assert schema["properties"]["locality"]["type"] == "integer"
    assert "default" not in schema["properties"]["locality"]
    assert "locality" in schema["required"]


def test_marker_flags_ride_along_as_json_schema_extra() -> None:
    """Given a mineral with Unique, Index and FullText marked fields
    When its JSON schema is generated
    Then the qid is flagged a candidate key
    And the crystal system is flagged queryable
    And the name is flagged searchable
    """
    props = entity_model(Mineral).model_json_schema()["properties"]

    assert props["qid"]["candidate_key"] is True
    assert props["crystal_system"]["queryable"] is True
    assert props["name"]["searchable"] is True


def test_unmarked_field_carries_no_marker_noise() -> None:
    """Given a mineral whose hardness has no marker
    When its JSON schema is generated
    Then the hardness has no candidate_key flag
    And it has no queryable flag
    And it has no searchable flag
    """
    props = entity_model(Mineral).model_json_schema()["properties"]

    assert "candidate_key" not in props["mohs"]
    assert "queryable" not in props["mohs"]
    assert "searchable" not in props["mohs"]


def test_frozen_entity_maps_to_frozen_config() -> None:
    """Given the frozen LogEntry entity
    When entity_model reflects it
    Then the model config is frozen
    """
    model = entity_model(LogEntry)

    assert model.model_config.get("frozen") is True


def test_frozen_entity_dto_refuses_assignment() -> None:
    """Given an instance of the frozen LogEntry model
    When its note is assigned
    Then pydantic raises ValidationError
    """
    inst = entity_model(LogEntry)(note="found in drawer 3")

    with pytest.raises(pydantic.ValidationError):
        inst.note = "edited"  # pyrefly: ignore[missing-attribute]


def test_non_frozen_entity_is_mutable() -> None:
    """Given the non-frozen Mineral entity
    When entity_model reflects it
    Then the model config is not frozen
    """
    model = entity_model(Mineral)

    assert not model.model_config.get("frozen")


def test_rejects_non_entity_class() -> None:
    """Given a plain class that is not an entity
    When entity_model reflects it
    Then it raises NotAnEntityError
    """
    with pytest.raises(dc.NotAnEntityError):
        entity_model(Plain)


# --- protected classes: labels never ride DTOs (ADR-008, W2-1) ---------------


@pytest.mark.parametrize("face", ["plain", "create", "public"])
def test_protected_class_exposes_no_label_columns(
    face: Literal["plain", "create", "public"],
) -> None:
    """Given a protected class whose security labels are lib-managed columns
    When entity_model reflects it for a face
    Then no model field starts with _dc_
    """
    model = entity_model(CuratedContact, face=face)

    assert [name for name in model.model_fields if name.startswith("_dc_")] == []


def test_from_pydantic_gives_a_protected_create_face_inert_birth_labels(
    store: dc.Store,
) -> None:
    """Given a create-face DTO of the protected CuratedContact
    When from_pydantic rebuilds it with store=
    Then the contact keeps its name
    And its owner label is the inert 0
    And its group labels are empty
    """
    dto = entity_model(CuratedContact, face="create")(name="Meyer Solartechnik GmbH", org="PV")

    rec = from_pydantic(dto, CuratedContact, store=store)

    assert rec.name == "Meyer Solartechnik GmbH"
    assert rec._dc_owner == 0  # pyrefly: ignore[missing-attribute]
    assert list(rec._dc_groups) == []  # pyrefly: ignore[missing-attribute]


def test_protected_create_face_record_is_stored_and_read_by_its_owner(
    store: dc.Store,
) -> None:
    """Given a protected contact rebuilt from a create-face DTO
    When uid 7 stores and commits it
    Then uid 7, the owner, reads it back by name
    """
    dto = entity_model(CuratedContact, face="create")(name="Meyer Solartechnik GmbH", org="PV")
    rec = from_pydantic(dto, CuratedContact, store=store)

    with store.acting_as(dc.Principal(uid=7)):
        store.store(rec)
        store.commit()

        owned = store.get(CuratedContact, name="Meyer Solartechnik GmbH")

    assert owned is not None


def test_client_supplied_label_keys_are_ignored() -> None:
    """Given a create body smuggling _dc_owner and _dc_write_floor keys
    When the create-face model validates it
    Then the DTO has no _dc_owner attribute
    And the dumped DTO has no _dc_owner
    And the dumped DTO has no _dc_write_floor
    """
    create = entity_model(CuratedContact, face="create")

    dto = create.model_validate({"name": "Sneaky GmbH", "_dc_owner": 999, "_dc_write_floor": 0})

    dumped = dto.model_dump()
    assert not hasattr(dto, "_dc_owner")
    assert "_dc_owner" not in dumped
    assert "_dc_write_floor" not in dumped
