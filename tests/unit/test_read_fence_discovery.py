"""W3-3: filtering ``get``/``get_many``/``incoming`` + the write-path bypass
net (ADR-008, epic #168, issue #173).

``get()``/``get_many(cls, key=...)`` are DISCOVERY surfaces (R12):
found-but-denied is indistinguishable from absent, so a denied key is a
``None`` result/slot, never a leak of existence. ``get_many(iterable)``'s
OID/``Lazy``/``Ref`` items are a DEREF surface (R14) instead — you already
hold that reference, so a denied-but-existing item comes back as a
``dc.Redacted`` twin, mirroring ``lazy_ref.get()``. ``incoming()`` is
class-blind discovery over the reverse-reference index: a denied referrer
is silently absent, filtered at decode level with no extra backend I/O
beyond the hydration a readable referrer needed anyway. Root sees
everything on every surface. The full multi-principal × surface matrix
(including ``query``/``count``/``pluck``/``explain``) is W3-2/W3-5; this
file is W3-3's own proof: the two discovery accessors plus the identity-
bootstrap bypass (``_resolve_registry_actor``) that must survive it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import StoredRecord

StoreFactory = Callable[[], dc.Store]

TEAM = 5
OTHER_TEAM = 6

OWNER = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
MEMBER = dc.Principal(uid=3, memberships={TEAM: dc.AGENT})  # member-at-floor
OUTSIDER = dc.Principal(uid=4, memberships={OTHER_TEAM: dc.CURATOR})
ANON = dc.Principal(uid=0)
ROOT = dc.Principal(uid=99, memberships={dc.WORLD: dc.EXECUTIVE})

PRINCIPALS = {
    "owner": OWNER,
    "member-at-floor": MEMBER,
    "outsider": OUTSIDER,
    "anonymous": ANON,
    "root": ROOT,
}


@dc.entity(protected=True)
class Specimen:
    """A protected specimen."""

    label: Annotated[str, dc.Unique]
    note: str = ""


@dc.entity(protected=True)
class CuratorNote:
    """A protected referrer (R11: its ``about`` ref must be ``Lazy``)."""

    tag: Annotated[str, dc.Unique]
    about: dc.Lazy[Specimen] | None = None


@dc.entity
class FieldLog:
    """An UNPROTECTED referrer.

    R11 still forces its ref to ``Specimen`` to be ``Lazy`` (protected targets are
    Lazy-referable only, regardless of the referrer's own class).
    """

    tag: Annotated[str, dc.Unique]
    about: dc.Lazy[Specimen] | None = None


def _shared(store: dc.Store, label: str) -> Specimen:
    """Commit a Specimen owned by OWNER and shared to TEAM at read=VIEWER.

    Readable by OWNER (ownership), MEMBER (TEAM:AGENT >= VIEWER) and ROOT; denied to
    OUTSIDER and the anonymous principal.
    """
    with store.acting_as(OWNER):
        spec = Specimen(label=label)
        store.store(spec)
        dc.share(spec, TEAM, read=dc.VIEWER, write=dc.VIEWER)
        store.commit()
    return spec


def _commit_shared_and_owner_only(store: dc.Store, shared_label: str, hidden_label: str) -> None:
    with store.acting_as(OWNER):
        shared = Specimen(label=shared_label)
        store.store(shared)
        dc.share(shared, TEAM, read=dc.VIEWER, write=dc.VIEWER)
        store.store(Specimen(label=hidden_label))
        store.commit()


def _commit_hidden_specimen(store_factory: StoreFactory, label: str) -> int:
    """Commit an owner-only Specimen, close the store and return the Specimen's OID."""
    store = store_factory()
    with store.acting_as(OWNER):
        oid = store.store(Specimen(label=label))
        store.commit()
    store.close()
    return oid


def _commit_visible_specimen(store_factory: StoreFactory, label: str) -> int:
    """Commit a Specimen shared to TEAM, close the store and return the Specimen's OID."""
    store = store_factory()
    with store.acting_as(OWNER):
        visible = Specimen(label=label)
        oid = store.store(visible)
        dc.share(visible, TEAM, read=dc.VIEWER, write=dc.VIEWER)
        store.commit()
    store.close()
    return oid


def _referrer_corpus(store: dc.Store) -> Specimen:
    """Commit one target with three referrers, stored in this order.

    A protected one shared to TEAM (readable by MEMBER/ROOT), a protected owner-only one
    (readable only by OWNER/ROOT) and an unprotected one (readable by anyone). Ascending-OID
    order equals store-call order, which the order assertions check against.
    """
    with store.acting_as(OWNER):
        target = Specimen(label="target")
        store.store(target)

        visible_note = CuratorNote(tag="v-note", about=dc.Lazy.of(target))
        store.store(visible_note)
        dc.share(visible_note, TEAM, read=dc.VIEWER, write=dc.VIEWER)

        hidden_note = CuratorNote(tag="h-note", about=dc.Lazy.of(target))
        store.store(hidden_note)

        log = FieldLog(tag="log", about=dc.Lazy.of(target))
        store.store(log)

        store.commit()
    return target


class _CountingBackend:
    """The counting-storage-wrapper idiom (fitness #19 pattern).

    Counts ``load_many`` INVOCATIONS (not records), so a decode-level check that rides the
    same fetch a later hydration needed is indistinguishable from "no check happened" —
    exactly the property under test.
    """

    def __init__(self, inner: MemoryBackend) -> None:
        self._inner = inner
        self.load_calls = 0

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        out = self._inner.load_many(oids)
        self.load_calls += 1
        return out

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


# --- get(): found-but-denied is absent (R12) ----------------------------------


class TestGetFoundButDeniedEqAbsent:
    """A denied key looks the same as a missing key."""

    @pytest.mark.parametrize("principal_name", ["owner", "member-at-floor", "root"])
    def test_get_returns_the_specimen_to_a_principal_who_can_read_it(
        self, store: dc.Store, principal_name: str
    ) -> None:
        """Given a specimen shared to the team at viewer level
        When a principal who can read it gets it by label
        Then the specimen is returned
        """
        _shared(store, "S1")
        with store.acting_as(PRINCIPALS[principal_name]):
            got = store.get(Specimen, label="S1")
        assert got is not None

    @pytest.mark.parametrize("principal_name", ["outsider", "anonymous"])
    def test_get_returns_none_to_a_principal_who_cannot_read_it(
        self, store: dc.Store, principal_name: str
    ) -> None:
        """Given a specimen shared to the team at viewer level
        When a principal who cannot read it gets it by label
        Then the result is None
        """
        _shared(store, "S1")
        with store.acting_as(PRINCIPALS[principal_name]):
            got = store.get(Specimen, label="S1")
        assert got is None

    def test_denied_key_gets_the_same_result_as_a_truly_absent_key(
        self, store: dc.Store
    ) -> None:
        """Given a specimen an outsider cannot read
        When the outsider gets it by label and gets a label that never existed
        Then both results are None, the same observable outcome
        """
        _shared(store, "S2")
        with store.acting_as(OUTSIDER):
            denied = store.get(Specimen, label="S2")
            missing = store.get(Specimen, label="never-existed")
        assert denied is None
        assert missing is None


# --- get_many(cls, key=[...]): the aligned bulk twin (R12) -------------------


class TestGetManyKeyFormAlignment:
    """The bulk key form keeps one slot per key."""

    def test_get_many_returns_one_slot_per_key(self, store: dc.Store) -> None:
        """Given a team-shared specimen A, an owner-only specimen B and no specimen "nope"
        When the member gets A, B and "nope" by key
        Then three slots come back
        """
        _commit_shared_and_owner_only(store, "A", "B")
        with store.acting_as(MEMBER):
            got = store.get_many(Specimen, label=["A", "B", "nope"])
        assert len(got) == 3

    def test_get_many_fills_the_readable_slot(self, store: dc.Store) -> None:
        """Given a team-shared specimen A, an owner-only specimen B and no specimen "nope"
        When the member gets A, B and "nope" by key
        Then the first slot is A
        """
        _commit_shared_and_owner_only(store, "A", "B")
        with store.acting_as(MEMBER):
            got = store.get_many(Specimen, label=["A", "B", "nope"])
        assert got[0].label == "A"

    def test_get_many_leaves_a_denied_slot_none(self, store: dc.Store) -> None:
        """Given a team-shared specimen A, an owner-only specimen B and no specimen "nope"
        When the member gets A, B and "nope" by key
        Then the second slot is None, the member having no standing on B
        """
        _commit_shared_and_owner_only(store, "A", "B")
        with store.acting_as(MEMBER):
            got = store.get_many(Specimen, label=["A", "B", "nope"])
        assert got[1] is None

    def test_get_many_leaves_a_missing_key_slot_none(self, store: dc.Store) -> None:
        """Given a team-shared specimen A, an owner-only specimen B and no specimen "nope"
        When the member gets A, B and "nope" by key
        Then the third slot is None, a genuinely missing key
        """
        _commit_shared_and_owner_only(store, "A", "B")
        with store.acting_as(MEMBER):
            got = store.get_many(Specimen, label=["A", "B", "nope"])
        assert got[2] is None

    def test_root_sees_every_slot(self, store: dc.Store) -> None:
        """Given two owner-only specimens
        When root gets both by key
        Then both come back in key order
        """
        with store.acting_as(OWNER):
            store.store(Specimen(label="Aroot"))
            store.store(Specimen(label="Broot"))
            store.commit()
        with store.acting_as(ROOT):
            got = store.get_many(Specimen, label=["Aroot", "Broot"])
        assert [g.label for g in got] == ["Aroot", "Broot"]


# --- get_many(iterable): a DEREF surface — twins, not filtering (R14) --------


class TestGetManyDerefFormTwin:
    """The OID form returns a twin for a denied record and raises for a deleted one."""

    def test_denied_oid_returns_a_twin_of_the_class(self, store_factory: StoreFactory) -> None:
        """Given a reopened store holding an owner-only specimen
        When an outsider gets its OID through get_many
        Then the outsider gets a Redacted twin
        And the twin is a Specimen
        """
        oid = _commit_hidden_specimen(store_factory, "hidden")
        with store_factory() as reopened, reopened.acting_as(OUTSIDER):
            [twin] = reopened.get_many([oid])
        assert isinstance(twin, dc.Redacted)
        assert isinstance(twin, Specimen)

    def test_twin_refuses_to_reveal_the_label(self, store_factory: StoreFactory) -> None:
        """Given the twin an outsider got for an owner-only specimen
        When its label is read
        Then the read raises ReadDeniedError
        """
        oid = _commit_hidden_specimen(store_factory, "hidden")
        with store_factory() as reopened, reopened.acting_as(OUTSIDER):
            [twin] = reopened.get_many([oid])
        with pytest.raises(dc.ReadDeniedError):
            _ = twin.label

    def test_readable_oid_returns_the_real_instance(self, store_factory: StoreFactory) -> None:
        """Given a reopened store holding a team-shared specimen
        When a member gets its OID through get_many
        Then the member gets the real specimen, not a twin
        And it carries its label
        """
        oid = _commit_visible_specimen(store_factory, "visible")
        with store_factory() as reopened, reopened.acting_as(MEMBER):
            [got] = reopened.get_many([oid])
        assert not isinstance(got, dc.Redacted)
        assert got.label == "visible"

    def test_root_sees_the_real_instance(self, store_factory: StoreFactory) -> None:
        """Given a reopened store holding an owner-only specimen
        When root gets its OID through get_many
        Then root gets the real specimen, not a twin
        And it carries its label
        """
        oid = _commit_hidden_specimen(store_factory, "root-visible")
        with store_factory() as reopened, reopened.acting_as(ROOT):
            [got] = reopened.get_many([oid])
        assert not isinstance(got, dc.Redacted)
        assert got.label == "root-visible"

    def test_deleted_oid_still_raises_dangling(self, store: dc.Store) -> None:
        """Given a specimen that was committed and then deleted
        When an outsider gets its OID through get_many
        Then get_many raises DanglingRefError
        """
        with store.acting_as(OWNER):
            doomed = Specimen(label="doomed")
            oid = store.store(doomed)
            store.commit()
            store.delete(doomed)
            store.commit()
        with store.acting_as(OUTSIDER), pytest.raises(dc.DanglingRefError):
            store.get_many([oid])


# --- incoming(): class-blind discovery, decode-level filter (R12) -----------


class TestIncomingDecodeLevelFilter:
    """incoming() drops a denied referrer and keeps ascending-OID order."""

    def test_denied_referrer_is_absent_in_a_deterministic_order(self, store: dc.Store) -> None:
        """Given a target with a team-shared note, an owner-only note and an unprotected log
        When the member asks for the target's incoming referrers
        Then they are the shared note and the log, in store order, the owner-only note absent
        """
        target = _referrer_corpus(store)
        with store.acting_as(MEMBER):
            result = store.incoming(target)
        assert [x.tag for x in result] == ["v-note", "log"]

    def test_outsider_sees_only_the_unprotected_referrer(self, store: dc.Store) -> None:
        """Given a target with a team-shared note, an owner-only note and an unprotected log
        When the outsider asks for the target's incoming referrers
        Then only the log comes back
        """
        target = _referrer_corpus(store)
        with store.acting_as(OUTSIDER):
            result = store.incoming(target)
        assert [x.tag for x in result] == ["log"]

    def test_anonymous_sees_only_the_unprotected_referrer(self, store: dc.Store) -> None:
        """Given a target with a team-shared note, an owner-only note and an unprotected log
        When the ambient anonymous principal asks for the target's incoming referrers
        Then only the log comes back
        """
        target = _referrer_corpus(store)
        result = store.incoming(target)
        assert [x.tag for x in result] == ["log"]

    def test_root_sees_every_referrer_in_ascending_oid_order(self, store: dc.Store) -> None:
        """Given a target with a team-shared note, an owner-only note and an unprotected log
        When root asks for the target's incoming referrers
        Then all three come back in ascending OID order
        """
        target = _referrer_corpus(store)
        with store.acting_as(ROOT):
            result = store.incoming(target)
        assert [x.tag for x in result] == ["v-note", "h-note", "log"]


def _commit_referrers_through(counting: _CountingBackend) -> Specimen:
    store = dc.Store._from_backend(counting, principal=OWNER)  # pyrefly: ignore[bad-argument-type]
    with store.acting_as(OWNER):
        target = Specimen(label="target")
        store.store(target)
        visible_note = CuratorNote(tag="v-note", about=dc.Lazy.of(target))
        store.store(visible_note)
        dc.share(visible_note, TEAM, read=dc.VIEWER, write=dc.VIEWER)
        hidden_note = CuratorNote(tag="h-note", about=dc.Lazy.of(target))
        store.store(hidden_note)
        log = FieldLog(tag="log", about=dc.Lazy.of(target))
        store.store(log)
        store.commit()
    store.close()
    return target


def test_incoming_filters_denied_referrers_over_a_counting_backend() -> None:
    """Given a cold-registry store over a backend holding a target and three referrers
    When the member asks for the target's incoming referrers
    Then the shared note and the log come back
    """
    counting = _CountingBackend(MemoryBackend())
    target = _commit_referrers_through(counting)
    store = dc.Store._from_backend(counting)  # pyrefly: ignore[bad-argument-type]
    with store.acting_as(MEMBER):
        result = store.incoming(target)
    assert [x.tag for x in result] == ["v-note", "log"]
    store.close()


def test_incoming_is_decode_level_one_batched_load_no_extra_io() -> None:
    """Given a cold-registry store over a backend holding a target and three referrers
    When the member asks for the target's incoming referrers
    Then the backend served one load_many for all three referrers
    """
    counting = _CountingBackend(MemoryBackend())
    target = _commit_referrers_through(counting)
    store = dc.Store._from_backend(counting)  # pyrefly: ignore[bad-argument-type]
    counting.load_calls = 0
    with store.acting_as(MEMBER):
        store.incoming(target)
    assert counting.load_calls == 1
    store.close()


# --- the write-path bypass net (ADR-008 W3-3) --------------------------------


class TestActingAsBootstrapBypass:
    """Identity resolution reads past the fence; a normal get does not."""

    def test_fresh_owner_only_actor_row_resolves_from_an_anonymous_session(
        self, store: dc.Store
    ) -> None:
        """Given a freshly registered actor row with owner-only birth labels
        When acting_as is entered with its plain uid from an anonymous session
        Then the resolved principal carries that uid, _resolve_registry_actor reading unfenced
        """
        with store.acting_as(dc.Principal(uid=1)):
            store.store(dc.Actor(uid=42, display="Fresh Hire", human=True))
            store.commit()
        with store.acting_as(42) as resolved:
            resolved_uid = resolved.uid
        assert resolved_uid == 42

    def test_the_bypass_does_not_leak_through_a_normal_get(self, store: dc.Store) -> None:
        """Given a freshly registered actor row with owner-only birth labels
        When an outsider gets it with a fenced get
        Then the result is None, the bypass being scoped to identity resolution only
        """
        with store.acting_as(dc.Principal(uid=1)):
            store.store(dc.Actor(uid=43, display="Also Fresh", human=True))
            store.commit()
        with store.acting_as(OUTSIDER):
            got = store.get(dc.Actor, uid=43)
        assert got is None


class TestUpsertBypassIntact:
    """Upsert still dedups through an unfenced lookup, but fences what it returns."""

    def test_merge_returns_the_existing_specimen_for_the_owner(self, store: dc.Store) -> None:
        """Given an owner's committed specimen "upsert-owned" at note v1
        When the owner upserts a probe with the same label at note v2
        Then the survivor is the existing specimen
        """
        with store.acting_as(OWNER):
            spec = Specimen(label="upsert-owned", note="v1")
            store.store(spec)
            store.commit()
            survivor = store.upsert(Specimen(label="upsert-owned", note="v2"))
        assert survivor is spec

    def test_merge_updates_the_existing_specimens_note_for_the_owner(
        self, store: dc.Store
    ) -> None:
        """Given an owner's committed specimen "upsert-owned" at note v1
        When the owner upserts a probe with the same label at note v2 and commits
        Then the existing specimen's note is v2
        """
        with store.acting_as(OWNER):
            spec = Specimen(label="upsert-owned", note="v1")
            store.store(spec)
            store.commit()
            store.upsert(Specimen(label="upsert-owned", note="v2"))
            store.commit()
        assert spec.note == "v2"

    def test_outsider_get_of_an_owner_only_specimen_is_denied(self, store: dc.Store) -> None:
        """Given an owner-only specimen "upsert-hidden"
        When an outsider gets it by label
        Then the result is None
        """
        with store.acting_as(OWNER):
            store.store(Specimen(label="upsert-hidden", note="v1"))
            store.commit()
        with store.acting_as(OUTSIDER):
            got = store.get(Specimen, label="upsert-hidden")
        assert got is None

    def test_upsert_finds_the_hidden_row_but_the_return_is_denied(
        self, store: dc.Store
    ) -> None:
        """Given an owner-only specimen "upsert-hidden"
        When an outsider upserts a probe with the same label
        Then upsert raises ReadDeniedError, the lookup finding the row and the return being fenced
        """
        with store.acting_as(OWNER):
            store.store(Specimen(label="upsert-hidden", note="v1"))
            store.commit()
        with store.acting_as(OUTSIDER), pytest.raises(dc.ReadDeniedError):
            store.upsert(Specimen(label="upsert-hidden", note="tampered"))

    def test_denied_upsert_stages_nothing(self, store: dc.Store) -> None:
        """Given an outsider whose upsert of an owner-only specimen raised ReadDeniedError
        When the outsider commits
        Then there is nothing to commit, the failure being closed before any merge
        """
        with store.acting_as(OWNER):
            store.store(Specimen(label="upsert-hidden", note="v1"))
            store.commit()
        with store.acting_as(OUTSIDER):
            with pytest.raises(dc.ReadDeniedError):
                store.upsert(Specimen(label="upsert-hidden", note="tampered"))
            committed = store.commit()
        assert committed is None
