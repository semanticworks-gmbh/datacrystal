"""dc.Redacted (ADR-008 R14 variant (a)) — the redacted-twin deref checkpoint,
epic #168 W3-4.

``Store.get_many`` of an OID is the single CHECKED deref, judging COMMITTED
labels (D1): the real instance (readable) | a ``dc.Redacted`` twin (exists,
denied) | ``DanglingRefError`` (no record at all). ``Lazy.get()`` rides this
checkpoint and never caches a protected target on the engine handle — the
loaded-Lazy leak this story closes (landmine 2: without it, a curator's
earlier load would hand the real instance to every later principal that
derefs the same handle). This file proves the MECHANISM with a hand-built
protected corpus; the full multi-principal × surface matrix is W3-5.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import pytest

import datacrystal as dc

StoreFactory = Callable[[], dc.Store]

TEAM = 5
OWNER = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
OUTSIDER = dc.Principal(uid=9, memberships={TEAM: dc.CURATOR})
ROOT = dc.Principal(uid=99, memberships={dc.WORLD: dc.EXECUTIVE})


@dc.entity(protected=True)
class Dossier:
    """A protected dossier that may link to another."""

    label: Annotated[str, dc.Unique]
    note: str = ""
    linked: dc.Lazy[Dossier] | None = None


@dc.entity
class Plain:
    """An unprotected record."""

    label: Annotated[str, dc.Unique]


@dc.entity
class PlainHolder:
    """An unprotected holder of a lazy handle onto a plain record."""

    label: Annotated[str, dc.Unique]
    linked: dc.Lazy[Plain] | None = None


@dc.entity
class Vault:
    """Unprotected parent holding a protected child through a USER ``Lazy.of`` handle.

    The cross-principal shared-graph shape the Fable review exploited: the handle keeps its
    cached ``_obj`` and is reachable by any principal that reaches the live parent.
    """

    label: Annotated[str, dc.Unique]
    secret: dc.Lazy[Dossier] | None = None


def _owner_only(store: dc.Store, label: str) -> Dossier:
    """Commit a birth-default record (owner-only, no groups), inert to all but owner and root."""
    with store.acting_as(OWNER):
        dossier = Dossier(label=label)
        store.store(dossier)
        store.commit()
    return dossier


def _commit_linked_pair(
    store_factory: StoreFactory, parent_label: str, target_label: str
) -> None:
    """Commit a parent dossier linking to a target dossier, then close the store."""
    store = store_factory()
    with store.acting_as(OWNER):
        target = Dossier(label=target_label)
        store.store(target)
        parent = Dossier(label=parent_label)
        parent.linked = dc.Lazy.of(target)
        store.store(parent)
        store.commit()
    store.close()


def _commit_readable_hidden_and_deleted(store_factory: StoreFactory) -> tuple[int, int, int]:
    """Commit a readable, a hidden and a deleted dossier; close the store and return their OIDs."""
    store = store_factory()
    with store.acting_as(OWNER):
        readable = Dossier(label="readable")
        readable_oid = store.store(readable)
        dc.share(readable, TEAM, read=dc.VIEWER, write=dc.VIEWER)
        hidden_oid = store.store(Dossier(label="hidden"))
        doomed = Dossier(label="doomed")
        doomed_oid = store.store(doomed)
        store.commit()
    with store.acting_as(OWNER):
        store.delete(doomed)
        store.commit()
    store.close()
    return readable_oid, hidden_oid, doomed_oid


@pytest.fixture
def twin(store: dc.Store) -> Dossier:
    """Return the redacted twin an outsider gets for an owner-only dossier."""
    dossier = _owner_only(store, "net")
    dossier_oid = store.store(dossier)
    with store.acting_as(OUTSIDER):
        return store.get_many([dossier_oid])[0]


# --- twin shape (R14 variant (a)) --------------------------------------------


class TestTwinShape:
    """What a redacted twin is and what it refuses."""

    def test_twin_is_an_instance_of_the_protected_class(self, store: dc.Store) -> None:
        """Given an owner-only dossier
        When an outsider derefs its OID
        Then the result is an instance of Dossier
        """
        dossier = _owner_only(store, "shape-1")
        dossier_oid = store.store(dossier)
        with store.acting_as(OUTSIDER):
            twin = store.get_many([dossier_oid])[0]
        assert isinstance(twin, Dossier)

    def test_twin_is_an_instance_of_redacted(self, store: dc.Store) -> None:
        """Given an owner-only dossier
        When an outsider derefs its OID
        Then the result is an instance of dc.Redacted
        """
        dossier = _owner_only(store, "shape-1")
        dossier_oid = store.store(dossier)
        with store.acting_as(OUTSIDER):
            twin = store.get_many([dossier_oid])[0]
        assert isinstance(twin, dc.Redacted)

    def test_twin_is_falsy(self, store: dc.Store) -> None:
        """Given an owner-only dossier
        When an outsider derefs its OID
        Then the result is falsy
        """
        dossier = _owner_only(store, "shape-1")
        dossier_oid = store.store(dossier)
        with store.acting_as(OUTSIDER):
            twin = store.get_many([dossier_oid])[0]
        assert bool(twin) is False

    def test_twin_keeps_the_typename_readable(self, store: dc.Store) -> None:
        """Given an owner-only dossier
        When an outsider derefs its OID
        Then the twin's typename is Dossier's
        """
        dossier = _owner_only(store, "shape-1")
        dossier_oid = store.store(dossier)
        with store.acting_as(OUTSIDER):
            twin = store.get_many([dossier_oid])[0]
        assert twin.typename == "tests.unit.test_redacted_twin:Dossier"

    def test_reading_a_data_field_raises_read_denied(self, store: dc.Store) -> None:
        """Given the twin of an owner-only dossier
        When its note is read
        Then the read raises ReadDeniedError
        """
        dossier = _owner_only(store, "shape-2")
        dossier_oid = store.store(dossier)
        with store.acting_as(OUTSIDER):
            twin = store.get_many([dossier_oid])[0]
        with pytest.raises(dc.ReadDeniedError):
            _ = twin.note

    def test_reading_the_unique_key_raises_read_denied(self, store: dc.Store) -> None:
        """Given the twin of an owner-only dossier
        When its label is read
        Then the read raises ReadDeniedError
        """
        dossier = _owner_only(store, "shape-2")
        dossier_oid = store.store(dossier)
        with store.acting_as(OUTSIDER):
            twin = store.get_many([dossier_oid])[0]
        with pytest.raises(dc.ReadDeniedError):
            _ = twin.label

    def test_reading_dc_permissions_raises_read_denied(self, store: dc.Store) -> None:
        """Given the twin of an owner-only dossier
        When its dc_permissions are read
        Then the read raises ReadDeniedError, the labels of an unreadable record being a leak
        """
        dossier = _owner_only(store, "shape-3")
        dossier_oid = store.store(dossier)
        with store.acting_as(OUTSIDER):
            twin = store.get_many([dossier_oid])[0]
        with pytest.raises(dc.ReadDeniedError):
            _ = twin.dc_permissions

    def test_repr_never_raises(self, store: dc.Store) -> None:
        """Given the twin of an owner-only dossier
        When it is rendered with repr
        Then the text names Redacted
        """
        dossier = _owner_only(store, "shape-4")
        dossier_oid = store.store(dossier)
        with store.acting_as(OUTSIDER):
            twin = store.get_many([dossier_oid])[0]
        assert "Redacted" in repr(twin)


# --- the never-committable net -----------------------------------------------


class TestNeverCommittable:
    """A twin can be neither stored, marked, deleted, upserted nor shared."""

    def test_storing_a_twin_raises_read_denied(self, store: dc.Store, twin: Dossier) -> None:
        """Given the twin of an owner-only dossier
        When it is stored
        Then store raises ReadDeniedError
        """
        with pytest.raises(dc.ReadDeniedError):
            store.store(twin)

    def test_marking_a_twin_dirty_raises_read_denied(self, store: dc.Store, twin: Dossier) -> None:
        """Given the twin of an owner-only dossier
        When it is marked dirty
        Then mark_dirty raises ReadDeniedError
        """
        with pytest.raises(dc.ReadDeniedError):
            store.mark_dirty(twin)

    def test_deleting_a_twin_raises_read_denied(self, store: dc.Store, twin: Dossier) -> None:
        """Given the twin of an owner-only dossier
        When it is deleted
        Then delete raises ReadDeniedError
        """
        with pytest.raises(dc.ReadDeniedError):
            store.delete(twin)

    def test_upserting_a_twin_raises_read_denied(self, store: dc.Store, twin: Dossier) -> None:
        """Given the twin of an owner-only dossier
        When it is upserted
        Then upsert raises ReadDeniedError
        """
        with pytest.raises(dc.ReadDeniedError):
            store.upsert(twin)

    def test_share_raises_via_the_twins_own_setattr(self, twin: Dossier) -> None:
        """Given the twin of an owner-only dossier
        When it is shared with the team
        Then share raises ReadDeniedError, the twin's __setattr__ always raising
        """
        with pytest.raises(dc.ReadDeniedError):
            dc.share(twin, TEAM, read=dc.VIEWER, write=dc.VIEWER)

    def test_twin_smuggled_into_a_field_is_caught_at_p1_discovery(
        self, store: dc.Store, twin: Dossier
    ) -> None:
        """Given the twin of an owner-only dossier wrapped in Lazy.of on a new holder
        When the holder is stored
        Then store raises ReadDeniedError from the graph discovery, not the R11 walk
        """
        with store.acting_as(OWNER):
            holder = Dossier(label="holder-for-smuggled")
            holder.linked = dc.Lazy.of(twin)
            with pytest.raises(dc.ReadDeniedError):
                store.store(holder)


def test_twin_never_registered(store_factory: StoreFactory) -> None:
    """Given a reopened store where nobody has loaded an owner-only dossier
    When an outsider derefs its OID
    Then the result is a twin
    And the owner then derefs the real dossier, not a cached twin
    """
    with store_factory() as first, first.acting_as(OWNER):
        hidden_oid = first.store(Dossier(label="never-registered"))
        first.commit()
    with store_factory() as reopened:
        with reopened.acting_as(OUTSIDER):
            twin = reopened.get_many([hidden_oid])[0]
        with reopened.acting_as(OWNER):
            owner_view = reopened.get_many([hidden_oid])[0]
        assert isinstance(twin, dc.Redacted)
        assert not isinstance(owner_view, dc.Redacted)
        assert owner_view.label == "never-registered"


# --- OID deref: three distinguishable outcomes -------------------------


def test_readable_dossier_derefs_to_the_real_instance(store_factory: StoreFactory) -> None:
    """Given a reopened store with a dossier shared with the team
    When an outsider on the team derefs its OID
    Then the result is the real dossier, not a twin
    And it carries its label
    """
    readable_oid, _hidden_oid, _doomed_oid = _commit_readable_hidden_and_deleted(store_factory)
    with store_factory() as reopened, reopened.acting_as(OUTSIDER):
        real = reopened.get_many([readable_oid])[0]
    assert not isinstance(real, dc.Redacted)
    assert real.label == "readable"


def test_hidden_dossier_derefs_to_a_twin(store_factory: StoreFactory) -> None:
    """Given a reopened store with an owner-only dossier
    When an outsider derefs its OID
    Then the result is a twin
    """
    _readable_oid, hidden_oid, _doomed_oid = _commit_readable_hidden_and_deleted(store_factory)
    with store_factory() as reopened, reopened.acting_as(OUTSIDER):
        twin = reopened.get_many([hidden_oid])[0]
    assert isinstance(twin, dc.Redacted)


def test_deleted_dossier_deref_raises_dangling(store_factory: StoreFactory) -> None:
    """Given a reopened store where a dossier was deleted
    When an outsider derefs its OID
    Then the deref raises DanglingRefError
    """
    _readable_oid, _hidden_oid, doomed_oid = _commit_readable_hidden_and_deleted(store_factory)
    with (
        store_factory() as reopened,
        reopened.acting_as(OUTSIDER),
        pytest.raises(dc.DanglingRefError),
    ):
        reopened.get_many([doomed_oid])


def test_root_bypasses_the_checkpoint(store: dc.Store) -> None:
    """Given an owner-only dossier
    When root derefs its OID
    Then the result is the real dossier, not a twin
    And it carries its label
    """
    hidden = _owner_only(store, "root-sees-all")
    hidden_oid = store.store(hidden)
    with store.acting_as(ROOT):
        seen = store.get_many([hidden_oid])[0]
    assert not isinstance(seen, dc.Redacted)
    assert seen.label == "root-sees-all"


# --- invariant 6: only twins are exempt, readable identity stays intact -----


def test_denied_deref_leaves_the_registered_instance_in_the_registry(store: dc.Store) -> None:
    """Given an owner-only dossier registered live
    When an outsider derefs its OID
    Then the outsider gets a twin
    And the real dossier is still the instance a later deref returns
    """
    dossier = _owner_only(store, "poison")
    dossier_oid = store.store(dossier)
    with store.acting_as(OUTSIDER):
        twin = store.get_many([dossier_oid])[0]
    with store.acting_as(OWNER):
        real_again = store.get_many([dossier_oid])[0]
    assert isinstance(twin, dc.Redacted)
    assert real_again is dossier


def test_owner_deref_after_a_denied_deref_yields_the_same_instance(store: dc.Store) -> None:
    """Given an owner-only dossier an outsider already derefed into a twin
    When the owner derefs its OID
    Then the owner gets the identical live instance, never re-materialized or poisoned
    """
    dossier = _owner_only(store, "poison")
    dossier_oid = store.store(dossier)
    with store.acting_as(OUTSIDER):
        store.get_many([dossier_oid])[0]
    with store.acting_as(OWNER):
        real_again = store.get_many([dossier_oid])[0]
    assert real_again is dossier


# --- Lazy.get() re-checks across acting_as scopes (landmine 2) --------------


def test_lazy_get_loads_the_real_instance_for_the_owner(store_factory: StoreFactory) -> None:
    """Given a reopened store and a Lazy handle onto an owner-only target
    When the owner calls get on the handle
    Then the real target comes back, not a twin
    And it carries its label
    """
    _commit_linked_pair(store_factory, "parent-with-link", "linked-target")
    with store_factory() as reopened, reopened.acting_as(OWNER):
        handle = reopened.query(dc.fields(Dossier).label == "parent-with-link")[0].linked
        loaded = handle.get()
    assert not isinstance(loaded, dc.Redacted)
    assert loaded.label == "linked-target"


def test_lazy_get_never_caches_a_protected_target_on_the_handle(
    store_factory: StoreFactory,
) -> None:
    """Given a Lazy handle onto an owner-only target the owner already loaded
    When the handle is peeked
    Then peek returns None
    """
    _commit_linked_pair(store_factory, "parent-with-link", "linked-target")
    with store_factory() as reopened, reopened.acting_as(OWNER):
        handle = reopened.query(dc.fields(Dossier).label == "parent-with-link")[0].linked
        handle.get()
        peeked = handle.peek()
    assert peeked is None


def test_lazy_get_for_an_outsider_on_the_same_handle_yields_a_twin(
    store_factory: StoreFactory,
) -> None:
    """Given a Lazy handle onto an owner-only target the owner already loaded
    When an outsider calls get on the same handle
    Then the outsider gets a twin
    """
    _commit_linked_pair(store_factory, "parent-with-link", "linked-target")
    with store_factory() as reopened:
        with reopened.acting_as(OWNER):
            handle = reopened.query(dc.fields(Dossier).label == "parent-with-link")[0].linked
            handle.get()
        with reopened.acting_as(OUTSIDER):
            denied = handle.get()
    assert isinstance(denied, dc.Redacted)


def test_lazy_get_for_an_outsider_still_caches_nothing(store_factory: StoreFactory) -> None:
    """Given a Lazy handle onto an owner-only target an outsider just derefed into a twin
    When the handle is peeked
    Then peek returns None
    """
    _commit_linked_pair(store_factory, "parent-with-link", "linked-target")
    with store_factory() as reopened:
        with reopened.acting_as(OWNER):
            handle = reopened.query(dc.fields(Dossier).label == "parent-with-link")[0].linked
            handle.get()
        with reopened.acting_as(OUTSIDER):
            handle.get()
        peeked = handle.peek()
    assert peeked is None


def test_owner_lazy_get_after_an_outsider_deref_returns_the_same_instance(
    store_factory: StoreFactory,
) -> None:
    """Given a Lazy handle the owner loaded and an outsider then derefed
    When the owner calls get on the handle again
    Then the owner gets the identical instance, with no re-materialization
    """
    _commit_linked_pair(store_factory, "parent-with-link", "linked-target")
    with store_factory() as reopened:
        with reopened.acting_as(OWNER):
            handle = reopened.query(dc.fields(Dossier).label == "parent-with-link")[0].linked
            loaded = handle.get()
        with reopened.acting_as(OUTSIDER):
            handle.get()
        with reopened.acting_as(OWNER):
            again = handle.get()
    assert again is loaded


def test_resolve_never_prehydrates_a_protected_handle_on_a_registry_hit(
    store_factory: StoreFactory,
) -> None:
    """Given a parent hydrated after its owner-only target is already in the registry
    When the parent's Lazy handle is peeked
    Then peek returns None, the handle not pre-loaded despite the registry hit
    """
    _commit_linked_pair(store_factory, "prehydrate-parent", "prehydrate-target")
    with store_factory() as reopened, reopened.acting_as(OWNER):
        reopened.get(Dossier, label="prehydrate-target")
        parent = reopened.query(dc.fields(Dossier).label == "prehydrate-parent")[0]
        peeked = parent.linked.peek()
    assert peeked is None


def test_lazy_get_on_an_unprotected_target_still_caches(store_factory: StoreFactory) -> None:
    """Given a reopened store and a Lazy handle onto an unprotected target
    When get is called on the handle twice
    Then peek returns the first instance
    And the second get returns that same instance
    """
    with store_factory() as first:
        target = Plain(label="open-target")
        first.store(target)
        holder = PlainHolder(label="open-holder")
        holder.linked = dc.Lazy.of(target)
        first.store(holder)
        first.commit()
    with store_factory() as reopened:
        handle = reopened.query(PlainHolder)[0].linked
        first_get = handle.get()
        peeked = handle.peek()
        second_get = handle.get()
    assert peeked is first_get
    assert second_get is first_get


# --- Fable read-fence review follow-ups: peek()/__repr__ + pre-commit deref ---


def test_peek_returns_none_for_a_protected_target(store: dc.Store) -> None:
    """Given a stored vault holding a user Lazy.of handle onto a protected dossier
    When the handle is peeked
    Then peek returns None, uniformly fenced even for the owner
    """
    secret = dc.Lazy.of(Dossier(label="d", note="classified"))
    with store.acting_as(OWNER):
        store.store(Vault(label="v", secret=secret))
        store.commit()
    assert secret.peek() is None


def test_get_is_the_way_in_to_a_protected_target(store: dc.Store) -> None:
    """Given a stored vault holding a user Lazy.of handle onto a protected dossier
    When the owner calls get on the handle
    Then the owner reads the dossier's note
    """
    secret = dc.Lazy.of(Dossier(label="d", note="classified"))
    with store.acting_as(OWNER):
        store.store(Vault(label="v", secret=secret))
        store.commit()
        note = secret.get().note
    assert note == "classified"


def test_peek_still_serves_an_unprotected_target(store: dc.Store) -> None:
    """Given a stored holder with a user Lazy.of handle onto an unprotected record
    When the handle is peeked
    Then peek returns the record
    """
    linked = dc.Lazy.of(Plain(label="pl"))
    with store.acting_as(OWNER):
        store.store(PlainHolder(label="ph", linked=linked))
        store.commit()
    assert linked.peek() is not None


def test_repr_of_a_protected_handle_names_the_state_not_the_data(store: dc.Store) -> None:
    """Given a stored vault holding a Lazy.of handle onto a dossier with the note XYZZY
    When the handle is rendered with repr
    Then the text says protected
    And it shows no field content
    And it shows no label columns
    """
    secret = dc.Lazy.of(Dossier(label="d", note="XYZZY"))
    with store.acting_as(OWNER):
        store.store(Vault(label="v", secret=secret))
        store.commit()
    rendered = repr(secret)
    assert "protected" in rendered
    assert "XYZZY" not in rendered
    assert "_dc_owner" not in rendered


def test_peek_by_an_outsider_scope_returns_none(store: dc.Store) -> None:
    """Given a curator's Lazy.of handle onto a protected dossier in a shared live parent
    When an outsider scope peeks the handle
    Then peek returns None (Fable leak #2)
    """
    secret = dc.Lazy.of(Dossier(label="d", note="secret"))
    with store.acting_as(OWNER):
        store.store(Vault(label="v", secret=secret))
        store.commit()
    with store.acting_as(OUTSIDER):
        peeked = secret.peek()
    assert peeked is None


def test_get_by_an_outsider_scope_returns_a_twin(store: dc.Store) -> None:
    """Given a curator's Lazy.of handle onto a protected dossier in a shared live parent
    When an outsider scope calls get on the handle
    Then it gets a twin (Fable leak #3)
    """
    secret = dc.Lazy.of(Dossier(label="d", note="secret"))
    with store.acting_as(OWNER):
        store.store(Vault(label="v", secret=secret))
        store.commit()
    with store.acting_as(OUTSIDER):
        got = secret.get()
    assert isinstance(got, dc.Redacted)


def test_twin_from_an_outsider_get_refuses_the_note(store: dc.Store) -> None:
    """Given the twin an outsider got from a curator's Lazy.of handle
    When its note is read
    Then the read raises ReadDeniedError
    """
    secret = dc.Lazy.of(Dossier(label="d", note="secret"))
    with store.acting_as(OWNER):
        store.store(Vault(label="v", secret=secret))
        store.commit()
    with store.acting_as(OUTSIDER):
        got = secret.get()
    with pytest.raises(dc.ReadDeniedError):
        _ = got.note


def test_owner_derefs_own_uncommitted_protected_target(store: dc.Store) -> None:
    """Given a vault with a new protected dossier, stored but not yet committed
    When the owner calls get on the handle
    Then the owner reads the in-flight note, not a DanglingRefError (Fable finding B)
    """
    secret = dc.Lazy.of(Dossier(label="d", note="wip"))
    with store.acting_as(OWNER):
        store.store(Vault(label="v", secret=secret))
        note = secret.get().note
    assert note == "wip"


def test_outsider_gets_a_twin_for_an_uncommitted_protected_target(store: dc.Store) -> None:
    """Given a vault with a new protected dossier, stored but not yet committed
    When an outsider calls get on the handle in that window
    Then it gets a twin
    """
    secret = dc.Lazy.of(Dossier(label="d", note="wip"))
    with store.acting_as(OWNER):
        store.store(Vault(label="v", secret=secret))
        with store.acting_as(OUTSIDER):
            got = secret.get()
    assert isinstance(got, dc.Redacted)


def test_owner_still_reads_the_target_after_the_commit(store: dc.Store) -> None:
    """Given a vault with a new protected dossier, stored and then committed
    When the owner calls get on the handle
    Then the owner still reads the note
    """
    secret = dc.Lazy.of(Dossier(label="d", note="wip"))
    with store.acting_as(OWNER):
        store.store(Vault(label="v", secret=secret))
        store.commit()
        note = secret.get().note
    assert note == "wip"
