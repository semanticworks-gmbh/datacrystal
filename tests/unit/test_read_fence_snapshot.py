"""W4-1/2/3: the read fence on the snapshot tier (ADR-008, epic #168, R15).

``store.snapshot(principal=...)`` binds ONE acting principal (R15); the
expensive per-watermark state lives on a shared ``_SnapshotCore`` and
``Snapshot.for_principal`` derives O(1) siblings over it, each enforcing its
OWN principal. Discovery surfaces (``query``/``count``/``all``/``explain``/
``incoming``) intersect a lazily-compiled ``readable_bitmap`` BEFORE any
window/order/hydration (R12, filter never error); deref surfaces
(``get``/``get_many``/``open_blob``) check ``can_read_row`` at decode level —
``get`` raises, ``get_many`` returns the redacted twin (R14, carried onto the
DTO tier). ``index_bitmaps()`` refuses on protected classes (R12); a persisted
``_dc_*`` class with no live code fails closed for non-root (W4-5); blobs are
fenced with their owning record (W4-3). Mirrors the live-store fence tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import TYPES_BY_NAME, type_info

StoreFactory = Callable[[], dc.Store]

TEAM = 5
OTHER = 6

OWNER = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
MEMBER = dc.Principal(uid=3, memberships={TEAM: dc.AGENT})
OUTSIDER = dc.Principal(uid=4, memberships={OTHER: dc.CURATOR})
ANON = dc.Principal(uid=0)
ROOT = dc.Principal(uid=99, memberships={dc.WORLD: dc.EXECUTIVE})

PRINCIPALS = {"owner": OWNER, "member": MEMBER, "outsider": OUTSIDER, "root": ROOT}


@dc.entity(protected=True)
class Dossier:
    """A protected dossier."""

    label: Annotated[str, dc.Unique]
    note: str = ""


@dc.entity(protected=True)
class Vault:
    """A protected record with an out-of-line scan."""

    label: Annotated[str, dc.Unique]
    scan: Annotated[bytes | None, dc.Blob] = None


@dc.entity
class Plain:
    """An unprotected record."""

    label: Annotated[str, dc.Unique]
    n: int = 0


@dc.entity(protected=True)
class Memo:
    """A protected memo that refers to a plain record (R11: the ref is Lazy)."""

    label: Annotated[str, dc.Unique]
    about: dc.Lazy[Plain] | None = None


def _seed_shared(store: dc.Store) -> tuple[int, int]:
    """Store a team-readable dossier (floor AGENT) and an owner-only one; return their OIDs."""
    with store.acting_as(OWNER):
        shared = Dossier(label="shared", note="team")
        shared_oid = store.store(shared)
        dc.share(shared, TEAM, read=dc.AGENT, write=dc.CURATOR)
        secret = Dossier(label="secret", note="owner-only")
        secret_oid = store.store(secret)
        store.commit()
    return shared_oid, secret_oid


def _commit_vault(store_factory: StoreFactory) -> int:
    """Commit one owner-only vault, close the store and return the vault's OID."""
    store = store_factory()
    with store.acting_as(OWNER):
        vault_oid = store.store(Vault(label="v", scan=b"top-secret-bytes"))
        store.commit()
    store.close()
    return vault_oid


# --- R15: one shared core, per-principal handles -----------------------------


def test_for_principal_derives_siblings_over_the_same_core(store: dc.Store) -> None:
    """Given a snapshot bound to the owner
    When siblings are derived for a member and an outsider
    Then both siblings share the owner snapshot's core, for_principal building nothing
    """
    _seed_shared(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        member_snap = owner_snap.for_principal(MEMBER)
        outsider_snap = owner_snap.for_principal(OUTSIDER)
        assert member_snap._core is owner_snap._core
        assert outsider_snap._core is owner_snap._core


def test_for_principal_binds_the_sibling_to_its_own_principal(store: dc.Store) -> None:
    """Given a snapshot bound to the owner
    When a sibling is derived for the member
    Then the sibling's principal is the member
    """
    _seed_shared(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        member_snap = owner_snap.for_principal(MEMBER)
        assert member_snap.principal is MEMBER


def test_owner_snapshot_queries_both_dossiers(store: dc.Store) -> None:
    """Given a team-shared dossier and an owner-only dossier
    When the owner's snapshot queries dossiers
    Then it sees both
    """
    _seed_shared(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        found = owner_snap.query(Dossier)
    assert {v.label for v in found} == {"shared", "secret"}


def test_member_sibling_queries_only_the_shared_dossier(store: dc.Store) -> None:
    """Given a team-shared dossier and an owner-only dossier
    When a member's sibling of the owner's snapshot queries dossiers
    Then it sees only the shared one
    """
    _seed_shared(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        found = owner_snap.for_principal(MEMBER).query(Dossier)
    assert {v.label for v in found} == {"shared"}


def test_outsider_sibling_queries_no_dossier(store: dc.Store) -> None:
    """Given a team-shared dossier and an owner-only dossier
    When an outsider's sibling of the owner's snapshot queries dossiers
    Then it sees none
    """
    _seed_shared(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        found = owner_snap.for_principal(OUTSIDER).query(Dossier)
    assert found == []


def test_a_second_query_on_the_same_outsider_snapshot_still_sees_no_dossier(
    store: dc.Store,
) -> None:
    """Given a team-shared dossier and an owner-only dossier
    And an outsider's snapshot that already counted the dossiers
    When the same snapshot queries dossiers
    Then it sees none, the cached readable set still fencing
    """
    _seed_shared(store)
    with store.snapshot(principal=OUTSIDER) as outsider_snap:
        outsider_snap.count(Dossier)

        found = outsider_snap.query(Dossier)

    assert found == []


def test_snapshot_binds_the_acting_principal_by_default(store: dc.Store) -> None:
    """Given a store acted on as the member
    When a snapshot is taken without naming a principal
    Then the snapshot is bound to the member (R15, bind at snapshot() time)
    """
    _seed_shared(store)
    with store.acting_as(MEMBER):
        snap = store.snapshot()
    with snap:
        assert snap.principal == MEMBER


def test_default_bound_snapshot_enforces_the_acting_principal(store: dc.Store) -> None:
    """Given a snapshot taken without a principal while acting as the member
    When the snapshot queries dossiers
    Then it sees only the shared one
    """
    _seed_shared(store)
    with store.acting_as(MEMBER):
        snap = store.snapshot()
    with snap:
        found = snap.query(Dossier)
    assert {v.label for v in found} == {"shared"}


def test_explicit_principal_overrides_the_acting_one(store: dc.Store) -> None:
    """Given a store acted on as an outsider
    When a snapshot is taken naming the owner
    Then the snapshot sees both dossiers
    """
    _seed_shared(store)
    with store.acting_as(OUTSIDER):
        snap = store.snapshot(principal=OWNER)
    with snap:
        found = snap.query(Dossier)
    assert {v.label for v in found} == {"shared", "secret"}


# --- discovery filters (R12) -------------------------------------------------


@pytest.mark.parametrize(
    ("principal_name", "expected_count"),
    [("owner", 2), ("member", 1), ("outsider", 0), ("root", 2)],
)
def test_count_is_over_the_readable_subset(
    store: dc.Store, principal_name: str, expected_count: int
) -> None:
    """Given a team-shared dossier and an owner-only dossier
    When a snapshot sibling for a principal counts dossiers
    Then the count is the number that principal can read
    """
    _seed_shared(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        counted = owner_snap.for_principal(PRINCIPALS[principal_name]).count(Dossier)
    assert counted == expected_count


def test_all_filters_and_never_pins_a_denied_row_in_a_page(store: dc.Store) -> None:
    """Given a team-shared dossier and an owner-only dossier
    When the member's snapshot lists all dossiers with a limit of five
    Then the one readable row comes back, no denied row consuming a slot
    """
    _seed_shared(store)
    with store.snapshot(principal=MEMBER) as member_snap:
        rows = member_snap.all(Dossier, limit=5)
    assert {v.label for v in rows} == {"shared"}


@pytest.mark.parametrize(
    ("principal_name", "expected_extent"),
    [("owner", 2), ("member", 1), ("outsider", 0), ("root", 2)],
)
def test_explain_extent_is_filtered(
    store: dc.Store, principal_name: str, expected_extent: int
) -> None:
    """Given a team-shared dossier and an owner-only dossier
    When a snapshot sibling for a principal explains the dossier extent
    Then the extent is the number of dossiers that principal can read
    """
    _seed_shared(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        extent = owner_snap.for_principal(PRINCIPALS[principal_name]).explain(Dossier).extent
    assert extent == expected_extent


def test_incoming_of_a_readable_target_without_referrers_is_empty(store: dc.Store) -> None:
    """Given a team-shared dossier that nothing references
    When the member's snapshot asks for its incoming referrers
    Then there are none
    """
    with store.acting_as(OWNER):
        target = Dossier(label="target")
        target_oid = store.store(target)
        dc.share(target, TEAM, read=dc.AGENT, write=dc.CURATOR)
        store.commit()
    with store.snapshot(principal=MEMBER) as member_snap:
        referrers = member_snap.incoming(target_oid)
    assert referrers == []


def _seed_memos_about_a_plain_record(store: dc.Store) -> int:
    """Store a plain record with a team-shared memo and an owner-only memo about it; return its OID."""
    with store.acting_as(OWNER):
        target = Plain(label="target")
        target_oid = store.store(target)
        shared = Memo(label="shared-memo", about=dc.Lazy.of(target))
        store.store(shared)
        dc.share(shared, TEAM, read=dc.AGENT, write=dc.CURATOR)
        store.store(Memo(label="secret-memo", about=dc.Lazy.of(target)))
        store.commit()
    return target_oid


def test_incoming_drops_a_referrer_the_principal_cannot_read(store: dc.Store) -> None:
    """Given a plain record referred to by a team-shared memo and an owner-only memo
    When the member's snapshot asks for the record's incoming referrers
    Then only the shared memo comes back
    """
    target_oid = _seed_memos_about_a_plain_record(store)
    with store.snapshot(principal=MEMBER) as member_snap:
        referrers = member_snap.incoming(target_oid)
    assert [v.label for v in referrers] == ["shared-memo"]


def test_incoming_keeps_every_referrer_for_the_owner(store: dc.Store) -> None:
    """Given a plain record referred to by a team-shared memo and an owner-only memo
    When the owner's snapshot asks for the record's incoming referrers
    Then both memos come back
    """
    target_oid = _seed_memos_about_a_plain_record(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        referrers = owner_snap.incoming(target_oid)
    assert {v.label for v in referrers} == {"shared-memo", "secret-memo"}


def test_incoming_for_a_sibling_drops_what_the_owner_already_cached(store: dc.Store) -> None:
    """Given the owner's snapshot has already listed both memos into the shared cache
    When the outsider's sibling on the same core asks for the incoming referrers
    Then none comes back
    """
    target_oid = _seed_memos_about_a_plain_record(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        owner_snap.incoming(target_oid)
        referrers = owner_snap.for_principal(OUTSIDER).incoming(target_oid)
    assert referrers == []


# --- _stream: root-bound for a protected class (docs/reference.md) -----------


def test_stream_refuses_a_non_root_on_a_protected_class(store: dc.Store) -> None:
    """Given a team-shared dossier and an owner-only dossier
    When the member's snapshot streams the dossiers for a mirror bootstrap
    Then the stream raises ReadDeniedError rather than yield a partial mirror
    """
    _seed_shared(store)
    typename = type_info(Dossier).typename
    with store.snapshot(principal=MEMBER) as member_snap, pytest.raises(dc.ReadDeniedError):
        list(member_snap._stream(typename))


def test_stream_yields_every_row_of_a_protected_class_to_root(store: dc.Store) -> None:
    """Given a team-shared dossier and an owner-only dossier
    When the root snapshot streams the dossiers
    Then both rows stream
    """
    _seed_shared(store)
    typename = type_info(Dossier).typename
    with store.snapshot(principal=ROOT) as root_snap:
        streamed = list(root_snap._stream(typename))
    assert len(streamed) == 2


def test_stream_yields_every_row_of_an_unprotected_class_to_a_non_root(store: dc.Store) -> None:
    """Given two plain records
    When an outsider's snapshot streams them
    Then both rows stream, no fence applying to an unprotected class
    """
    with store.acting_as(OWNER):
        store.store(Plain(label="p1"))
        store.store(Plain(label="p2"))
        store.commit()
    with store.snapshot(principal=OUTSIDER) as outsider_snap:
        streamed = list(outsider_snap._stream(type_info(Plain).typename))
    assert len(streamed) == 2


# --- deref: get raises, get_many twins (R14) ---------------------------------


def test_get_returns_a_readable_row(store: dc.Store) -> None:
    """Given a team-shared dossier
    When the member's snapshot gets it by OID
    Then the dossier is returned with its label
    """
    shared_oid, _secret_oid = _seed_shared(store)
    with store.snapshot(principal=MEMBER) as member_snap:
        found = member_snap.get(shared_oid)
    assert found.label == "shared"


def test_get_raises_read_denied_on_a_denied_row(store: dc.Store) -> None:
    """Given an owner-only dossier
    When the member's snapshot gets it by OID
    Then the strict deref raises ReadDeniedError
    """
    _shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=MEMBER) as member_snap, pytest.raises(dc.ReadDeniedError):
        member_snap.get(secret_oid)


def test_get_many_returns_the_readable_row_itself(store: dc.Store) -> None:
    """Given a team-shared dossier and an owner-only dossier
    When the member's snapshot gets both by OID
    Then the first is the shared dossier
    """
    shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=MEMBER) as member_snap:
        readable, _twin = member_snap.get_many([shared_oid, secret_oid])
    assert readable.label == "shared"  # pyrefly: ignore[missing-attribute]


def test_get_many_returns_a_redacted_entity_view_on_denial(store: dc.Store) -> None:
    """Given a team-shared dossier and an owner-only dossier
    When the member's snapshot gets both by OID
    Then the second is a Redacted twin
    And the twin is still an EntityView on the DTO tier
    """
    shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=MEMBER) as member_snap:
        _readable, twin = member_snap.get_many([shared_oid, secret_oid])
    assert isinstance(twin, dc.Redacted)
    assert isinstance(twin, dc.EntityView)


def test_redacted_twin_keeps_its_oid_readable(store: dc.Store) -> None:
    """Given an owner-only dossier
    When the member's snapshot gets it through get_many
    Then the twin's oid is the dossier's, traversal staying graceful (R14)
    """
    _shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=MEMBER) as member_snap:
        [twin] = member_snap.get_many([secret_oid])
    assert twin.oid == secret_oid  # pyrefly: ignore[missing-attribute]


def test_redacted_twin_raises_on_a_data_field(store: dc.Store) -> None:
    """Given an owner-only dossier
    When the member's snapshot gets it through get_many and reads its note
    Then the read raises ReadDeniedError
    """
    _shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=MEMBER) as member_snap:
        [twin] = member_snap.get_many([secret_oid])
    with pytest.raises(dc.ReadDeniedError):
        _ = twin.note  # pyrefly: ignore[missing-attribute]


def test_get_many_keeps_a_readable_row_in_place_among_absent_and_denied(
    store: dc.Store,
) -> None:
    """Given a team-shared dossier, an absent OID and an owner-only dossier
    When the member's snapshot gets all three in that order
    Then the first is the shared dossier
    """
    shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=MEMBER) as member_snap:
        found = member_snap.get_many([shared_oid, 10**12, secret_oid])
    assert found[0].label == "shared"  # pyrefly: ignore[missing-attribute]


def test_get_many_leaves_a_truly_absent_oid_as_none(store: dc.Store) -> None:
    """Given a team-shared dossier, an absent OID and an owner-only dossier
    When the member's snapshot gets all three in that order
    Then the second is None
    """
    shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=MEMBER) as member_snap:
        found = member_snap.get_many([shared_oid, 10**12, secret_oid])
    assert found[1] is None


def test_get_many_turns_a_denied_oid_into_a_twin_between_other_rows(store: dc.Store) -> None:
    """Given a team-shared dossier, an absent OID and an owner-only dossier
    When the member's snapshot gets all three in that order
    Then the third is a Redacted twin
    """
    shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=MEMBER) as member_snap:
        found = member_snap.get_many([shared_oid, 10**12, secret_oid])
    assert isinstance(found[2], dc.Redacted)


def test_root_gets_the_owner_only_dossier(store: dc.Store) -> None:
    """Given an owner-only dossier
    When the root snapshot gets it by OID
    Then the dossier carries its note
    """
    _shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=ROOT) as root_snap:
        found = root_snap.get(secret_oid)
    assert found.note == "owner-only"


def test_root_get_many_returns_the_real_dossier(store: dc.Store) -> None:
    """Given an owner-only dossier
    When the root snapshot gets it through get_many
    Then it is not a Redacted twin
    """
    _shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=ROOT) as root_snap:
        [only] = root_snap.get_many([secret_oid])
    assert not isinstance(only, dc.Redacted)


def test_owner_gets_the_owner_only_dossier(store: dc.Store) -> None:
    """Given an owner-only dossier
    When the owner's snapshot gets it by OID
    Then the dossier carries its note
    """
    _shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        found = owner_snap.get(secret_oid)
    assert found.note == "owner-only"


def test_sibling_get_raises_after_the_owner_cached_the_view(store: dc.Store) -> None:
    """Given the owner's snapshot has materialized the owner-only dossier into the shared cache
    When the member's sibling on the same core gets it by OID
    Then the get raises ReadDeniedError
    """
    _shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        owner_snap.get(secret_oid)
        member_snap = owner_snap.for_principal(MEMBER)
        with pytest.raises(dc.ReadDeniedError):
            member_snap.get(secret_oid)


def test_sibling_get_many_twins_after_the_owner_cached_the_view(store: dc.Store) -> None:
    """Given the owner's snapshot has materialized the owner-only dossier into the shared cache
    When the member's sibling on the same core gets it through get_many
    Then it is a Redacted twin
    """
    _shared_oid, secret_oid = _seed_shared(store)
    with store.snapshot(principal=OWNER) as owner_snap:
        owner_snap.get(secret_oid)
        [twin] = owner_snap.for_principal(MEMBER).get_many([secret_oid])
    assert isinstance(twin, dc.Redacted)


# --- index_bitmaps refuses on protected (R12) --------------------------------


@pytest.mark.parametrize("principal_name", ["owner", "outsider", "root"])
def test_index_bitmaps_raises_on_a_protected_class(
    store: dc.Store, principal_name: str
) -> None:
    """Given a protected dossier class with stored rows
    When a snapshot of any principal, root included, asks for its index bitmaps
    Then the ask raises ReadDeniedError
    """
    _seed_shared(store)
    with (
        store.snapshot(principal=PRINCIPALS[principal_name]) as snap,
        pytest.raises(dc.ReadDeniedError),
    ):
        snap.index_bitmaps(Dossier)


def test_index_bitmaps_still_serves_an_unprotected_class(store: dc.Store) -> None:
    """Given one stored record of an unprotected class
    When an outsider's snapshot asks for its index bitmaps
    Then the extent holds one row
    """
    with store.acting_as(OWNER):
        store.store(Plain(label="p1", n=1))
        store.commit()
    with store.snapshot(principal=OUTSIDER) as outsider_snap:
        bitmaps = outsider_snap.index_bitmaps(Plain)
    assert len(bitmaps.extent) == 1


# --- no-live-class fail-closed (W4-5) ----------------------------------------


def test_all_by_name_raises_when_the_protected_class_has_no_live_code(
    store: dc.Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given persisted protected dossiers whose live class was removed
    When the member's snapshot lists them by type name
    Then the listing raises ReadDeniedError
    """
    _seed_shared(store)
    typename = type_info(Dossier).typename
    monkeypatch.delitem(TYPES_BY_NAME, typename)
    with store.snapshot(principal=MEMBER) as member_snap, pytest.raises(dc.ReadDeniedError):
        member_snap.all(typename)


def test_get_raises_when_the_protected_class_has_no_live_code(
    store: dc.Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a persisted protected dossier whose live class was removed
    When the member's snapshot gets it by OID
    Then the get raises ReadDeniedError
    """
    shared_oid, _secret_oid = _seed_shared(store)
    monkeypatch.delitem(TYPES_BY_NAME, type_info(Dossier).typename)
    with store.snapshot(principal=MEMBER) as member_snap, pytest.raises(dc.ReadDeniedError):
        member_snap.get(shared_oid)


def test_get_many_fails_closed_with_none_when_the_protected_class_has_no_live_code(
    store: dc.Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given persisted protected dossiers whose live class was removed
    When the member's snapshot gets both through get_many
    Then both come back None, no twin being buildable without a live class
    """
    shared_oid, secret_oid = _seed_shared(store)
    monkeypatch.delitem(TYPES_BY_NAME, type_info(Dossier).typename)
    with store.snapshot(principal=MEMBER) as member_snap:
        found = member_snap.get_many([shared_oid, secret_oid])
    assert found == [None, None]


def test_stream_refuses_a_non_root_when_the_protected_class_has_no_live_code(
    store: dc.Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given persisted protected dossiers whose live class was removed
    When the member's snapshot streams them by type name
    Then the stream raises ReadDeniedError, guarding against a silently partial mirror
    """
    _seed_shared(store)
    typename = type_info(Dossier).typename
    monkeypatch.delitem(TYPES_BY_NAME, typename)
    with store.snapshot(principal=MEMBER) as member_snap, pytest.raises(dc.ReadDeniedError):
        list(member_snap._stream(typename))


def test_root_lists_all_rows_when_the_protected_class_has_no_live_code(
    store: dc.Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given persisted protected dossiers whose live class was removed
    When the root snapshot lists them by type name
    Then both rows come back, root running ops dumps (R9)
    """
    _seed_shared(store)
    typename = type_info(Dossier).typename
    monkeypatch.delitem(TYPES_BY_NAME, typename)
    with store.snapshot(principal=ROOT) as root_snap:
        rows = root_snap.all(typename)
    assert len(rows) == 2


def test_root_gets_by_oid_when_the_protected_class_has_no_live_code(
    store: dc.Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a persisted protected dossier whose live class was removed
    When the root snapshot gets it by OID
    Then the dossier carries its label
    """
    shared_oid, _secret_oid = _seed_shared(store)
    monkeypatch.delitem(TYPES_BY_NAME, type_info(Dossier).typename)
    with store.snapshot(principal=ROOT) as root_snap:
        found = root_snap.get(shared_oid)
    assert found.label == "shared"


def test_root_streams_all_rows_when_the_protected_class_has_no_live_code(
    store: dc.Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given persisted protected dossiers whose live class was removed
    When the root snapshot streams them by type name
    Then both rows stream
    """
    _seed_shared(store)
    typename = type_info(Dossier).typename
    monkeypatch.delitem(TYPES_BY_NAME, typename)
    with store.snapshot(principal=ROOT) as root_snap:
        streamed = list(root_snap._stream(typename))
    assert len(streamed) == 2


# --- blobs fenced with their owning record (W4-3) ----------------------------


def test_snapshot_open_blob_reads_for_the_owner(store_factory: StoreFactory) -> None:
    """Given an owner-only vault with a scan
    When the owner's snapshot opens the scan blob and reads it
    Then it reads the scan bytes
    """
    vault_oid = _commit_vault(store_factory)
    with (
        store_factory() as store,
        store.snapshot(principal=OWNER) as owner_snap,
        owner_snap.open_blob(vault_oid, "scan") as fh,
    ):
        scan = fh.read()
    assert scan == b"top-secret-bytes"


def test_snapshot_open_blob_is_denied_to_an_outsider(store_factory: StoreFactory) -> None:
    """Given an owner-only vault with a scan
    When an outsider's snapshot opens the scan blob
    Then the open raises ReadDeniedError
    """
    vault_oid = _commit_vault(store_factory)
    with (
        store_factory() as store,
        store.snapshot(principal=OUTSIDER) as outsider_snap,
        pytest.raises(dc.ReadDeniedError),
    ):
        outsider_snap.open_blob(vault_oid, "scan")


def test_live_blob_handle_reads_bytes_for_the_owner(store_factory: StoreFactory) -> None:
    """Given a reopened store holding an owner-only vault
    When the owner reads the scan through its blob handle
    Then the handle returns the scan bytes
    """
    vault_oid = _commit_vault(store_factory)
    with store_factory() as store, store.acting_as(OWNER):
        handle = store.get_many([vault_oid])[0].scan
        scanned = handle.bytes()
    assert scanned == b"top-secret-bytes"


def test_live_blob_handle_regates_for_an_outsider_after_the_owner_cached_it(
    store_factory: StoreFactory,
) -> None:
    """Given a blob handle whose bytes the owner already read and cached
    When an outsider reads bytes through the same handle
    Then the read raises ReadDeniedError, the same handle being re-gated
    """
    vault_oid = _commit_vault(store_factory)
    with store_factory() as store:
        with store.acting_as(OWNER):
            handle = store.get_many([vault_oid])[0].scan
            handle.bytes()
        with store.acting_as(OUTSIDER), pytest.raises(dc.ReadDeniedError):
            handle.bytes()


def test_live_blob_handle_reads_bytes_for_root_after_a_denial(
    store_factory: StoreFactory,
) -> None:
    """Given a blob handle the owner cached and an outsider was denied
    When root reads bytes through the same handle
    Then the handle returns the scan bytes
    """
    vault_oid = _commit_vault(store_factory)
    with store_factory() as store:
        with store.acting_as(OWNER):
            handle = store.get_many([vault_oid])[0].scan
            handle.bytes()
        with store.acting_as(ROOT):
            scanned = handle.bytes()
    assert scanned == b"top-secret-bytes"


def test_store_open_blob_reads_for_the_owner(store_factory: StoreFactory) -> None:
    """Given a reopened store holding an owner-only vault
    When the owner opens the scan blob and reads it
    Then it reads the scan bytes
    """
    vault_oid = _commit_vault(store_factory)
    with store_factory() as store, store.acting_as(OWNER):
        vault = store.get_many([vault_oid])[0]
        with store.open_blob(vault, "scan") as fh:
            scan = fh.read()
    assert scan == b"top-secret-bytes"


def test_store_open_blob_is_denied_to_an_outsider(store_factory: StoreFactory) -> None:
    """Given a reopened store holding an owner-only vault
    When an outsider opens the scan blob
    Then the open raises ReadDeniedError
    """
    vault_oid = _commit_vault(store_factory)
    with store_factory() as store:
        with store.acting_as(OWNER):
            vault = store.get_many([vault_oid])[0]
        with store.acting_as(OUTSIDER), pytest.raises(dc.ReadDeniedError):
            store.open_blob(vault, "scan")
