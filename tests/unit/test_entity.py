"""@entity mechanics: slots, identity, markers, frozen mode, field exprs."""

from __future__ import annotations

import weakref
from collections.abc import Mapping
from dataclasses import field
from datetime import date, datetime
from typing import Annotated, Any

import pytest

import datacrystal as dc
from datacrystal._conditions import FieldExpr, Pred
from datacrystal._entity import FieldSpec, type_info
from tests.conftest import LogEntry, Mineral


def _specs_by_name(entity: type) -> dict[str, FieldSpec]:
    return {spec.name: spec for spec in type_info(entity).specs}


def _first_coordinate(old: Mapping[str, Any]) -> float:
    return float(old["coords"].split(",")[0])


def _same_record(old: Mapping[str, Any]) -> Mapping[str, Any]:
    return old


def _x_of(old: Mapping[str, Any]) -> str:
    return old["x"]


def _zero(old: Mapping[str, Any]) -> float:
    return 0.0


def test_entity_instances_have_no_dict() -> None:
    """Given a Mineral instance
    When its __dict__ is read
    Then AttributeError is raised
    """
    mineral = Mineral(qid="Q1", name="quartz")

    with pytest.raises(AttributeError):
        _ = mineral.__dict__


def test_entity_rejects_an_unknown_attribute() -> None:
    """Given a Mineral instance
    When an attribute it has no slot for is assigned
    Then AttributeError is raised
    """
    mineral = Mineral(qid="Q1", name="quartz")

    with pytest.raises(AttributeError):
        mineral.nonexistent_field = 1  # pyrefly: ignore[missing-attribute]


def test_entities_support_weakrefs() -> None:
    """Given a Mineral instance
    When a weak reference to it is made
    Then the weak reference resolves to the instance
    """
    mineral = Mineral(qid="Q1", name="quartz")

    reference = weakref.ref(mineral)

    assert reference() is mineral


def test_equal_field_values_do_not_make_entities_equal() -> None:
    """Given two Minerals with identical field values
    When they are compared
    Then they are not equal
    """
    first = Mineral(qid="Q1", name="quartz")
    second = Mineral(qid="Q1", name="quartz")

    assert first != second


def test_an_entity_equals_itself() -> None:
    """Given a Mineral instance
    When it is compared with itself
    Then they are equal
    """
    mineral = Mineral(qid="Q1", name="quartz")

    assert mineral == mineral  # noqa: PLR0124 - equality is the behaviour under test


def test_frozen_entity_keeps_its_value() -> None:
    """Given a frozen LogEntry noted "acquired"
    When its note is read
    Then it is "acquired"
    """
    entry = LogEntry(note="acquired")

    note = entry.note

    assert note == "acquired"


def test_frozen_entity_rejects_mutation() -> None:
    """Given a frozen LogEntry noted "acquired"
    When its note is assigned
    Then FrozenEntityError is raised
    """
    entry = LogEntry(note="acquired")

    with pytest.raises(dc.FrozenEntityError):
        entry.note = "edited"  # pyrefly: ignore[read-only]


def test_class_access_yields_a_field_expr() -> None:
    """Given the Mineral class
    When its name attribute is read on the class
    Then it is a FieldExpr
    """
    expr = Mineral.name

    assert isinstance(expr, FieldExpr)


def test_comparing_class_access_yields_a_pred() -> None:
    """Given the Mineral class
    When Mineral.name is compared with "quartz"
    Then the result is a Pred
    """
    pred = Mineral.name == "quartz"

    assert isinstance(pred, Pred)


def test_instance_access_is_plain_slot_access() -> None:
    """Given a Mineral instance named "quartz"
    When its name is read
    Then it is "quartz"
    """
    mineral = Mineral(qid="Q1", name="quartz")

    name = mineral.name

    assert name == "quartz"


def test_unique_marker_is_harvested() -> None:
    """Given the Mineral entity
    When its field specs are read
    Then qid is unique
    And qid is not indexed
    """
    specs = _specs_by_name(Mineral)

    assert specs["qid"].unique
    assert not specs["qid"].indexed


def test_index_marker_is_harvested() -> None:
    """Given the Mineral entity
    When its field specs are read
    Then crystal_system is indexed
    And name is not indexed
    """
    specs = _specs_by_name(Mineral)

    assert specs["crystal_system"].indexed
    assert not specs["name"].indexed


def test_lazy_marker_is_harvested() -> None:
    """Given the Mineral entity
    When its field specs are read
    Then type_locality holds lazy refs
    """
    specs = _specs_by_name(Mineral)

    assert specs["type_locality"].lazy_refs


@dc.entity
class FieldNote:
    """A note with a bare and a parameterized FullText field."""

    label: str
    befund: Annotated[str, dc.FullText(language="de")] = ""
    summary: Annotated[str, dc.FullText] = ""


def test_parameterized_fulltext_keeps_its_language() -> None:
    """dc.FullText(language="de") is frozen core API before datacrystal[fts] ships (2026-06-12).

    Given a field annotated dc.FullText(language="de")
    When the field specs are read
    Then the field is fulltext
    And its language is "de"
    """
    specs = _specs_by_name(FieldNote)

    assert specs["befund"].fulltext
    assert specs["befund"].fulltext_language == "de"


def test_bare_fulltext_has_no_language() -> None:
    """Given a field annotated with the bare dc.FullText
    When the field specs are read
    Then the field is fulltext
    And its language is None
    """
    specs = _specs_by_name(FieldNote)

    assert specs["summary"].fulltext
    assert specs["summary"].fulltext_language is None


def test_unmarked_field_is_not_fulltext() -> None:
    """Given a FieldNote label without a FullText marker
    When the field specs are read
    Then the label is not fulltext
    """
    specs = _specs_by_name(FieldNote)

    assert not specs["label"].fulltext


def test_fulltext_repr_names_its_language() -> None:
    """Given dc.FullText(language="de")
    When it is rendered with repr
    Then it reads datacrystal.FullText(language='de')
    """
    rendered = repr(dc.FullText(language="de"))

    assert rendered == "datacrystal.FullText(language='de')"


def test_fulltext_rejects_an_empty_language() -> None:
    """Given the FullText marker
    When it is built with language=""
    Then TypeError asks for a language code
    """
    with pytest.raises(TypeError, match="language code"):
        dc.FullText(language="")


def test_lazy_refs_are_found_inside_unions_and_lists_but_not_in_plain_fields() -> None:
    """Given an entity with Lazy, Lazy | None, list[Lazy] and plain str, dict and int | None fields
    When its field specs are read
    Then the three Lazy-bearing fields hold lazy refs
    And the plain ones do not
    """

    @dc.entity
    class LazyShelf:
        direct: dc.Lazy[Mineral]
        optional: dc.Lazy[Mineral] | None
        listed: list[dc.Lazy[Mineral]]
        plain: str
        mapping: dict[str, int]
        maybe_int: int | None

    specs = _specs_by_name(LazyShelf)

    assert specs["direct"].lazy_refs is True
    assert specs["optional"].lazy_refs is True
    assert specs["listed"].lazy_refs is True
    assert specs["plain"].lazy_refs is False
    assert specs["mapping"].lazy_refs is False
    assert specs["maybe_int"].lazy_refs is False


def test_entity_refs_are_found_in_entity_unions_and_not_in_scalar_fields() -> None:
    """Given an entity with Mineral, Mineral | None, Any, str and int | None fields
    When its field specs are read
    Then the first three may hold an entity ref
    And str and int | None may not
    """

    @dc.entity
    class RefShelf:
        direct: Mineral
        optional: Mineral | None
        anything: Any
        label: str
        count: int | None

    specs = _specs_by_name(RefShelf)

    assert specs["direct"].entity_ref is True
    assert specs["optional"].entity_ref is True
    assert specs["anything"].entity_ref is True
    assert specs["label"].entity_ref is False
    assert specs["count"].entity_ref is False


def test_a_type_with_an_entity_ref_field_reports_entity_refs() -> None:
    """Given an entity with a Mineral | None field and a str field
    When the type info is asked for entity refs
    Then it says True
    """

    @dc.entity
    class RefHolder:
        label: str
        mineral: Mineral | None = None

    has_refs = type_info(RefHolder).has_entity_refs

    assert has_refs is True


def test_fulltext_markers_carry_flag_and_language_into_the_spec() -> None:
    """Given an entity with a FullText(language="de"), a bare FullText and an unmarked field
    When its field specs are read
    Then the marked fields are fulltext
    And only the first has the language "de"
    And the unmarked field is not fulltext and has no language
    """

    @dc.entity
    class NoteShelf:
        plain: str
        german: Annotated[str, dc.FullText(language="de")]
        bare: Annotated[str, dc.FullText]

    specs = _specs_by_name(NoteShelf)

    assert specs["german"].fulltext is True
    assert specs["german"].fulltext_language == "de"
    assert specs["bare"].fulltext is True
    assert specs["bare"].fulltext_language is None
    assert specs["plain"].fulltext is False
    assert specs["plain"].fulltext_language is None


def test_index_on_a_bare_list_is_rejected_at_decoration() -> None:
    """#19: the "must be scalar" error fires at decoration, not lazily at first commit().

    A bare `list` with no element type stays a reject after #13 added list[scalar] support.

    Given a class with a bare `list` field annotated dc.Index
    When dc.entity decorates it
    Then TypeError says the field must be scalar
    """

    class Bad:
        refs: Annotated[list, dc.Index]

    with pytest.raises(TypeError, match="must be scalar"):
        dc.entity(Bad)


@dc.entity
class Tagged:
    """An entity with multi-valued (inverted) list-of-scalar indexes (#13)."""

    tags: Annotated[list[str], dc.Index] = field(default_factory=list)
    codes: Annotated[list[int] | None, dc.Index] = None


def test_indexed_list_of_scalars_is_multivalued_and_indexed() -> None:
    """Given a list[str] field annotated dc.Index
    When the field specs are read
    Then tags is multivalued
    And tags is indexed
    """
    specs = _specs_by_name(Tagged)

    assert specs["tags"].multivalued
    assert specs["tags"].indexed


def test_optional_list_of_scalars_index_is_multivalued() -> None:
    """Given a list[int] | None field annotated dc.Index
    When the field specs are read
    Then codes is multivalued
    """
    specs = _specs_by_name(Tagged)

    assert specs["codes"].multivalued


def test_index_on_a_list_of_entity_refs_is_rejected() -> None:
    """#13: only list[scalar] is indexable, a list of entity refs is not.

    Given a class with a list[Mineral] field annotated dc.Index
    When dc.entity decorates it
    Then TypeError says the field must be scalar
    """

    class Bad:
        peers: Annotated[list[Mineral], dc.Index]

    with pytest.raises(TypeError, match="must be scalar"):
        dc.entity(Bad)


def test_unique_on_a_list_field_is_rejected() -> None:
    """#13: a multi-valued field has no single key, so Unique on a list is out.

    Given a class with a list[str] field annotated dc.Unique
    When dc.entity decorates it
    Then TypeError says the field cannot be a list
    """

    class Bad:
        tags: Annotated[list[str], dc.Unique]

    with pytest.raises(TypeError, match="cannot be a list"):
        dc.entity(Bad)


@dc.entity
class Renamed:
    """An entity whose mohs field was persisted as "hardness" (#26 a)."""

    mohs: Annotated[float | None, dc.RenamedFrom("hardness")] = None


def test_renamed_from_records_the_old_field_name() -> None:
    """Given a field annotated dc.RenamedFrom("hardness")
    When the field specs are read
    Then mohs records the old name "hardness"
    """
    specs = _specs_by_name(Renamed)

    assert specs["mohs"].renamed_from == "hardness"


def test_renamed_from_repr_names_the_old_field() -> None:
    """Given dc.RenamedFrom("hardness")
    When it is rendered with repr
    Then it reads datacrystal.RenamedFrom('hardness')
    """
    rendered = repr(dc.RenamedFrom("hardness"))

    assert rendered == "datacrystal.RenamedFrom('hardness')"


def test_renamed_from_rejects_an_empty_name() -> None:
    """Given the RenamedFrom marker
    When it is built with ""
    Then TypeError asks for a non-empty name
    """
    with pytest.raises(TypeError, match="non-empty"):
        dc.RenamedFrom("")


def test_renamed_from_on_an_indexed_field_is_rejected() -> None:
    """v0.2 renames are non-indexed-only: the index and snapshot decode paths ignore renames.

    Given a class with a field annotated dc.Index and dc.RenamedFrom("system")
    When dc.entity decorates it
    Then TypeError says RenamedFrom is not allowed on an indexed field
    """

    class Bad:
        crystal_system: Annotated[str | None, dc.Index, dc.RenamedFrom("system")] = None

    with pytest.raises(TypeError, match="RenamedFrom on an indexed"):
        dc.entity(Bad)


@dc.entity
class Derived:
    """An entity whose lat is derived from the old record's "coords" (#26 b)."""

    lat: Annotated[float, dc.Glue(_first_coordinate)] = 0.0


def test_glue_records_a_callable_deriving_from_the_old_record() -> None:
    """Given a field annotated dc.Glue that reads the first of "coords"
    When the glue is applied to the old record {"coords": "48.1,11.5"}
    Then it derives 48.1
    """
    glue = _specs_by_name(Derived)["lat"].glue
    assert glue is not None

    derived = glue({"coords": "48.1,11.5"})

    assert derived == 48.1


def test_glue_repr_hides_the_callable() -> None:
    """Given dc.Glue wrapping a function
    When it is rendered with repr
    Then it reads datacrystal.Glue(...)
    """
    rendered = repr(dc.Glue(_same_record))

    assert rendered == "datacrystal.Glue(...)"


def test_glue_rejects_a_non_callable() -> None:
    """Given the Glue marker
    When it is built with 42
    Then TypeError asks for a callable
    """
    not_callable: Any = 42

    with pytest.raises(TypeError, match="callable"):
        dc.Glue(not_callable)


def test_glue_on_an_indexed_field_is_rejected() -> None:
    """Glue is non-indexed-only, the same boundary as RenamedFrom.

    Given a class with a field annotated dc.Index and dc.Glue
    When dc.entity decorates it
    Then TypeError says Glue is not allowed on an indexed field
    """

    class Bad:
        code: Annotated[str, dc.Index, dc.Glue(_x_of)] = ""

    with pytest.raises(TypeError, match="Glue on an indexed"):
        dc.entity(Bad)


def test_glue_with_renamed_from_is_rejected() -> None:
    """The two fill-when-absent markers are mutually exclusive: pick one.

    Given a class with a field annotated dc.RenamedFrom and dc.Glue
    When dc.entity decorates it
    Then TypeError says a field cannot declare both
    """

    class Bad:
        v: Annotated[float, dc.RenamedFrom("old"), dc.Glue(_zero)] = 0.0

    with pytest.raises(TypeError, match="cannot declare both"):
        dc.entity(Bad)


def test_decorating_an_entity_twice_is_rejected() -> None:
    """Given the already decorated Mineral entity
    When dc.entity decorates it again
    Then TypeError says it is already an @entity
    """
    with pytest.raises(TypeError, match="already an @entity"):
        dc.entity(Mineral)


def test_store_rejects_non_entities(store: dc.Store) -> None:
    """Given a store
    When a plain dict is stored
    Then NotAnEntityError is raised
    """
    with pytest.raises(dc.NotAnEntityError):
        store.store({"not": "an entity"})


# --- #19: eager Index/Unique validation is forward-ref-safe ------------------
# Up references Down, which is defined just below it, so at @entity decoration of
# Up the name Down is not yet bound: an eager get_type_hints() raises NameError
# under `from __future__ import annotations`. This module importing at all is the
# load-bearing regression guard — decoration-time validation must fall back to
# the lazy path on an unresolved ref (mirrors the demo's Lazy[T] graph).


@dc.entity
class Up:
    """Refers forward to Down, which is not yet bound at decoration."""

    label: str
    down: dc.Lazy[Down] | None = None


@dc.entity
class Down:
    """Refers back to Up."""

    label: str
    up: dc.Lazy[Up] | None = None


def test_forward_ref_up_resolves_its_lazy_field() -> None:
    """Up decorated at import despite the forward ref; AC3 is held jointly with the reject tests.

    Given Up, decorated before Down was bound
    When its field specs are read
    Then down holds lazy refs
    """
    specs = _specs_by_name(Up)

    assert specs["down"].lazy_refs


def test_forward_ref_down_resolves_its_lazy_field() -> None:
    """Given Down, whose Up was bound before it
    When its field specs are read
    Then up holds lazy refs
    """
    specs = _specs_by_name(Down)

    assert specs["up"].lazy_refs


@dc.entity
class Reading:
    """Datetime and date join the indexable scalar types (#106)."""

    label: Annotated[str, dc.Unique]
    ts: Annotated[datetime, dc.SortedIndex] = datetime(2000, 1, 1)
    day: Annotated[date | None, dc.SortedIndex] = None
    seen: Annotated[datetime | None, dc.Index] = None


def test_datetime_and_date_are_accepted_as_sorted_index_keys() -> None:
    """The eval found datetime the most common sort/range key, so it and date join the type gate.

    Given a datetime and a date field annotated dc.SortedIndex
    When the class is decorated and its field specs are read
    Then ts is sorted
    And day is sorted
    """
    specs = _specs_by_name(Reading)

    assert specs["ts"].sorted
    assert specs["day"].sorted


def test_datetime_is_accepted_as_an_index_key() -> None:
    """Decoration is the gate: Index on a datetime is admitted (#106B owns ==/.in_).

    Given a datetime field annotated dc.Index
    When the class is decorated and its field specs are read
    Then seen is indexed
    """
    specs = _specs_by_name(Reading)

    assert specs["seen"].indexed
