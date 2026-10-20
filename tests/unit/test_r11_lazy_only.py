"""R11 (ADR-008): protected classes are Lazy-referable only — epic #168 W3-4.

Two enforcement points make deref the ONE read checkpoint (mask-on-deref,
R14, depends on this): prereq A is the decorator-time class-level rule
(``_entity._check_r11_lazy_only``, fires when a field's type hint holds a
DIRECT — non-Lazy — reference to a ``protected=True`` class); prereq B is
its runtime complement for positions the type system can't see (``Any``
fields, bare containers, ``store.root =``), enforced at the write boundary
in ``Store._walk_value``. This file proves the MECHANISM only — the full
multi-principal read-fence matrix is a later story (W3-5).
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

import pytest

import datacrystal as dc

OUTSIDER = dc.Principal(uid=999, memberships={})

CURATOR_ANNA = dc.Principal(uid=2, memberships={7: dc.CURATOR})


@dc.entity(protected=True)
class Secret:
    """A protected entity: referable only through dc.Lazy."""

    name: Annotated[str, dc.Unique]


@dc.entity
class Untyped:
    """An unprotected holder with an Any-typed field, which the type system cannot police."""

    label: Annotated[str, dc.Unique]
    payload: Any = None


@dc.entity
class BareListHolder:
    """An unprotected holder with a bare `list` field, an eager position."""

    label: Annotated[str, dc.Unique]
    items: list = dataclasses.field(default_factory=list)


@dc.entity
class Wrapper:
    """An unprotected holder with the one legal form, a typed dc.Lazy[Secret] field."""

    label: Annotated[str, dc.Unique]
    secret: dc.Lazy[Secret] | None = None


# --- prereq A: decorator-time (class-level) ----------------------------------


def test_decorator_rejects_a_direct_eager_scalar_ref_on_a_protected_holder() -> None:
    """Given a protected class with a plain `Secret | None` field
    When dc.entity decorates it
    Then TypeError says protected classes are Lazy-referable only
    """
    with pytest.raises(TypeError, match="Lazy-referable only"):

        @dc.entity(protected=True)
        class Holder:
            label: Annotated[str, dc.Unique]
            secret: Secret | None = None


def test_decorator_rejects_a_bare_eager_list() -> None:
    """Given a protected class with a plain `list[Secret]` field
    When dc.entity decorates it
    Then TypeError says protected classes are Lazy-referable only
    """
    with pytest.raises(TypeError, match="Lazy-referable only"):

        @dc.entity(protected=True)
        class Holder2:
            label: Annotated[str, dc.Unique]
            secrets: list[Secret] = dataclasses.field(default_factory=list)


def test_decorator_rejects_an_eager_ref_on_an_unprotected_holder_too() -> None:
    """R11 is class-level on the TARGET, so an unprotected holder gets no exemption (ADR-008).

    Given an unprotected class with a plain `Secret | None` field
    When dc.entity decorates it
    Then TypeError says protected classes are Lazy-referable only
    """
    with pytest.raises(TypeError, match="Lazy-referable only"):

        @dc.entity
        class PlainHolder:
            label: Annotated[str, dc.Unique]
            secret: Secret | None = None


def test_error_names_the_scalar_lazy_form() -> None:
    """Given an unprotected class with a plain `Secret | None` field
    When dc.entity decorates it
    Then the TypeError names dc.Lazy[Secret]
    """
    with pytest.raises(TypeError) as excinfo:

        @dc.entity
        class Holder3:
            label: Annotated[str, dc.Unique]
            secret: Secret | None = None

    assert "dc.Lazy[Secret]" in str(excinfo.value)


def test_error_names_the_list_lazy_form() -> None:
    """Given an unprotected class with a plain `Secret | None` field
    When dc.entity decorates it
    Then the TypeError names list[dc.Lazy[Secret]]
    """
    with pytest.raises(TypeError) as excinfo:

        @dc.entity
        class Holder4:
            label: Annotated[str, dc.Unique]
            secret: Secret | None = None

    assert "list[dc.Lazy[Secret]]" in str(excinfo.value)


def test_lazy_scalar_and_list_forms_leave_a_usable_class() -> None:
    """The ratified legal shapes never raise at decoration time.

    Given a protected class with `dc.Lazy[Secret] | None` and `list[dc.Lazy[Secret]]` fields
    When it is instantiated
    Then its lazy scalar is None
    And its lazy list is empty
    """

    @dc.entity(protected=True)
    class HolderOK:
        label: Annotated[str, dc.Unique]
        one: dc.Lazy[Secret] | None = None
        many: list[dc.Lazy[Secret]] = dataclasses.field(default_factory=list)

    holder = HolderOK(label="ok")

    assert holder.one is None
    assert holder.many == []


def test_eager_ref_to_an_unprotected_class_is_legal() -> None:
    """Given an unprotected class `Open` and a holder with an eager `Open | None` field
    When the holder is instantiated
    Then its field is None
    """

    @dc.entity
    class Open:
        label: Annotated[str, dc.Unique]

    @dc.entity
    class HolderOpen:
        label: Annotated[str, dc.Unique]
        thing: Open | None = None

    holder = HolderOpen(label="h")

    assert holder.thing is None


# --- prereq B: the runtime complement for untyped positions ------------------


def test_runtime_rejects_a_protected_entity_through_an_any_field(store: dc.Store) -> None:
    """Given an Any-typed payload holding a protected Secret
    When the holder is stored
    Then TypeError says protected classes are Lazy-referable only
    """
    holder = Untyped(label="x")
    holder.payload = Secret(name="smuggled")

    with pytest.raises(TypeError, match="Lazy-referable only"):
        store.store(holder)


def test_runtime_rejects_a_protected_entity_assigned_to_store_root(store: dc.Store) -> None:
    """Given a store
    When a protected Secret is assigned to store.root
    Then TypeError says protected classes are Lazy-referable only
    """
    with pytest.raises(TypeError, match="Lazy-referable only"):
        store.root = Secret(name="root-secret")


def test_runtime_rejects_lazy_wrapped_protected_in_an_any_field(store: dc.Store) -> None:
    """Wrapping in dc.Lazy.of(...) does not make an Any field safe.

    Regression for the Fable read-fence review (2026-07-15): `swizzle` erases the wrapper at
    persistence and an Any field re-decodes eagerly, so the real Secret would hydrate into a
    shared parent for every principal. The FIELD, not the value, has to be typed dc.Lazy[...].

    Given a curator and an Any-typed payload holding dc.Lazy.of(Secret)
    When the holder is stored
    Then TypeError says protected classes are Lazy-referable only
    """
    with store.acting_as(CURATOR_ANNA):
        holder = Untyped(label="y")
        holder.payload = dc.Lazy.of(Secret(name="wrapped"))

        with pytest.raises(TypeError, match="Lazy-referable only"):
            store.store(holder)


def test_runtime_rejects_lazy_wrapped_protected_in_a_bare_list(store: dc.Store) -> None:
    """A bare `list` field is an eager position too (Fable leak_breadth.py).

    Given a curator and a bare list field holding dc.Lazy.of(Secret)
    When the holder is stored
    Then TypeError says protected classes are Lazy-referable only
    """
    with store.acting_as(CURATOR_ANNA):
        holder = BareListHolder(label="b")
        holder.items.append(dc.Lazy.of(Secret(name="in-bare-list")))

        with pytest.raises(TypeError, match="Lazy-referable only"):
            store.store(holder)


def test_runtime_rejects_lazy_wrapped_protected_as_store_root(store: dc.Store) -> None:
    """store.root is an eager position too (Fable leak_breadth.py).

    Given a curator acting on a store
    When dc.Lazy.of(Secret) is assigned to store.root
    Then TypeError says protected classes are Lazy-referable only
    """
    with store.acting_as(CURATOR_ANNA), pytest.raises(TypeError, match="Lazy-referable only"):
        store.root = dc.Lazy.of(Secret(name="root-lazy"))


@pytest.fixture
def stored_secret(store: dc.Store) -> None:
    """Commit a Wrapper whose typed Lazy[Secret] field holds a Secret, stored by the curator."""
    with store.acting_as(CURATOR_ANNA):
        wrapper = Wrapper(label="w", secret=dc.Lazy.of(Secret(name="top-secret")))
        store.store(wrapper)
        store.commit()


def test_typed_lazy_field_commits_without_raising(store: dc.Store) -> None:
    """The typed dc.Lazy[Secret] field is the legal form, the positive control.

    Given a curator and a Wrapper with a typed dc.Lazy[Secret] field
    When the store commits
    Then a TID is returned
    """
    with store.acting_as(CURATOR_ANNA):
        store.store(Wrapper(label="w", secret=dc.Lazy.of(Secret(name="top-secret"))))

        tid = store.commit()

    assert tid is not None


@pytest.mark.usefixtures("stored_secret")
def test_outsider_derefs_a_redacted_twin_not_the_real_secret(store: dc.Store) -> None:
    """The typed Lazy field enforces the read fence end to end.

    Given a Wrapper holding a Secret, committed by a curator
    When an outsider fetches the Wrapper and derefs its secret
    Then the target is a dc.Redacted
    And it still passes isinstance Secret
    """
    with store.acting_as(OUTSIDER):
        got = store.get(Wrapper, label="w")
        assert got is not None
        assert got.secret is not None

        target = got.secret.get()

        assert isinstance(target, dc.Redacted)
        assert isinstance(target, Secret)


@pytest.mark.usefixtures("stored_secret")
def test_reading_a_field_of_the_redacted_twin_is_loud(store: dc.Store) -> None:
    """Given a Wrapper holding a Secret, committed by a curator
    When an outsider reads the name of the redacted twin
    Then ReadDeniedError is raised
    """
    with store.acting_as(OUTSIDER):
        got = store.get(Wrapper, label="w")
        assert got is not None
        assert got.secret is not None
        target = got.secret.get()

        with pytest.raises(dc.ReadDeniedError):
            _ = target.name
