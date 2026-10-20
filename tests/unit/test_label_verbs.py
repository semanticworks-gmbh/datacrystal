"""W2-4: the label verbs — dc.share / dc.unshare / dc.protect + the writable
``dc_permissions`` property (ADR-008 R6, epic #168).

Verbs STAGE through normal dirty-tracking and perform zero authority checks
(enforcement is the W2-5 commit gate, against the committing principal —
the maker–checker flow depends on stage-now-reject-at-commit). Raising
paths stage nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import state_of
from datacrystal._errors import DeletedEntityError, FrozenEntityError, NotAnEntityError
from datacrystal._state import STATE_CLEAN, STATE_DIRTY

StoreFactory = Callable[[], dc.Store]

ORG, TEAM = 1, 2
ANNA = dc.Principal(uid=2, memberships={ORG: dc.STAFF, TEAM: dc.CURATOR})
LOW = dc.Principal(uid=900, memberships={TEAM: dc.AGENT})


@dc.entity(protected=True)
class Contact:
    """A protected business contact."""

    name: Annotated[str, dc.Unique]
    org: str = ""


@dc.entity(frozen=True, protected=True)
class SealedNote:
    """A frozen protected note."""

    seq: Annotated[int, dc.Unique]
    text: str = ""


@dc.entity
class OpenNote:
    """An unprotected note."""

    seq: Annotated[int, dc.Unique]


def _permissions(record: object) -> dc.Permissions:
    return record.dc_permissions  # pyrefly: ignore[missing-attribute]


def _labels(record: object) -> tuple[dc.Permissions, int]:
    return (_permissions(record), state_of(record))


def _commit_contact(store: dc.Store, name: str) -> Contact:
    with store.acting_as(ANNA):
        contact = Contact(name=name)
        store.store(contact)
        store.commit()
    return contact


def _commit_contact_shared_with_the_team(store: dc.Store) -> Contact:
    with store.acting_as(ANNA):
        contact = Contact(name="Meyer")
        store.store(contact)
        dc.share(contact, TEAM, read=dc.VIEWER, write=dc.AGENT)
        store.commit()
    return contact


def _commit_sealed_note(store: dc.Store) -> SealedNote:
    with store.acting_as(ANNA):
        note = SealedNote(seq=1, text="fixed")
        store.store(note)
        store.commit()
    return note


class TestShare:
    """dc.share stages a group and its floors."""

    def test_share_without_levels_raises(self) -> None:
        """Given a new contact
        When it is shared with a group and no levels
        Then share raises TypeError, R6 allowing no silent grant levels
        """
        contact = Contact(name="Meyer")
        with pytest.raises(TypeError):
            dc.share(contact, TEAM)  # pyrefly: ignore[missing-argument]

    def test_share_with_a_read_level_only_raises(self) -> None:
        """Given a new contact
        When it is shared with a group and a read level but no write level
        Then share raises TypeError
        """
        contact = Contact(name="Meyer")
        with pytest.raises(TypeError):
            dc.share(contact, TEAM, read=dc.VIEWER)  # pyrefly: ignore[missing-argument]

    def test_share_marks_a_committed_record_dirty(self, store: dc.Store) -> None:
        """Given a committed contact in state CLEAN
        When it is shared with the team
        Then the contact is DIRTY, buffered via dirty-tracking
        """
        contact = _commit_contact(store, "Meyer")
        dc.share(contact, TEAM, read=dc.VIEWER, write=dc.AGENT)
        assert state_of(contact) == STATE_DIRTY

    def test_shared_group_and_floors_roundtrip_through_reopen(
        self, store_factory: StoreFactory
    ) -> None:
        """Given a contact shared with the team (viewer read, agent write) and committed
        When the store is reopened and the team's curator reads the contact
        Then its groups are the team
        And its read floor is viewer
        And its write floor is agent
        """
        with store_factory() as first:
            contact = _commit_contact(first, "Meyer")
            with first.acting_as(ANNA):
                dc.share(contact, TEAM, read=dc.VIEWER, write=dc.AGENT)
                first.commit()
        with store_factory() as reopened, reopened.acting_as(ANNA):
            back = reopened.get(Contact, name="Meyer")
        assert _permissions(back).groups == (TEAM,)
        assert _permissions(back).read_floor == dc.VIEWER
        assert _permissions(back).write_floor == dc.AGENT

    def test_sharing_a_group_twice_adds_it_once_and_moves_the_floors(self) -> None:
        """Given a contact shared with the team (viewer read, agent write)
        When it is shared with the team again (viewer read, curator write)
        Then the team is in its groups exactly once
        And its write floor is curator
        """
        contact = Contact(name="Meyer")
        dc.share(contact, TEAM, read=dc.VIEWER, write=dc.AGENT)
        dc.share(contact, TEAM, read=dc.VIEWER, write=dc.CURATOR)
        assert _permissions(contact).groups == (TEAM,)
        assert _permissions(contact).write_floor == dc.CURATOR

    def test_sharing_stages_floors_above_the_actors_own_rank(self, store: dc.Store) -> None:
        """Given an agent-level principal acting on a stored contact
        When the contact is shared with an executive write floor
        Then the executive floor is staged, rejection being the commit gate's job (W2-5)
        """
        with store.acting_as(LOW):
            contact = Contact(name="proposal-target")
            store.store(contact)
            dc.share(contact, TEAM, read=dc.VIEWER, write=dc.EXECUTIVE)
            staged_floor = _permissions(contact).write_floor
        assert staged_floor == dc.EXECUTIVE


class TestUnshare:
    """dc.unshare removes a group and stages the change."""

    def test_unshare_removes_the_group(self, store: dc.Store) -> None:
        """Given a committed contact shared with the team
        When the team is unshared
        Then the contact has no groups
        """
        contact = _commit_contact_shared_with_the_team(store)
        dc.unshare(contact, TEAM)
        assert _permissions(contact).groups == ()

    def test_unshare_leaves_the_floors_untouched(self, store: dc.Store) -> None:
        """Given a committed contact shared with the team (agent write floor)
        When the team is unshared
        Then the write floor is still agent
        """
        contact = _commit_contact_shared_with_the_team(store)
        dc.unshare(contact, TEAM)
        assert _permissions(contact).write_floor == dc.AGENT

    def test_unshare_marks_the_contact_dirty(self, store: dc.Store) -> None:
        """Given a committed contact shared with the team
        When the team is unshared
        Then the contact is DIRTY
        """
        contact = _commit_contact_shared_with_the_team(store)
        dc.unshare(contact, TEAM)
        assert state_of(contact) == STATE_DIRTY

    def test_unsharing_an_absent_group_is_a_clean_noop(self, store: dc.Store) -> None:
        """Given a committed contact in state CLEAN never shared with group 999
        When group 999 is unshared
        Then the contact is still CLEAN, with no spurious buffered write
        """
        contact = _commit_contact(store, "Meyer")
        dc.unshare(contact, 999)
        assert state_of(contact) == STATE_CLEAN


class TestProtect:
    """dc.protect stages one or both floors and never touches groups."""

    def test_protecting_the_read_floor_leaves_the_write_floor(self) -> None:
        """Given a new contact
        When its read floor is protected at agent
        Then the read floor is agent
        And the write floor is still viewer
        """
        contact = Contact(name="Meyer")
        dc.protect(contact, read=dc.AGENT)
        assert _permissions(contact).read_floor == dc.AGENT
        assert _permissions(contact).write_floor == dc.VIEWER

    def test_protecting_the_write_floor_leaves_the_read_floor_and_groups(self) -> None:
        """Given a contact whose read floor is protected at agent
        When its write floor is protected at curator
        Then the read floor is still agent
        And the write floor is curator
        And it has no groups
        """
        contact = Contact(name="Meyer")
        dc.protect(contact, read=dc.AGENT)
        dc.protect(contact, write=dc.CURATOR)
        assert _permissions(contact).read_floor == dc.AGENT
        assert _permissions(contact).write_floor == dc.CURATOR
        assert _permissions(contact).groups == ()

    def test_bare_protect_raises(self) -> None:
        """Given a new contact
        When it is protected with neither floor
        Then protect raises TypeError asking for read= and/or write=
        """
        contact = Contact(name="Meyer")
        with pytest.raises(TypeError, match="read= and/or write="):
            dc.protect(contact)


class TestRaisingPathsStageNothing:
    """A verb that raises leaves the labels and the state as they were."""

    def test_sharing_a_frozen_record_raises(self, store: dc.Store) -> None:
        """Given a committed frozen note
        When it is shared with the team
        Then share raises FrozenEntityError, labels being fixed at registration
        """
        note = _commit_sealed_note(store)
        with pytest.raises(FrozenEntityError, match="fixed at registration"):
            dc.share(note, TEAM, read=dc.VIEWER, write=dc.AGENT)

    def test_sharing_a_frozen_record_stages_nothing(self, store: dc.Store) -> None:
        """Given a committed frozen note
        When sharing it with the team raises
        Then its labels and state are unchanged
        """
        note = _commit_sealed_note(store)
        before = _labels(note)
        with pytest.raises(FrozenEntityError):
            dc.share(note, TEAM, read=dc.VIEWER, write=dc.AGENT)
        assert _labels(note) == before

    def test_unsharing_a_frozen_record_raises(self, store: dc.Store) -> None:
        """Given a committed frozen note
        When the world group is unshared from it
        Then unshare raises FrozenEntityError, labels being fixed at registration
        """
        note = _commit_sealed_note(store)
        with pytest.raises(FrozenEntityError, match="fixed at registration"):
            dc.unshare(note, dc.WORLD)

    def test_unsharing_a_frozen_record_stages_nothing(self, store: dc.Store) -> None:
        """Given a committed frozen note
        When unsharing the world group from it raises
        Then its labels and state are unchanged
        """
        note = _commit_sealed_note(store)
        before = _labels(note)
        with pytest.raises(FrozenEntityError):
            dc.unshare(note, dc.WORLD)
        assert _labels(note) == before

    def test_protecting_a_frozen_record_raises(self, store: dc.Store) -> None:
        """Given a committed frozen note
        When its write floor is protected at admin
        Then protect raises FrozenEntityError, labels being fixed at registration
        """
        note = _commit_sealed_note(store)
        with pytest.raises(FrozenEntityError, match="fixed at registration"):
            dc.protect(note, write=dc.ADMIN)

    def test_protecting_a_frozen_record_stages_nothing(self, store: dc.Store) -> None:
        """Given a committed frozen note
        When protecting its write floor at admin raises
        Then its labels and state are unchanged
        """
        note = _commit_sealed_note(store)
        before = _labels(note)
        with pytest.raises(FrozenEntityError):
            dc.protect(note, write=dc.ADMIN)
        assert _labels(note) == before

    def test_sharing_a_snapshot_view_raises_read_only(self, store: dc.Store) -> None:
        """Given a snapshot view of a committed contact, bound to its owner
        When the view is shared with the team
        Then share raises AttributeError saying read-only
        """
        _commit_contact(store, "Meyer")
        [view] = store.snapshot(principal=ANNA).query(dc.fields(Contact).name == "Meyer")
        with pytest.raises(AttributeError, match="read-only"):
            dc.share(view, TEAM, read=dc.VIEWER, write=dc.AGENT)

    def test_assigning_permissions_on_a_snapshot_view_raises_read_only(
        self, store: dc.Store
    ) -> None:
        """Given a snapshot view of a committed contact, bound to its owner
        When permissions are assigned to the view
        Then the assignment raises AttributeError saying read-only
        """
        _commit_contact(store, "Meyer")
        [view] = store.snapshot(principal=ANNA).query(dc.fields(Contact).name == "Meyer")
        with pytest.raises(AttributeError, match="read-only"):
            view.dc_permissions = dc.Permissions(0, (), 0, 0)

    def test_sharing_an_unprotected_record_raises_type_error(self) -> None:
        """Given an entity of an unprotected class
        When it is shared with the team
        Then share raises TypeError saying not protected
        """
        note = OpenNote(seq=1)
        with pytest.raises(TypeError, match="not protected"):
            dc.share(note, TEAM, read=dc.VIEWER, write=dc.AGENT)

    def test_protecting_a_non_entity_raises_not_an_entity(self) -> None:
        """Given a plain object
        When its write floor is protected
        Then protect raises NotAnEntityError
        """
        with pytest.raises(NotAnEntityError):
            dc.protect(object(), write=dc.ADMIN)  # pyrefly: ignore[bad-argument-type]

    def test_protecting_a_deleted_record_raises(self, store: dc.Store) -> None:
        """Given a contact deleted in the current transaction
        When its write floor is protected
        Then protect raises DeletedEntityError
        """
        with store.acting_as(ANNA):
            contact = Contact(name="doomed")
            store.store(contact)
            store.commit()
            store.delete(contact)
        with pytest.raises(DeletedEntityError):
            dc.protect(contact, write=dc.ADMIN)

    def test_sharing_with_the_no_standing_level_raises_and_stages_nothing(self) -> None:
        """Given a new contact
        When it is shared with the no-standing read level
        Then share raises ValueError saying not a grantable level
        And its labels and state are unchanged
        """
        contact = Contact(name="Meyer")
        before = _labels(contact)
        with pytest.raises(ValueError, match="not a grantable level"):
            dc.share(contact, TEAM, read=dc.NO_STANDING, write=dc.AGENT)
        assert _labels(contact) == before

    def test_protecting_with_a_level_name_raises_and_stages_nothing(self) -> None:
        """Given a new contact
        When its write floor is protected with the string "CURATOR"
        Then protect raises TypeError asking for an int ladder level
        And its labels and state are unchanged
        """
        contact = Contact(name="Meyer")
        before = _labels(contact)
        with pytest.raises(TypeError, match="int ladder level"):
            dc.protect(contact, write="CURATOR")  # pyrefly: ignore[bad-argument-type]
        assert _labels(contact) == before

    def test_sharing_with_a_group_name_raises_and_stages_nothing(self) -> None:
        """Given a new contact
        When it is shared with the group "team-pv"
        Then share raises TypeError asking for an int group id
        And its labels and state are unchanged
        """
        contact = Contact(name="Meyer")
        before = _labels(contact)
        with pytest.raises(TypeError, match="int group id"):
            dc.share(
                contact,
                "team-pv",  # pyrefly: ignore[bad-argument-type]
                read=dc.VIEWER,
                write=dc.AGENT,
            )
        assert _labels(contact) == before

    def test_sharing_with_a_bool_level_raises_and_stages_nothing(self) -> None:
        """Given a new contact
        When it is shared with True as its read level
        Then share raises TypeError asking for an int ladder level, bools not being levels
        And its labels and state are unchanged
        """
        contact = Contact(name="Meyer")
        before = _labels(contact)
        with pytest.raises(TypeError, match="int ladder level"):
            dc.share(contact, TEAM, read=True, write=dc.AGENT)
        assert _labels(contact) == before


class TestWritableDcPermissions:
    """dc_permissions accepts a Permissions value and copies it."""

    def test_assigning_permissions_copies_owner_groups_and_write_floor(
        self, store_factory: StoreFactory
    ) -> None:
        """Given a child contact assigned its parent's permissions (team group, curator write)
        When the store is reopened and the team's curator reads the child
        Then the child's owner is the parent's owner
        And its groups are the team
        And its write floor is curator
        """
        with store_factory() as first, first.acting_as(ANNA):
            parent = Contact(name="parent")
            first.store(parent)
            dc.share(parent, TEAM, read=dc.VIEWER, write=dc.CURATOR)
            child = Contact(name="child")
            first.store(child)
            child.dc_permissions = parent.dc_permissions  # pyrefly: ignore[missing-attribute]
            first.commit()
        with store_factory() as reopened, reopened.acting_as(ANNA):
            back = reopened.get(Contact, name="child")
        assert _permissions(back).owner == 2
        assert _permissions(back).groups == (TEAM,)
        assert _permissions(back).write_floor == dc.CURATOR

    def test_assigned_permissions_do_not_alias_the_source_record(self, store: dc.Store) -> None:
        """Given a child assigned its parent's permissions (team group)
        When the org group is appended to the child's groups
        Then the parent's groups are still the team alone
        """
        with store.acting_as(ANNA):
            parent = Contact(name="p2")
            store.store(parent)
            dc.share(parent, TEAM, read=dc.VIEWER, write=dc.AGENT)
            child = Contact(name="c2")
            store.store(child)
            child.dc_permissions = parent.dc_permissions  # pyrefly: ignore[missing-attribute]
            child._dc_groups.append(ORG)  # pyrefly: ignore[missing-attribute]
        assert _permissions(parent).groups == (TEAM,)

    def test_labels_staged_before_store_survive_registration(self, store: dc.Store) -> None:
        """Given a new child shared with the team before store() and a parent shared with the org
        When the child is stored and committed
        Then its groups are the team, not the org
        And its read floor is agent
        And its write floor is curator
        And its owner is the acting principal
        """
        with store.acting_as(ANNA):
            parent = Contact(name="holder")
            store.store(parent)
            dc.share(parent, ORG, read=dc.VIEWER, write=dc.STAFF)
            child = Contact(name="staged-first")
            dc.share(child, TEAM, read=dc.AGENT, write=dc.CURATOR)
            parent.org = "links-child"
            store.store(child)
            store.commit()
        assert _permissions(child).groups == (TEAM,)
        assert _permissions(child).read_floor == dc.AGENT
        assert _permissions(child).write_floor == dc.CURATOR
        assert _permissions(child).owner == 2

    def test_child_stored_directly_without_a_container_link_keeps_birth_groups(
        self, store: dc.Store
    ) -> None:
        """Given a stored parent shared with the org and a new untouched child
        When the child is stored directly, with no container link to the parent
        Then the child has no groups
        """
        with store.acting_as(ANNA):
            parent = Contact(name="h2")
            store.store(parent)
            dc.share(parent, ORG, read=dc.VIEWER, write=dc.STAFF)
            child = Contact(name="plain-child")
            parent.org = "x"
            store.store(child)
        assert _permissions(child).groups == ()
