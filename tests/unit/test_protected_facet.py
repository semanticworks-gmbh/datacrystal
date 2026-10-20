"""W2-1: the ``@entity(protected=True)`` facet (ADR-008, epic #168).

The decorator injects four lib-managed ``init=False`` columns and the
read-only ``dc_permissions`` view; everything downstream (encode, lineage,
indexes, snapshots) sees them through existing machinery. Unprotected
classes must be bit-identical to the pre-W2 decorator output — the fence
exists only where the flag is set.

The injected ``_dc_*`` columns and the ``dc_permissions`` property are
runtime-injected and invisible to Pyrefly on the user's class (the same
untypeable-by-design doctrine as the magic query syntax) — hence the line ignores.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._containers import PersistentList
from datacrystal._entity import STATE_NEW, state_of, type_info
from datacrystal._errors import FrozenEntityError
from datacrystal._state import STATE_CLEAN, STATE_DIRTY

StoreFactory = Callable[[], dc.Store]


@dc.entity(protected=True)
class Specimen:
    """A protected mineral specimen."""

    label: Annotated[str, dc.Unique]
    mass_g: float = 0.0


@dc.entity
class PlainSpecimen:
    """An unprotected mineral specimen."""

    label: Annotated[str, dc.Unique]
    mass_g: float = 0.0


@dc.entity(frozen=True, protected=True)
class SealedEvent:
    """A frozen protected event in a specimen's history."""

    seq: Annotated[int, dc.Unique]
    note: str = ""
    tags: list[str] = dataclasses.field(default_factory=list)


F = dc.fields(Specimen)

# Protected records need a non-anonymous creator since W2-2 (ADR-008 R6).
CURATOR_ANNA = dc.Principal(uid=2, memberships={7: dc.CURATOR})

INJECTED_COLUMNS = ["_dc_owner", "_dc_groups", "_dc_read_floor", "_dc_write_floor"]


def _commit_sealed_event(store_factory: StoreFactory) -> None:
    with store_factory() as store, store.acting_as(CURATOR_ANNA):
        store.store(SealedEvent(seq=1, note="acquired", tags=["field-trip"]))
        store.commit()


def _commit_specimens_with_read_floors(store: dc.Store) -> None:
    with store.acting_as(CURATOR_ANNA):
        for_viewer = Specimen(label="S0")
        store.store(for_viewer)
        for_viewer._dc_read_floor = dc.VIEWER  # pyrefly: ignore[missing-attribute]
        for_agent = Specimen(label="S1")
        store.store(for_agent)
        for_agent._dc_read_floor = dc.AGENT  # pyrefly: ignore[missing-attribute]
        for_curator = Specimen(label="S2")
        store.store(for_curator)
        for_curator._dc_read_floor = dc.CURATOR  # pyrefly: ignore[missing-attribute]
        store.commit()


# --- shape ------------------------------------------------------------------


def test_facet_injects_exactly_four_columns_after_user_fields() -> None:
    """Given a protected entity class with two user fields
    When its dataclass fields are listed
    Then the four lib-managed columns follow the user fields in order
    """
    assert [f.name for f in dataclasses.fields(Specimen)] == [
        "label", "mass_g", "_dc_owner", "_dc_groups", "_dc_read_floor", "_dc_write_floor",
    ]


@pytest.mark.parametrize("column", INJECTED_COLUMNS)
def test_injected_column_is_not_a_constructor_argument(column: str) -> None:
    """Given a protected entity class
    When an injected column's dataclass field is read
    Then it is declared init=False
    """
    assert [f.init for f in dataclasses.fields(Specimen) if f.name == column] == [False]


def test_type_info_marks_the_class_protected() -> None:
    """Given a protected entity class
    When its type info is read
    Then it is marked protected
    """
    info = type_info(Specimen)
    assert info.protected is True


def test_type_info_ends_its_field_names_with_the_four_columns() -> None:
    """Given a protected entity class
    When its type info is read
    Then its field names end with the four lib-managed columns
    """
    info = type_info(Specimen)
    assert info.field_names[-4:] == (
        "_dc_owner", "_dc_groups", "_dc_read_floor", "_dc_write_floor")


def test_constructor_takes_the_user_fields_only() -> None:
    """Given a protected entity class
    When it is constructed with its user fields
    Then the new entity is in state NEW
    """
    specimen = Specimen(label="quartz-01", mass_g=12.5)
    assert state_of(specimen) == STATE_NEW


def test_constructor_rejects_an_injected_column() -> None:
    """Given a protected entity class
    When it is constructed with the _dc_owner column
    Then construction raises TypeError
    """
    with pytest.raises(TypeError):
        Specimen(label="x", _dc_owner=7)  # pyrefly: ignore[unexpected-keyword]


def test_birth_values_are_inert() -> None:
    """Given a protected entity class
    When a specimen is constructed
    Then it has no owner, no groups and viewer floors on both sides (R6)
    """
    specimen = Specimen(label="calcite-02")
    assert specimen._dc_owner == 0  # pyrefly: ignore[missing-attribute]
    assert list(specimen._dc_groups) == []  # pyrefly: ignore[missing-attribute]
    assert specimen._dc_read_floor == dc.VIEWER  # pyrefly: ignore[missing-attribute]
    assert specimen._dc_write_floor == dc.VIEWER  # pyrefly: ignore[missing-attribute]


# --- persistence ------------------------------------------------------------


def test_store_stamps_the_acting_principal_as_owner(store: dc.Store) -> None:
    """Given a curator acting on a store
    When she stores a protected specimen
    Then its owner is her uid (W2-2)
    """
    with store.acting_as(CURATOR_ANNA):
        specimen = Specimen(label="fluorite-03", mass_g=3.3)
        store.store(specimen)
    assert specimen._dc_owner == 2  # pyrefly: ignore[missing-attribute]


def test_columns_roundtrip_through_commit_and_reopen(store_factory: StoreFactory) -> None:
    """Given a committed specimen with agent read floor, curator write floor and group 7
    When the store is closed and reopened and she reads the specimen
    Then the owner, both floors and the groups come back as committed
    """
    with store_factory() as first, first.acting_as(CURATOR_ANNA):
        specimen = Specimen(label="fluorite-03", mass_g=3.3)
        first.store(specimen)
        specimen._dc_read_floor = dc.AGENT  # pyrefly: ignore[missing-attribute]
        specimen._dc_write_floor = dc.CURATOR  # pyrefly: ignore[missing-attribute]
        specimen._dc_groups.append(7)  # pyrefly: ignore[missing-attribute]
        first.commit()
    with store_factory() as reopened, reopened.acting_as(CURATOR_ANNA):
        back = reopened.get(Specimen, label="fluorite-03")
    assert back is not None
    assert back.mass_g == 3.3
    assert back._dc_owner == 2
    assert back._dc_read_floor == dc.AGENT
    assert back._dc_write_floor == dc.CURATOR
    assert list(back._dc_groups) == [7]


def test_read_floor_range_plans_as_an_indexed_lookup(store: dc.Store) -> None:
    """Given three specimens with viewer, agent and curator read floors
    When the plan for read floor at most agent is explained
    Then the plan uses the sorted index
    """
    _commit_specimens_with_read_floors(store)
    with store.acting_as(CURATOR_ANNA):
        plan = store.explain(F._dc_read_floor <= dc.AGENT)  # pyrefly: ignore[missing-attribute]
    assert plan.indexed


def test_read_floor_range_plan_has_no_residual_scan(store: dc.Store) -> None:
    """Given three specimens with viewer, agent and curator read floors
    When the plan for read floor at most agent is explained
    Then the plan has no Python residual
    """
    _commit_specimens_with_read_floors(store)
    with store.acting_as(CURATOR_ANNA):
        plan = store.explain(F._dc_read_floor <= dc.AGENT)  # pyrefly: ignore[missing-attribute]
    assert plan.residual is None


def test_read_floor_range_query_returns_the_lower_floors(store: dc.Store) -> None:
    """Given three specimens with viewer, agent and curator read floors, all owned by her
    When she queries read floor at most agent
    Then she gets the viewer and agent specimens
    """
    _commit_specimens_with_read_floors(store)
    with store.acting_as(CURATOR_ANNA):
        found = store.query(F._dc_read_floor <= dc.AGENT)  # pyrefly: ignore[missing-attribute]
    assert {specimen.label for specimen in found} == {"S0", "S1"}


def test_snapshot_read_floor_range_query_returns_the_lower_floors(store: dc.Store) -> None:
    """Given three specimens with viewer, agent and curator read floors, all owned by her
    When a snapshot bound to her queries read floor at most agent
    Then it returns the viewer and agent specimens
    """
    _commit_specimens_with_read_floors(store)
    snapshot = store.snapshot(principal=CURATOR_ANNA)
    found = snapshot.query(F._dc_read_floor <= dc.AGENT)  # pyrefly: ignore[missing-attribute]
    assert {specimen.label for specimen in found} == {"S0", "S1"}


# --- the dc_permissions view --------------------------------------------------


def test_dc_permissions_packages_the_columns() -> None:
    """Given a freshly constructed protected specimen
    When its dc_permissions view is read
    Then it equals the inert birth permissions
    """
    specimen = Specimen(label="pyrite-04")
    view = specimen.dc_permissions  # pyrefly: ignore[missing-attribute]
    assert view == dc.Permissions(
        owner=0, groups=(), read_floor=dc.VIEWER, write_floor=dc.VIEWER
    )


def test_dc_permissions_view_is_frozen() -> None:
    """Given a specimen's dc_permissions view
    When its owner is assigned
    Then the assignment raises FrozenInstanceError
    """
    specimen = Specimen(label="pyrite-04")
    view = specimen.dc_permissions  # pyrefly: ignore[missing-attribute]
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.owner = 9  # pyrefly: ignore[read-only]  # frozen-by-design probe


def test_dc_permissions_view_taken_earlier_ignores_later_group_changes() -> None:
    """Given a dc_permissions view taken before any group was added
    When group 7 is appended to the specimen
    Then the earlier view still has no groups
    """
    specimen = Specimen(label="topaz-05")
    view = specimen.dc_permissions  # pyrefly: ignore[missing-attribute]
    specimen._dc_groups.append(7)  # pyrefly: ignore[missing-attribute]
    assert view.groups == ()


def test_fresh_dc_permissions_view_holds_the_groups_as_a_tuple() -> None:
    """Given a specimen with group 7 appended
    When a fresh dc_permissions view is read
    Then its groups are the tuple (7,)
    """
    specimen = Specimen(label="topaz-05")
    specimen._dc_groups.append(7)  # pyrefly: ignore[missing-attribute]
    fresh_view = specimen.dc_permissions  # pyrefly: ignore[missing-attribute]
    assert fresh_view.groups == (7,)
    assert isinstance(fresh_view.groups, tuple)


def test_dc_permissions_rejects_non_permissions_assignment() -> None:
    """Given a protected specimen
    When a plain dict is assigned to dc_permissions
    Then assignment raises TypeError naming dc.Permissions
    """
    specimen = Specimen(label="beryl-06")
    with pytest.raises(TypeError, match=r"dc\.Permissions"):
        specimen.dc_permissions = {"owner": 9}  # pyrefly: ignore[missing-attribute]


# --- the reserved-name guard ---------------------------------------------------


def test_user_dc_prefixed_field_raises_at_plain_decoration() -> None:
    """Given a class declaring its own _dc_owner field
    When it is decorated as a plain entity
    Then decoration raises TypeError naming the reserved prefix
    """
    with pytest.raises(TypeError, match="reserved"):

        @dc.entity(protected=False)
        class Bad:
            _dc_owner: int = 0


def test_user_dc_prefixed_field_raises_at_protected_decoration() -> None:
    """Given a class declaring its own _dc_owner field
    When it is decorated as a protected entity
    Then decoration raises TypeError naming the reserved prefix
    """
    with pytest.raises(TypeError, match="reserved"):

        @dc.entity(protected=True)
        class Bad:
            _dc_owner: int = 0


def test_user_dc_permissions_annotation_raises() -> None:
    """Given a class annotating its own dc_permissions field
    When it is decorated as an entity
    Then decoration raises TypeError naming the reserved name
    """
    with pytest.raises(TypeError, match="reserved"):

        @dc.entity
        class Bad:
            dc_permissions: str = ""


def test_user_dc_permissions_method_raises() -> None:
    """Given a protected class defining its own dc_permissions method
    When it is decorated as an entity
    Then decoration raises TypeError naming the reserved name
    """
    with pytest.raises(TypeError, match="reserved"):

        @dc.entity(protected=True)
        class Bad:
            x: int = 0

            def dc_permissions(self) -> None:
                return None


# --- the container backref coexists ----------------------------------------


def test_injected_groups_column_is_a_persistent_list(store: dc.Store) -> None:
    """Given a committed protected specimen
    When its groups column is read
    Then it is a PersistentList
    """
    with store.acting_as(CURATOR_ANNA):
        specimen = Specimen(label="galena-07")
        store.store(specimen)
        store.commit()
    groups = specimen._dc_groups  # pyrefly: ignore[missing-attribute]
    assert isinstance(groups, PersistentList)


def test_container_backref_points_at_the_owning_entity(store: dc.Store) -> None:
    """Given a committed protected specimen
    When its groups list's _dc_owner is read
    Then it is the specimen itself
    """
    with store.acting_as(CURATOR_ANNA):
        specimen = Specimen(label="galena-07")
        store.store(specimen)
        store.commit()
    groups = specimen._dc_groups  # pyrefly: ignore[missing-attribute]
    assert groups._dc_owner is specimen


def test_appending_to_the_groups_list_dirty_marks_the_owning_entity(store: dc.Store) -> None:
    """Given a committed protected specimen in state CLEAN
    When group 3 is appended in place
    Then the specimen is DIRTY
    """
    with store.acting_as(CURATOR_ANNA):
        specimen = Specimen(label="galena-07")
        store.store(specimen)
        store.commit()
    state_before = state_of(specimen)
    specimen._dc_groups.append(3)  # pyrefly: ignore[missing-attribute]
    assert state_before == STATE_CLEAN
    assert state_of(specimen) == STATE_DIRTY


# --- zero cost for unprotected classes ----------------------------------------


def test_unprotected_class_declares_only_its_user_fields() -> None:
    """Given an unprotected entity class with two user fields
    When its dataclass fields are listed
    Then only those two are there
    """
    assert [f.name for f in dataclasses.fields(PlainSpecimen)] == ["label", "mass_g"]


def test_unprotected_class_type_info_is_not_protected() -> None:
    """Given an unprotected entity class
    When its type info is read
    Then it is not marked protected
    """
    info = type_info(PlainSpecimen)
    assert info.protected is False


def test_unprotected_class_type_info_lists_no_injected_column() -> None:
    """Given an unprotected entity class
    When its type info field names are read
    Then none starts with _dc_
    """
    info = type_info(PlainSpecimen)
    assert not any(name.startswith("_dc_") for name in info.field_names)


def test_unprotected_class_fieldset_lists_no_injected_column() -> None:
    """Given an unprotected entity class
    When its __dc_fieldset__ is read
    Then none starts with _dc_
    """
    fieldset = type.__getattribute__(PlainSpecimen, "__dc_fieldset__")
    assert not any(name.startswith("_dc_") for name in fieldset)


def test_unprotected_class_has_no_dc_permissions_view() -> None:
    """Given an unprotected entity class
    When the class is probed for dc_permissions
    Then it has none
    """
    assert not hasattr(PlainSpecimen, "dc_permissions")


def test_unprotected_instance_has_no_injected_column() -> None:
    """Given an unprotected specimen
    When it is probed for _dc_owner
    Then it has none
    """
    plain = PlainSpecimen(label="plain-08")
    assert not hasattr(plain, "_dc_owner")


def test_unprotected_instance_has_no_dc_permissions_view() -> None:
    """Given an unprotected specimen
    When it is probed for dc_permissions
    Then it has none
    """
    plain = PlainSpecimen(label="plain-08")
    assert not hasattr(plain, "dc_permissions")


# --- frozen x protected ---------------------------------------------------------


def test_frozen_protected_event_reads_back_with_the_stamped_owner(
    store_factory: StoreFactory,
) -> None:
    """Given a committed frozen protected event stored by her
    When the store is reopened and she reads the event
    Then its owner is her uid, stamped past the frozen setattr
    """
    _commit_sealed_event(store_factory)
    with store_factory() as reopened, reopened.acting_as(CURATOR_ANNA):
        back = reopened.get(SealedEvent, seq=1)
    assert back is not None
    assert back.dc_permissions.owner == 2  # pyrefly: ignore[missing-attribute]


def test_frozen_protected_event_reads_back_with_a_viewer_write_floor(
    store_factory: StoreFactory,
) -> None:
    """Given a committed frozen protected event stored by her
    When the store is reopened and she reads the event
    Then its write floor is viewer
    """
    _commit_sealed_event(store_factory)
    with store_factory() as reopened, reopened.acting_as(CURATOR_ANNA):
        back = reopened.get(SealedEvent, seq=1)
    assert back is not None
    assert back.dc_permissions.write_floor == dc.VIEWER  # pyrefly: ignore[missing-attribute]


def test_frozen_protected_event_refuses_a_field_assignment(
    store_factory: StoreFactory,
) -> None:
    """Given a committed frozen protected event read back by her
    When its note is assigned
    Then the assignment raises FrozenEntityError
    """
    _commit_sealed_event(store_factory)
    with store_factory() as reopened, reopened.acting_as(CURATOR_ANNA):
        back = reopened.query(SealedEvent)[0]
    with pytest.raises(FrozenEntityError):
        back.note = "edited"


def test_frozen_protected_event_refuses_an_append_to_its_tags(
    store_factory: StoreFactory,
) -> None:
    """Given a committed frozen protected event read back by her
    When a tag is appended to its tags list
    Then the append raises FrozenEntityError, the P3-wrapped frozen container
    """
    _commit_sealed_event(store_factory)
    with store_factory() as reopened, reopened.acting_as(CURATOR_ANNA):
        back = reopened.query(SealedEvent)[0]
    with pytest.raises(FrozenEntityError):
        back.tags.append("x")


def test_frozen_protected_event_refuses_an_append_to_its_groups(
    store_factory: StoreFactory,
) -> None:
    """Given a committed frozen protected event read back by her
    When a group is appended to its injected groups list
    Then the append raises FrozenEntityError, the injected container being fenced too
    """
    _commit_sealed_event(store_factory)
    with store_factory() as reopened, reopened.acting_as(CURATOR_ANNA):
        back = reopened.query(SealedEvent)[0]
    with pytest.raises(FrozenEntityError):
        back._dc_groups.append(3)
