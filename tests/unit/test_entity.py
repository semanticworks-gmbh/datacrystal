"""@entity mechanics: slots, identity, markers, frozen mode, field exprs."""

from __future__ import annotations

import weakref
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Annotated, Any

import pytest

import datacrystal as dc
from datacrystal._conditions import FieldExpr, Pred
from tests.conftest import Locality, LogEntry, Mineral

StoreFactory = Callable[[], dc.Store]


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


def test_unique_marker_rejects_a_second_record_with_the_same_key(store: dc.Store) -> None:
    """Given a committed mineral with qid Q1
    When a second mineral with qid Q1 is committed
    Then UniqueViolationError is raised
    """
    store.store(Mineral(qid="Q1", name="quartz"))
    store.commit()
    store.store(Mineral(qid="Q1", name="duplicate"))

    with pytest.raises(dc.UniqueViolationError):
        store.commit()


def test_unique_marker_finds_the_record_by_its_key(store: dc.Store) -> None:
    """Given a committed mineral with qid Q1
    When the store gets a mineral by qid Q1
    Then it is the quartz
    """
    store.store(Mineral(qid="Q1", name="quartz"))
    store.commit()

    found = store.get(Mineral, qid="Q1")

    assert found is not None
    assert found.name == "quartz"


def test_index_marker_makes_the_field_answerable_from_the_index(store: dc.Store) -> None:
    """Given a committed mineral
    When the plan for crystal_system == "trigonal" is explained
    Then the plan is indexed
    """
    store.store(Mineral(qid="Q1", name="quartz", crystal_system="trigonal"))
    store.commit()

    query_plan = store.explain(dc.fields(Mineral).crystal_system == "trigonal")

    assert query_plan.indexed


def test_unmarked_field_is_answered_by_a_scan(store: dc.Store) -> None:
    """Given a committed mineral
    When the plan for name == "quartz" is explained
    Then the plan is not indexed
    """
    store.store(Mineral(qid="Q1", name="quartz", crystal_system="trigonal"))
    store.commit()

    query_plan = store.explain(dc.fields(Mineral).name == "quartz")

    assert not query_plan.indexed


def test_lazy_marker_leaves_the_handle_unloaded_after_reopen(store_factory: StoreFactory) -> None:
    """Given a committed mineral with a Lazy type locality, reopened
    When the mineral is read
    Then its type locality handle is not loaded
    """
    first = store_factory()
    first.store(Mineral(qid="Q1", name="azurite", type_locality=dc.Lazy.of(Locality(qid="L1", name="Tsumeb"))))
    first.commit()
    first.close()
    reopened = store_factory()

    azurite = reopened.get(Mineral, qid="Q1")

    assert azurite is not None
    assert azurite.type_locality is not None
    assert not azurite.type_locality.loaded
    reopened.close()


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


def test_lazy_handles_stay_unloaded_after_reopen_in_unions_and_lists(
    store_factory: StoreFactory,
) -> None:
    """Given an entity with Lazy, Lazy | None and list[Lazy] fields, committed and reopened
    When the shelf is read
    Then the direct handle is not loaded
    And the optional handle is not loaded
    And the listed handle is not loaded
    """

    @dc.entity
    class LazyShelf:
        direct: dc.Lazy[Mineral]
        optional: dc.Lazy[Mineral] | None
        listed: list[dc.Lazy[Mineral]]

    first = store_factory()
    first.store(
        LazyShelf(
            direct=dc.Lazy.of(Mineral(qid="Q1", name="quartz")),
            optional=dc.Lazy.of(Mineral(qid="Q2", name="azurite")),
            listed=[dc.Lazy.of(Mineral(qid="Q3", name="beryl"))],
        )
    )
    first.commit()
    first.close()
    reopened = store_factory()

    [shelf] = reopened.query(LazyShelf)

    assert not shelf.direct.loaded
    assert shelf.optional is not None
    assert not shelf.optional.loaded
    assert not shelf.listed[0].loaded
    reopened.close()


def test_entity_refs_are_discovered_in_entity_unions_and_any_fields(store: dc.Store) -> None:
    """Given an entity with Mineral, Mineral | None and Any fields each holding a new mineral
    When the shelf is stored and committed
    Then all three minerals are in the store
    """

    @dc.entity
    class RefShelf:
        direct: Mineral
        optional: Mineral | None
        anything: Any

    store.store(
        RefShelf(
            direct=Mineral(qid="Q1", name="quartz"),
            optional=Mineral(qid="Q2", name="azurite"),
            anything=Mineral(qid="Q3", name="beryl"),
        )
    )
    store.commit()

    counted = store.count(Mineral)

    assert counted == 3


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


def test_forward_ref_up_resolves_its_lazy_field(store_factory: StoreFactory) -> None:
    """Up decorated at import despite the forward ref; AC3 is held jointly with the reject tests.

    Given Up, decorated before Down was bound, committed with a Lazy down and reopened
    When the up is read
    Then its down handle is not loaded
    """
    first = store_factory()
    first.store(Up(label="u", down=dc.Lazy.of(Down(label="d"))))
    first.commit()
    first.close()
    reopened = store_factory()

    up = reopened.query(Up)[0]

    assert up.down is not None
    assert not up.down.loaded
    reopened.close()


def test_forward_ref_down_resolves_its_lazy_field(store_factory: StoreFactory) -> None:
    """Given Down, whose Up was bound before it, committed with a Lazy up and reopened
    When the down is read
    Then its up handle is not loaded
    """
    first = store_factory()
    first.store(Down(label="d", up=dc.Lazy.of(Up(label="u"))))
    first.commit()
    first.close()
    reopened = store_factory()

    down = reopened.query(Down)[0]

    assert down.up is not None
    assert not down.up.loaded
    reopened.close()


@dc.entity
class Reading:
    """Datetime and date join the indexable scalar types (#106)."""

    label: Annotated[str, dc.Unique]
    ts: Annotated[datetime, dc.SortedIndex] = datetime(2000, 1, 1)
    day: Annotated[date | None, dc.SortedIndex] = None
    seen: Annotated[datetime | None, dc.Index] = None


def test_datetime_and_date_are_accepted_as_sorted_index_keys(store: dc.Store) -> None:
    """The eval found datetime the most common sort/range key, so it and date join the type gate.

    Given a committed reading with a datetime and a date field annotated dc.SortedIndex
    When the plans for a datetime and a date range are explained
    Then the datetime range is indexed
    And the date range is indexed
    """
    store.store(Reading(label="r1", ts=datetime(2024, 5, 1), day=date(2024, 5, 1)))
    store.commit()
    fields = dc.fields(Reading)

    ts_plan = store.explain(fields.ts >= datetime(2024, 1, 1))
    day_plan = store.explain(fields.day >= date(2024, 1, 1))

    assert ts_plan.indexed
    assert day_plan.indexed


def test_datetime_is_accepted_as_an_index_key(store: dc.Store) -> None:
    """Decoration is the gate: Index on a datetime is admitted (#106B owns ==/.in_).

    Given a committed reading with a datetime field annotated dc.Index
    When the plan for seen == that datetime is explained
    Then the plan is indexed
    """
    store.store(Reading(label="r1", seen=datetime(2024, 5, 1)))
    store.commit()

    seen_plan = store.explain(dc.fields(Reading).seen == datetime(2024, 5, 1))

    assert seen_plan.indexed
