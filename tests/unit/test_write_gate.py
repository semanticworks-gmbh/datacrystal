"""W2-5/6/7: the commit-P1 write gate (ADR-008 R8/R9/R10, epic #168).

Every buffered protected write — content, label change, delete — clears the
record's CURRENT persisted write floor; changed floors clear the R8 ceiling;
root (EXECUTIVE in WORLD) is the one bypass and still stamps. Denial =
WriteDeniedError strictly before the TID: gapless sequence, buffers intact.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import CommitBatch, StoredRecord

StoreFactory = Callable[[], dc.Store]

ORG, TEAM = 1, 2
OWNER_STAFF = dc.Principal(uid=2, memberships={TEAM: dc.STAFF})
CURATOR = dc.Principal(uid=4, memberships={TEAM: dc.CURATOR})
AGENT_900 = dc.Principal(uid=900, memberships={TEAM: dc.AGENT})
OUTSIDER = dc.Principal(uid=7, memberships={ORG: dc.EXECUTIVE})  # exec, wrong group
STORE_ADMIN = dc.Principal(uid=8, memberships={dc.WORLD: dc.ADMIN})
ROOT = dc.Principal(uid=99, memberships={dc.WORLD: dc.EXECUTIVE})
FAKE_ROOT = dc.Principal(uid=0, memberships={dc.WORLD: dc.EXECUTIVE})  # R7a


@dc.entity(protected=True)
class Contact:
    """A protected contact; refs to protected records are Lazy-only (R11)."""

    name: Annotated[str, dc.Unique]
    org: str = ""
    link: dc.Lazy[Contact] | None = None


@dc.entity
class OpenNote:
    """An unprotected note."""

    seq: Annotated[int, dc.Unique]
    text: str = ""


CONTACT = dc.fields(Contact)
ACTOR = dc.fields(dc.Actor)


def _curated_contact(store: dc.Store, name: str = "Meyer") -> Contact:
    """Commit a contact created by OWNER_STAFF, shared to TEAM, then ratcheted to write=CURATOR.

    The LUMEN headline setup: the curator raised the floor above the owner's own level.
    """
    with store.acting_as(OWNER_STAFF):
        contact = Contact(name=name)
        store.store(contact)
        dc.share(contact, TEAM, read=dc.VIEWER, write=dc.STAFF)
        store.commit()
    with store.acting_as(CURATOR):
        dc.protect(contact, write=dc.CURATOR)
        store.commit()
    return contact


def _register_bot_agent(store: dc.Store, memberships: dict[int, int]) -> None:
    """Have root register actor 900, the bot, with the given memberships."""
    with store.acting_as(ROOT):
        store.store(dc.Actor(uid=900, display="bot", human=True, memberships=memberships))
        store.commit()


def _commit_w1_ledger(store_factory: StoreFactory) -> type:
    """Persist a pre-protection Ledger row, then return the protected retrofit class.

    The same-typename retrofit trick: the persisted row decodes through the new protected class.
    """
    annotations = {"tag": Annotated[str, dc.Unique]}
    namespace: dict = {
        "__module__": __name__,
        "__qualname__": "Ledger",
        "__annotations__": annotations,
    }
    legacy = dc.entity(type("Ledger", (), namespace))
    store = store_factory()
    store.store(legacy(tag="w1-row"))
    store.commit()
    store.close()
    retrofit_namespace: dict = {
        "__module__": __name__,
        "__qualname__": "Ledger",
        "__annotations__": dict(annotations),
    }
    return dc.entity(protected=True)(type("Ledger", (), retrofit_namespace))


class _DeltaRecorder:
    """A delta consumer recording the actor stamped on each commit, attached mid-life."""

    def __init__(self, watermark: int) -> None:
        self.watermark = watermark
        self.stamps: list[int] = []

    def apply(self, delta: dict) -> None:
        self.stamps.append(delta["actor"])
        self.watermark = delta["tid"]


class _Counting:
    """The counting-storage-wrapper idiom (fitness #19 pattern), with an apply counter.

    The apply counter serves the async pre-P2 assertion.
    """

    def __init__(self, inner: MemoryBackend) -> None:
        self._inner = inner
        self.load_calls = 0
        self.records_loaded = 0
        self.applies = 0

    def reset(self) -> None:
        self.load_calls = 0
        self.records_loaded = 0

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        out = self._inner.load_many(oids)
        self.load_calls += 1
        self.records_loaded += len(out)
        return out

    def apply(self, batch: CommitBatch) -> object:
        self.applies += 1
        return self._inner.apply(batch)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def _commit_through_async_store(store: dc.Store) -> None:
    astore = dc.AsyncStore(store)
    try:
        await astore.commit()
    finally:
        astore.close()


class TestWriteFloor:
    """A write clears the record's current persisted write floor."""

    def test_agent_cannot_overwrite_a_curated_record(self, store: dc.Store) -> None:
        """Given a contact whose write floor the curator ratcheted to curator
        When an agent overwrites its org and commits
        Then the commit raises WriteDeniedError naming the write floor
        """
        contact = _curated_contact(store)
        with store.acting_as(AGENT_900):
            contact.org = "tampered"
            with pytest.raises(dc.WriteDeniedError, match="write floor"):
                store.commit()
            store.discard()

    def test_denied_overwrite_leaves_the_curated_record_unchanged(self, store: dc.Store) -> None:
        """Given an agent whose overwrite of a curated contact was denied and discarded
        When the agent reads the contact back
        Then its org is still empty
        """
        contact = _curated_contact(store)
        with store.acting_as(AGENT_900):
            contact.org = "tampered"
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()
            org_after = store.query(CONTACT.name == "Meyer")[0].org
        assert org_after == ""

    def test_the_floor_binds_the_owner_too(self, store: dc.Store) -> None:
        """Given a contact the staff owner created and the curator ratcheted to curator
        When the owner overwrites its org and commits
        Then the commit raises WriteDeniedError saying the floor binds the owner too
        """
        contact = _curated_contact(store)
        with store.acting_as(OWNER_STAFF):
            contact.org = "owner-edit"
            with pytest.raises(dc.WriteDeniedError, match="including the owner"):
                store.commit()
            store.discard()

    def test_label_only_change_is_gated_like_content(self, store: dc.Store) -> None:
        """Given a contact whose write floor the curator ratcheted to curator
        When an agent unshares the team and commits
        Then the commit raises WriteDeniedError, a label write being a write
        """
        contact = _curated_contact(store)
        with store.acting_as(AGENT_900):
            dc.unshare(contact, TEAM)
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()

    def test_delete_is_gated(self, store: dc.Store) -> None:
        """Given a contact whose write floor the curator ratcheted to curator
        When an agent deletes it and commits
        Then the commit raises WriteDeniedError naming delete
        """
        contact = _curated_contact(store)
        with store.acting_as(AGENT_900):
            store.delete(contact)
            with pytest.raises(dc.WriteDeniedError, match="delete"):
                store.commit()
            store.discard()

    def test_denied_delete_keeps_the_record(self, store: dc.Store) -> None:
        """Given an agent whose delete of a curated contact was denied and discarded
        When the agent looks the contact up
        Then it is still there
        """
        contact = _curated_contact(store)
        with store.acting_as(AGENT_900):
            store.delete(contact)
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()
            found = store.get(Contact, name="Meyer")
        assert found is not None

    def test_clearing_principal_writes_fine(self, store: dc.Store) -> None:
        """Given a contact whose write floor is curator
        When the curator edits its org and commits
        Then the new org is stored
        """
        contact = _curated_contact(store)
        with store.acting_as(CURATOR):
            contact.org = "curated edit"
            store.commit()
            org_after = store.query(CONTACT.name == "Meyer")[0].org
        assert org_after == "curated edit"

    def test_outside_authority_does_not_carry(self, store: dc.Store) -> None:
        """Given a team-shared contact and an executive of another group
        When the outsider edits its org and commits
        Then the commit raises WriteDeniedError, authority in ORG meaning nothing in TEAM
        """
        contact = _curated_contact(store)
        with store.acting_as(OUTSIDER):
            contact.org = "cross-group"
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()


class TestLegacyGating:
    """A pre-protection record is fenced at admin in the world (R7)."""

    def test_curator_cannot_write_a_legacy_record(self, store_factory: StoreFactory) -> None:
        """Given a W1-era row that is now protected
        When a curator, high but not admin in the world, renames it and commits
        Then the commit raises WriteDeniedError
        """
        retrofit = _commit_w1_ledger(store_factory)
        with store_factory() as reopened, reopened.acting_as(CURATOR):
            row = reopened.query(dc.fields(retrofit).tag == "w1-row")[0]
            row.tag = "renamed"
            with pytest.raises(dc.WriteDeniedError):
                reopened.commit()
            reopened.discard()

    def test_store_wide_admin_writes_a_legacy_record(self, store_factory: StoreFactory) -> None:
        """Given a W1-era row that is now protected
        When an admin held in the world renames it and commits
        Then the row is found under its new tag
        """
        retrofit = _commit_w1_ledger(store_factory)
        with store_factory() as reopened:
            with reopened.acting_as(STORE_ADMIN):
                row = reopened.query(dc.fields(retrofit).tag == "w1-row")[0]
                row.tag = "renamed"
                reopened.commit()
            renamed = reopened.get(retrofit, tag="renamed")
        assert renamed is not None


class TestCeiling:
    """A changed floor or group is capped by the committer's own authority (R8)."""

    def test_changed_floor_above_own_authority_denied(self, store: dc.Store) -> None:
        """Given an agent sharing its new contact with a curator write floor
        When the agent commits
        Then the commit raises WriteDeniedError naming the ceiling
        """
        with store.acting_as(AGENT_900):
            contact = Contact(name="proposal")
            store.store(contact)
            dc.share(contact, TEAM, read=dc.VIEWER, write=dc.CURATOR)
            with pytest.raises(dc.WriteDeniedError, match="ceiling"):
                store.commit()
            store.discard()

    def test_maker_checker_works_at_own_level(self, store: dc.Store) -> None:
        """Given an agent's committed proposal fenced at the agent's own write level
        When the curator edits it and commits
        Then the edit commits
        """
        with store.acting_as(AGENT_900):
            contact = Contact(name="proposal-2")
            store.store(contact)
            dc.share(contact, TEAM, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
        with store.acting_as(CURATOR):
            contact.org = "enacted"
            committed = store.commit()
        assert committed is not None

    def test_unchanged_high_floor_never_blocks_a_content_edit(self, store: dc.Store) -> None:
        """Given a contact with curator read and write floors committed by the curator
        When the curator edits its content and commits
        Then the edit commits, R8 checking changed floors only
        """
        contact = _curated_contact(store)
        with store.acting_as(CURATOR):
            dc.protect(contact, read=dc.CURATOR)
            store.commit()
        with store.acting_as(CURATOR):
            contact.org = "still fine"
            committed = store.commit()
        assert committed is not None

    def test_owner_ratchets_to_personal_best(self, store: dc.Store) -> None:
        """Given a staff owner's committed contact
        When the owner protects its write floor at staff, their personal best, and commits
        Then the commit succeeds
        """
        with store.acting_as(OWNER_STAFF):
            contact = Contact(name="mine")
            store.store(contact)
            store.commit()
            dc.protect(contact, write=dc.STAFF)
            committed = store.commit()
        assert committed is not None

    def test_owner_cannot_ratchet_beyond_personal_best(self, store: dc.Store) -> None:
        """Given a staff owner's committed contact
        When the owner protects its write floor at admin and commits
        Then the commit raises WriteDeniedError naming the ceiling
        """
        with store.acting_as(OWNER_STAFF):
            contact = Contact(name="mine")
            store.store(contact)
            store.commit()
            dc.protect(contact, write=dc.ADMIN)
            with pytest.raises(dc.WriteDeniedError, match="ceiling"):
                store.commit()
            store.discard()

    def test_share_into_unheld_group_denied(self, store: dc.Store) -> None:
        """Given a staff owner who holds TEAM only and shares a new contact into ORG
        When the owner commits
        Then the commit raises WriteDeniedError saying no standing
        """
        with store.acting_as(OWNER_STAFF):
            contact = Contact(name="handoff")
            store.store(contact)
            dc.share(contact, ORG, read=dc.VIEWER, write=dc.VIEWER)
            with pytest.raises(dc.WriteDeniedError, match="no standing"):
                store.commit()
            store.discard()

    def test_lowering_within_ceiling_is_fine(self, store: dc.Store) -> None:
        """Given a contact the curator lowered from curator to staff write
        When the staff owner edits its org and commits
        Then the edit commits
        """
        contact = _curated_contact(store)
        with store.acting_as(CURATOR):
            dc.protect(contact, write=dc.STAFF)
            store.commit()
        with store.acting_as(OWNER_STAFF):
            contact.org = "owner back in"
            committed = store.commit()
        assert committed is not None


class TestOwnershipImmutable:
    """Ownership is immutable after birth — only root may reassign it (ADR-008 R8 amendment).

    Without this, any writer who clears a record's CURRENT write floor could stage itself as
    owner and gain both a permanent owner-read bypass and its personal-best level as the R8
    ceiling (which reads the staged owner). A chown is not a mint for authority.
    """

    def test_writer_cannot_seize_ownership(self, store: dc.Store) -> None:
        """Given a contact whose write floor lets agent 900 write
        When agent 900 stages itself as owner, floors unchanged, and commits
        Then the commit raises WriteDeniedError saying ownership is immutable
        """
        with store.acting_as(OWNER_STAFF):
            contact = Contact(name="seize-me")
            store.store(contact)
            dc.share(contact, TEAM, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
        with store.acting_as(AGENT_900):
            contact.dc_permissions = dc.Permissions(  # pyrefly: ignore[missing-attribute]
                owner=AGENT_900.uid,
                groups=(TEAM,),
                read_floor=dc.VIEWER,
                write_floor=dc.AGENT,
            )
            with pytest.raises(dc.WriteDeniedError, match="ownership is immutable"):
                store.commit()
            store.discard()

    def test_denied_ownership_seizure_leaves_the_owner(self, store: dc.Store) -> None:
        """Given agent 900 whose staged seizure of a contact's ownership was denied and discarded
        When the owner reads the contact back
        Then the owner is still the staff owner
        """
        with store.acting_as(OWNER_STAFF):
            contact = Contact(name="seize-me")
            store.store(contact)
            dc.share(contact, TEAM, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
        with store.acting_as(AGENT_900):
            contact.dc_permissions = dc.Permissions(  # pyrefly: ignore[missing-attribute]
                owner=AGENT_900.uid,
                groups=(TEAM,),
                read_floor=dc.VIEWER,
                write_floor=dc.AGENT,
            )
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()
        with store.acting_as(OWNER_STAFF):
            owner_after = store.query(CONTACT.name == "seize-me")[0]._dc_owner
        assert owner_after == OWNER_STAFF.uid

    def test_direct_owner_field_poke_is_gated_too(self, store: dc.Store) -> None:
        """Given a contact whose write floor lets agent 900 write
        When agent 900 assigns the owner column directly and commits
        Then the commit raises WriteDeniedError saying ownership is immutable
        """
        with store.acting_as(OWNER_STAFF):
            contact = Contact(name="poke-me")
            store.store(contact)
            dc.share(contact, TEAM, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
        with store.acting_as(AGENT_900):
            contact._dc_owner = AGENT_900.uid  # pyrefly: ignore[missing-attribute]
            with pytest.raises(dc.WriteDeniedError, match="ownership is immutable"):
                store.commit()
            store.discard()

    def test_root_may_reassign_ownership(self, store: dc.Store) -> None:
        """Given the staff owner's committed contact
        When root assigns the curator as owner and commits
        Then the contact's owner is the curator, a break-glass chown
        """
        with store.acting_as(OWNER_STAFF):
            contact = Contact(name="handover")
            store.store(contact)
            store.commit()
        with store.acting_as(ROOT):
            contact.dc_permissions = dc.Permissions(  # pyrefly: ignore[missing-attribute]
                owner=CURATOR.uid,
                groups=(TEAM,),
                read_floor=dc.VIEWER,
                write_floor=dc.STAFF,
            )
            store.commit()
            owner_after = store.query(CONTACT.name == "handover")[0]._dc_owner
        assert owner_after == CURATOR.uid

    def test_same_owner_relabel_is_not_a_chown(self, store: dc.Store) -> None:
        """Given the staff owner's committed contact shared with the team
        When the owner reassigns dc_permissions keeping the same owner and commits
        Then the commit succeeds, a normal re-share
        """
        with store.acting_as(OWNER_STAFF):
            contact = Contact(name="relabel")
            store.store(contact)
            dc.share(contact, TEAM, read=dc.VIEWER, write=dc.STAFF)
            store.commit()
            contact.dc_permissions = dc.Permissions(  # pyrefly: ignore[missing-attribute]
                owner=OWNER_STAFF.uid,
                groups=(TEAM,),
                read_floor=dc.VIEWER,
                write_floor=dc.STAFF,
            )
            committed = store.commit()
        assert committed is not None

    def test_same_owner_relabel_keeps_the_owner(self, store: dc.Store) -> None:
        """Given the staff owner's committed contact shared with the team
        When the owner reassigns dc_permissions keeping the same owner and commits
        Then the contact's owner is still the staff owner
        """
        with store.acting_as(OWNER_STAFF):
            contact = Contact(name="relabel")
            store.store(contact)
            dc.share(contact, TEAM, read=dc.VIEWER, write=dc.STAFF)
            store.commit()
            contact.dc_permissions = dc.Permissions(  # pyrefly: ignore[missing-attribute]
                owner=OWNER_STAFF.uid,
                groups=(TEAM,),
                read_floor=dc.VIEWER,
                write_floor=dc.STAFF,
            )
            store.commit()
            owner_after = store.query(CONTACT.name == "relabel")[0]._dc_owner
        assert owner_after == OWNER_STAFF.uid


class TestInheritanceBaseline:
    """The gate baseline for a NEW record is its birth/inherited labels, not empty (review fix).

    Inheritance is the library's policy — the container was already shared by someone with
    standing — so the acting principal is answerable only for what it stages ON TOP of it.
    """

    def test_pure_inheritance_into_unheld_group_passes(self, store: dc.Store) -> None:
        """Given a parent shared with TEAM and ORG, and an agent holding TEAM only
        When the agent links a new kid to the parent and commits
        Then the kid inherits both groups
        """
        owner = dc.Principal(uid=3, memberships={TEAM: dc.CURATOR, ORG: dc.CURATOR})
        with store.acting_as(owner):
            parent = Contact(name="parent")
            store.store(parent)
            dc.share(parent, TEAM, read=dc.VIEWER, write=dc.AGENT)
            dc.share(parent, ORG, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
        with store.acting_as(AGENT_900):
            parent.org = "dirtied"
            parent.link = dc.Lazy.of(Contact(name="kid"))
            store.commit()
            kid = store.query(CONTACT.name == "kid")[0]
        assert set(kid._dc_groups) == {TEAM, ORG}

    def test_pure_inheritance_stamps_the_agent_as_owner(self, store: dc.Store) -> None:
        """Given a parent shared with TEAM and ORG, and an agent holding TEAM only
        When the agent links a new kid to the parent and commits
        Then the kid's owner is the agent
        """
        owner = dc.Principal(uid=3, memberships={TEAM: dc.CURATOR, ORG: dc.CURATOR})
        with store.acting_as(owner):
            parent = Contact(name="parent")
            store.store(parent)
            dc.share(parent, TEAM, read=dc.VIEWER, write=dc.AGENT)
            dc.share(parent, ORG, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
        with store.acting_as(AGENT_900):
            parent.org = "dirtied"
            parent.link = dc.Lazy.of(Contact(name="kid"))
            store.commit()
            kid = store.query(CONTACT.name == "kid")[0]
        assert kid._dc_owner == AGENT_900.uid

    def test_explicit_share_beyond_inheritance_still_denied(self, store: dc.Store) -> None:
        """Given a parent shared with TEAM and an agent that shares a new kid into group 99
        When the agent commits
        Then the commit raises WriteDeniedError saying no standing
        """
        owner = dc.Principal(uid=3, memberships={TEAM: dc.CURATOR, ORG: dc.CURATOR})
        with store.acting_as(owner):
            parent = Contact(name="p3")
            store.store(parent)
            dc.share(parent, TEAM, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
        foreign_group = 99
        with store.acting_as(AGENT_900):
            parent.org = "d"
            kid = Contact(name="k3")
            parent.link = dc.Lazy.of(kid)
            dc.share(kid, foreign_group, read=dc.VIEWER, write=dc.VIEWER)
            with pytest.raises(dc.WriteDeniedError, match="no standing"):
                store.commit()
            store.discard()

    def test_explicit_floor_raise_on_new_record_denied(self, store: dc.Store) -> None:
        """Given an agent sharing its new contact with a curator write floor
        When the agent commits
        Then the commit raises WriteDeniedError naming the ceiling
        """
        with store.acting_as(AGENT_900):
            contact = Contact(name="raise-me")
            store.store(contact)
            dc.share(contact, TEAM, read=dc.VIEWER, write=dc.CURATOR)
            with pytest.raises(dc.WriteDeniedError, match="ceiling"):
                store.commit()
            store.discard()

    def test_gapless_after_inheritance_baseline_denial(self, store: dc.Store) -> None:
        """Given a committed note and an agent whose admin-floor contact was denied and discarded
        When the owner commits another note
        Then its TID is the one right after the first note's, the denial burning none
        """
        with store.acting_as(OWNER_STAFF):
            store.store(OpenNote(seq=7))
            tid = store.commit()
        with store.acting_as(AGENT_900):
            contact = Contact(name="doomed-kid")
            store.store(contact)
            dc.protect(contact, write=dc.ADMIN)
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()
        with store.acting_as(OWNER_STAFF):
            store.store(OpenNote(seq=8))
            next_tid = store.commit()
        assert next_tid == tid + 1  # pyrefly: ignore[unsupported-operation]


class TestRoot:
    """Root (EXECUTIVE in WORLD) bypasses the gate and is stamped."""

    def test_root_reaches_owner_only_records(self, store: dc.Store) -> None:
        """Given a staff owner's owner-only contact nobody else can reach
        When root edits its org and commits
        Then the new org is stored
        """
        with store.acting_as(OWNER_STAFF):
            contact = Contact(name="orphan")
            store.store(contact)
            store.commit()
        with store.acting_as(ROOT):
            contact.org = "rescued"
            store.commit()
            org_after = store.query(CONTACT.name == "orphan")[0].org
        assert org_after == "rescued"

    def test_root_bypasses_floor_and_ceiling_and_is_stamped(self, store: dc.Store) -> None:
        """Given a curated contact and a delta consumer attached mid-life
        When root edits it, protects it above anyone's ceiling and shares it into an unheld group
        Then the one delta carries root's uid as actor, the break-glass being visible
        """
        contact = _curated_contact(store)
        recorder = _DeltaRecorder(watermark=store.last_tid)
        store.attach(recorder)
        with store.acting_as(ROOT):
            contact.org = "root-edit"
            dc.protect(contact, write=dc.EXECUTIVE)
            dc.share(contact, ORG, read=dc.VIEWER, write=dc.EXECUTIVE)
            store.commit()
        assert recorder.stamps == [ROOT.uid]

    def test_uid_zero_never_gets_the_bypass(self, store: dc.Store) -> None:
        """Given a curated contact and a uid-0 principal holding EXECUTIVE in WORLD
        When it edits the contact and commits
        Then the commit raises WriteDeniedError, R7a: anonymous is not root
        """
        contact = _curated_contact(store)
        with store.acting_as(FAKE_ROOT):
            contact.org = "impostor"
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()

    def test_root_principal_factory_matches_the_sentinel(self) -> None:
        """Given the root_principal factory
        When it builds uid 99
        Then it equals the ROOT sentinel, sugar over {WORLD: EXECUTIVE}
        """
        built = dc.root_principal(uid=99)
        assert built == ROOT

    def test_root_principal_factory_merges_extra_hats(self) -> None:
        """Given the root_principal factory
        When it builds uid 99 with a staff hat in TEAM
        Then the memberships hold the hat and the world executive level
        """
        merged = dc.root_principal(uid=99, memberships={TEAM: dc.STAFF})
        assert merged.memberships == {TEAM: dc.STAFF, dc.WORLD: dc.EXECUTIVE}

    def test_root_principal_factory_acts_as_root(self, store: dc.Store) -> None:
        """Given a principal built by the root_principal factory
        When it stores an owner-only contact and commits
        Then it can look the contact up, the break-glass behavior being the same
        """
        with store.acting_as(dc.root_principal(uid=99)):
            store.store(Contact(name="via-factory"))
            store.commit()
            found = store.get(Contact, name="via-factory")
        assert found is not None


class TestActorAuthorityCeiling:
    """An Actor's memberships ARE authority, so minting one is bounded by the committer's level.

    F1 (ADR-008 R8, 2026-07-16): without this, any writer mints itself Actor(WORLD:EXECUTIVE)
    and acting_as() into root — the whole ladder becomes advisory.
    """

    def test_lowly_agent_cannot_mint_itself_root(self, store: dc.Store) -> None:
        """Given a bot actor holding AGENT in TEAM
        When the bot stores a new actor with EXECUTIVE in the world and commits
        Then the commit raises WriteDeniedError saying only root may mint root
        """
        _register_bot_agent(store, {TEAM: dc.AGENT})
        with store.acting_as(900):
            store.store(
                dc.Actor(uid=999, display="evil", human=True, memberships={dc.WORLD: dc.EXECUTIVE})
            )
            with pytest.raises(dc.WriteDeniedError, match="only root may mint root"):
                store.commit()
            store.discard()

    def test_denied_root_mint_never_commits_the_actor(self, store: dc.Store) -> None:
        """Given a bot whose attempt to mint a root actor was denied and discarded
        When root looks the evil actor up
        Then there is none
        """
        _register_bot_agent(store, {TEAM: dc.AGENT})
        with store.acting_as(900):
            store.store(
                dc.Actor(uid=999, display="evil", human=True, memberships={dc.WORLD: dc.EXECUTIVE})
            )
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()
        with store.acting_as(ROOT):
            found = store.get(dc.Actor, uid=999)
        assert found is None

    def test_agent_cannot_mint_above_its_own_level(self, store: dc.Store) -> None:
        """Given a bot actor holding AGENT in TEAM
        When the bot stores a new actor with CURATOR in TEAM and commits
        Then the commit raises WriteDeniedError saying grants are capped by what you hold
        """
        _register_bot_agent(store, {TEAM: dc.AGENT})
        with store.acting_as(900):
            store.store(
                dc.Actor(uid=901, display="promoted", human=True, memberships={TEAM: dc.CURATOR})
            )
            with pytest.raises(dc.WriteDeniedError, match="capped by what you hold"):
                store.commit()
            store.discard()

    def test_agent_cannot_grant_into_a_group_it_lacks(self, store: dc.Store) -> None:
        """Given a bot actor holding CURATOR in TEAM and no standing in ORG
        When the bot stores a new actor with VIEWER in ORG and commits
        Then the commit raises WriteDeniedError
        """
        _register_bot_agent(store, {TEAM: dc.CURATOR})
        with store.acting_as(900):
            store.store(
                dc.Actor(uid=901, display="cross", human=True, memberships={ORG: dc.VIEWER})
            )
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()

    def test_curator_may_onboard_a_bot_within_its_own_group(self, store: dc.Store) -> None:
        """Given Anna, a curator in TEAM
        When Anna registers a bot with AGENT in TEAM
        Then acting as the bot yields memberships of AGENT in TEAM, no break-glass needed
        """
        with store.acting_as(ROOT):
            store.store(
                dc.Actor(uid=2, display="Anna", human=True, memberships={TEAM: dc.CURATOR})
            )
            store.commit()
        with store.acting_as(2):
            store.store(
                dc.Actor(
                    uid=900, display="bot", human=False, sponsor=2, memberships={TEAM: dc.AGENT}
                )
            )
            store.commit()
        with store.acting_as(900) as bot:
            bot_memberships = bot.memberships
        assert bot_memberships == {TEAM: dc.AGENT}

    def test_relevel_within_the_ceiling_commits_and_unchanged_hats_pass(
        self, store: dc.Store
    ) -> None:
        """Given Anna, CURATOR in TEAM and STAFF in ORG, who registered a bot with AGENT in TEAM
        When Anna raises the bot to STAFF in TEAM, within her ceiling, with ORG:STAFF unchanged
        Then the bot's TEAM level is STAFF
        """
        with store.acting_as(ROOT):
            store.store(
                dc.Actor(
                    uid=2, display="Anna", human=True, memberships={TEAM: dc.CURATOR, ORG: dc.STAFF}
                )
            )
            store.commit()
        with store.acting_as(2):
            store.store(
                dc.Actor(
                    uid=900,
                    display="bot",
                    human=True,
                    memberships={TEAM: dc.AGENT, ORG: dc.STAFF},
                )
            )
            store.commit()
            store.query(ACTOR.uid == 900)[0].memberships[TEAM] = dc.STAFF
            store.commit()
            level_after = store.query(ACTOR.uid == 900)[0].memberships[TEAM]
        assert level_after == dc.STAFF

    def test_relevel_above_the_ceiling_is_denied(self, store: dc.Store) -> None:
        """Given Anna, CURATOR in TEAM, who registered a bot with AGENT in TEAM
        When Anna raises the bot to ADMIN in TEAM and commits
        Then the commit raises WriteDeniedError saying grants are capped by what you hold
        """
        with store.acting_as(ROOT):
            store.store(
                dc.Actor(uid=2, display="Anna", human=True, memberships={TEAM: dc.CURATOR})
            )
            store.commit()
        with store.acting_as(2):
            store.store(
                dc.Actor(uid=900, display="bot", human=True, memberships={TEAM: dc.AGENT})
            )
            store.commit()
            store.query(ACTOR.uid == 900)[0].memberships[TEAM] = dc.ADMIN
            with pytest.raises(dc.WriteDeniedError, match="capped by what you hold"):
                store.commit()
            store.discard()

    def test_minting_denial_is_gapless(self, store: dc.Store) -> None:
        """Given a bot whose attempt to mint a root actor was denied and discarded
        When root registers another actor
        Then its TID is the one right after the last commit before the denial
        """
        _register_bot_agent(store, {TEAM: dc.AGENT})
        before = store.last_tid
        with store.acting_as(900):
            store.store(
                dc.Actor(uid=999, display="evil", human=True, memberships={dc.WORLD: dc.EXECUTIVE})
            )
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()
        with store.acting_as(ROOT):
            store.store(dc.Actor(uid=901, display="fine", human=True, memberships={TEAM: dc.AGENT}))
            store.commit()
        assert store.last_tid == before + 1


class TestBirthOwnerPin:
    """A new record's owner is the acting principal's uid, stamped by the engine.

    F2 (ADR-008 R6, 2026-07-16): never assigned by hand. The persisted branch already pinned
    ownership; this closes the birth asymmetry.
    """

    def test_cannot_forge_a_victim_owner_at_birth(self, store: dc.Store) -> None:
        """Given an agent storing a new contact whose owner column it set to uid 2
        When the agent commits
        Then the commit raises WriteDeniedError saying the owner is the acting principal
        """
        with store.acting_as(AGENT_900):
            contact = Contact(name="planted")
            store.store(contact)
            contact._dc_owner = 2  # pyrefly: ignore[missing-attribute]
            with pytest.raises(dc.WriteDeniedError, match="owner is the acting"):
                store.commit()
            store.discard()

    def test_cannot_forge_a_nobody_owner_at_birth(self, store: dc.Store) -> None:
        """Given an agent storing a new contact whose owner column it set to nobody
        When the agent commits
        Then the commit raises WriteDeniedError, an unreachable orphan being refused
        """
        with store.acting_as(AGENT_900):
            contact = Contact(name="orphan")
            store.store(contact)
            contact._dc_owner = 0  # pyrefly: ignore[missing-attribute]
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
            store.discard()

    def test_honest_birth_still_stamps_the_actor(self, store: dc.Store) -> None:
        """Given an agent storing a new contact
        When the agent commits
        Then the contact's owner is the agent's uid
        """
        with store.acting_as(AGENT_900):
            contact = Contact(name="honest")
            store.store(contact)
            store.commit()
            owner = contact.dc_permissions.owner  # pyrefly: ignore[missing-attribute]
        assert owner == AGENT_900.uid


class TestOwnerBoostIsUnsharedOnly:
    """The owner's personal-best boost applies only to an UNSHARED record.

    F3 (ADR-008 amendment 2026-07-16): once shared, authority must come through a group it is
    shared into — an owner's level in an unrelated compartment must not clear a curated floor.
    """

    @pytest.mark.parametrize("owner_name", ["with-an-unrelated-hat", "without-it"])
    def test_unrelated_hat_does_not_clear_a_curated_floor(
        self, store: dc.Store, owner_name: str
    ) -> None:
        """Given two owners identical but for a curator hat in an unrelated group
        When each owner edits their own draft after the curator raised its floor and commits
        Then both commits raise WriteDeniedError naming the curation guarantee, no laundering
        """
        owners = {
            "with-an-unrelated-hat": dc.Principal(
                uid=20, memberships={TEAM: dc.AGENT, ORG: dc.CURATOR}
            ),
            "without-it": dc.Principal(uid=21, memberships={TEAM: dc.AGENT}),
        }
        owner = owners[owner_name]
        with store.acting_as(owner):
            draft = Contact(name="draft")
            store.store(draft)
            dc.share(draft, TEAM, read=dc.VIEWER, write=dc.AGENT)
            store.commit()
        with store.acting_as(CURATOR):
            dc.protect(draft, write=dc.CURATOR)
            store.commit()
        with store.acting_as(owner):
            draft.org = "overwritten"
            with pytest.raises(dc.WriteDeniedError, match="curation guarantee"):
                store.commit()
            store.discard()

    def test_owner_of_an_unshared_draft_can_still_ratchet_it(self, store: dc.Store) -> None:
        """Given an owner-only draft by an ORG curator
        When the owner edits its org and ratchets its write floor to curator, then commits
        Then the edit is stored, the boost's legitimate purpose surviving
        """
        owner = dc.Principal(uid=22, memberships={ORG: dc.CURATOR})
        with store.acting_as(owner):
            draft = Contact(name="private")
            store.store(draft)
            store.commit()
            draft.org = "edited"
            dc.protect(draft, write=dc.CURATOR)
            store.commit()
            org_after = store.query(CONTACT.name == "private")[0].org
        assert org_after == "edited"


class TestDenialMechanics:
    """A denial burns no TID, keeps its buffer and is never retried."""

    def test_denial_burns_no_tid(self, store: dc.Store) -> None:
        """Given a committed note and an agent whose edit of a curated contact was denied
        When the curator commits the same buffered change
        Then its TID is the one right after the note's (invariant 5)
        """
        contact = _curated_contact(store)
        with store.acting_as(OWNER_STAFF):
            store.store(OpenNote(seq=1))
            tid_before = store.commit()
        with store.acting_as(AGENT_900):
            contact.org = "denied"
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
        with store.acting_as(CURATOR):
            tid_after = store.commit()
        assert tid_after == tid_before + 1  # pyrefly: ignore[unsupported-operation]

    def test_denial_leaves_the_buffer_intact(self, store: dc.Store) -> None:
        """Given an agent whose edit of a curated contact was denied
        When the curator commits the same buffered change
        Then the stored org is the agent's buffered edit
        """
        contact = _curated_contact(store)
        with store.acting_as(AGENT_900):
            contact.org = "denied"
            with pytest.raises(dc.WriteDeniedError):
                store.commit()
        with store.acting_as(CURATOR):
            store.commit()
            org_after = store.query(CONTACT.name == "Meyer")[0].org
        assert org_after == "denied"

    def test_committing_never_retries_a_denial(self, store: dc.Store) -> None:
        """Given an agent running a committing() attempt that edits a curated contact
        When the attempt exits its with block
        Then WriteDeniedError propagates out of the first attempt, deterministic and not retried
        """
        contact = _curated_contact(store)
        attempt = next(store.committing(retries=5))
        with store.acting_as(AGENT_900), pytest.raises(dc.WriteDeniedError), attempt:
            contact.org = "loop-edit"
        store.discard()

    def test_committing_denial_propagates_out_of_the_first_attempt(self, store: dc.Store) -> None:
        """Given a committing() loop unrolled by hand to its first attempt
        When an agent edits a curated contact inside that attempt
        Then the WriteDeniedError propagates out of the block, the buffered edit left intact
        """
        contact = _curated_contact(store)
        attempts = store.committing(retries=5)
        with store.acting_as(AGENT_900), pytest.raises(dc.WriteDeniedError), next(attempts):
            contact.org = "loop-edit"
        store.discard()
        assert contact.org == "loop-edit"

    def test_verify_is_never_gated(self, store: dc.Store) -> None:
        """Given a store holding a curated contact
        When it is verified by an anonymous session
        Then it reports nothing, verify being read-only for any principal
        """
        _curated_contact(store)
        problems = store.verify()
        assert problems == []

    def test_unprotected_commits_do_zero_gate_io(self) -> None:
        """Given five committed notes of an unprotected class over a counting backend
        When the owner edits all five and commits
        Then the backend served no load_many
        """
        counting = _Counting(MemoryBackend())
        store = dc.Store._from_backend(counting)  # pyrefly: ignore[bad-argument-type]
        with store.acting_as(OWNER_STAFF):
            first, second, third = OpenNote(seq=1), OpenNote(seq=2), OpenNote(seq=3)
            store.store(first)
            store.store(second)
            store.store(third)
            store.commit()
            counting.reset()
            first.text = "edited"
            second.text = "edited"
            third.text = "edited"
            store.commit()
        assert counting.load_calls == 0
        store.close()

    def test_gate_and_delta_priors_share_one_load(self) -> None:
        """Given a curated contact on a counting backend and a delta consumer attached
        When the curator edits the contact and commits
        Then the one protected prior was loaded exactly once for the gate and the delta
        """
        counting = _Counting(MemoryBackend())
        store = dc.Store._from_backend(counting)  # pyrefly: ignore[bad-argument-type]
        contact = _curated_contact(store)
        counting.reset()
        store.attach(_DeltaRecorder(watermark=store.last_tid))
        with store.acting_as(CURATOR):
            contact.org = "watched edit"
            store.commit()
        assert counting.records_loaded == 1
        store.close()

    def test_async_denial_fires_before_p2(self) -> None:
        """Given a committed contact on a counting backend and an agent staging an executive floor
        When the agent commits through an AsyncStore
        Then the commit raises WriteDeniedError
        And the backend applied nothing more, P2 never running
        """
        counting = _Counting(MemoryBackend())
        store = dc.Store._from_backend(counting)  # pyrefly: ignore[bad-argument-type]
        with store.acting_as(OWNER_STAFF):
            contact = Contact(name="async-fence")
            store.store(contact)
            store.commit()
        applies_before = counting.applies
        with store.acting_as(AGENT_900):
            dc.protect(contact, write=dc.EXECUTIVE)
            with pytest.raises(dc.WriteDeniedError):
                asyncio.run(_commit_through_async_store(store))
        assert counting.applies == applies_before
