"""store.delete(): unchecked deletes per ADR-003.

Covers the API shapes (instance / unique key), buffering through the one
commit path, idempotency, the DELETED lifecycle barrier, precedence over
buffered writes, unique-key reuse, index/unique-map maintenance, physical
removal across reopen, tombstone delta emission (verified by the strict
reference applier), dangling-ref loudness, and the root recovery path.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of
from datacrystal.contract import ReferenceApplier
from tests.conftest import Locality, LogEntry, Mineral

StoreFactory = Callable[[], dc.Store]
M = dc.fields(Mineral)


def _oid(entity: object) -> int:
    oid = oid_of(entity)
    if oid is None:
        raise LookupError("entity has no oid yet")
    return oid


def _quartz() -> Mineral:
    return Mineral(qid="Q43010", name="quartz", crystal_system="trigonal", mohs=7.0)


def _azurite() -> Mineral:
    return Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic", mohs=3.8)


@pytest.fixture
def committed_quartz(store: dc.Store) -> Mineral:
    """Commit quartz and return the live instance."""
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    return quartz


# -- API shapes and idempotency ---------------------------------------------


def test_delete_by_instance_reports_it_deleted(store: dc.Store, committed_quartz: Mineral) -> None:
    """Given committed quartz
    When quartz is deleted by instance
    Then delete returns True
    """
    deleted = store.delete(committed_quartz)

    assert deleted is True


def test_delete_by_instance_removes_the_record_by_key(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz deleted by instance and the store committed
    When quartz is fetched by its qid
    Then it is gone
    """
    store.delete(committed_quartz)
    store.commit()

    found = store.get(Mineral, qid="Q43010")

    assert found is None


def test_delete_by_instance_removes_the_record_from_queries(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed trigonal quartz deleted by instance and the store committed
    When crystal_system == "trigonal" is queried
    Then nothing matches
    """
    store.delete(committed_quartz)
    store.commit()

    hits = store.query(M.crystal_system == "trigonal")

    assert hits == []


def test_delete_by_unique_key_needs_no_instance(store_factory: StoreFactory) -> None:
    """Given committed quartz and a reopened store where nothing is hydrated
    When quartz is deleted by its qid and the store commits
    Then quartz is gone
    """
    store = store_factory()
    store.store(_quartz())
    store.commit()
    store.close()
    reopened = store_factory()
    reopened.delete(Mineral, qid="Q43010")
    reopened.commit()

    found = reopened.get(Mineral, qid="Q43010")

    assert found is None
    reopened.close()


def test_delete_by_unique_key_reports_it_deleted(store_factory: StoreFactory) -> None:
    """Given committed quartz and a reopened store where nothing is hydrated
    When quartz is deleted by its qid
    Then delete returns True
    """
    store = store_factory()
    store.store(_quartz())
    store.commit()
    store.close()
    reopened = store_factory()

    deleted = reopened.delete(Mineral, qid="Q43010")

    assert deleted is True
    reopened.close()


def test_delete_of_a_missing_key_is_false(store: dc.Store, committed_quartz: Mineral) -> None:
    """Given committed quartz
    When a qid that does not exist is deleted
    Then delete returns False and raises nothing
    """
    deleted = store.delete(Mineral, qid="no-such-key")

    assert deleted is False


def test_deleting_twice_is_the_same_as_deleting_once(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz already deleted by instance
    When quartz is deleted by instance again
    Then delete returns False
    """
    store.delete(committed_quartz)

    deleted_again = store.delete(committed_quartz)

    assert deleted_again is False


def test_deleting_by_key_what_is_already_buffered_is_false(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz already deleted by instance, not yet committed
    When quartz is deleted by its qid
    Then delete returns False
    """
    store.delete(committed_quartz)

    deleted_by_key = store.delete(Mineral, qid="Q43010")

    assert deleted_by_key is False


def test_deleting_by_key_what_is_already_committed_gone_is_false(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz deleted and the deletion committed
    When quartz is deleted by its qid
    Then delete returns False
    """
    store.delete(committed_quartz)
    store.commit()

    deleted_by_key = store.delete(Mineral, qid="Q43010")

    assert deleted_by_key is False


def test_deleting_an_unregistered_instance_is_false(store: dc.Store) -> None:
    """Given a Mineral that was never stored
    When it is deleted
    Then delete returns False
    """
    deleted = store.delete(Mineral(qid="x", name="unregistered"))

    assert deleted is False


def test_delete_by_a_non_unique_field_is_rejected(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz
    When delete is keyed by the non-unique crystal_system
    Then QueryError says it is not a Unique field
    """
    with pytest.raises(dc.QueryError, match="not a Unique field"):
        store.delete(Mineral, crystal_system="trigonal")


def test_delete_by_two_keys_is_rejected(store: dc.Store, committed_quartz: Mineral) -> None:
    """Given committed quartz
    When delete is keyed by both qid and name
    Then TypeError asks for exactly one key
    """
    with pytest.raises(TypeError, match="exactly one"):
        store.delete(Mineral, qid="Q43010", name="quartz")


def test_delete_of_an_instance_with_a_key_is_rejected(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz
    When delete is given an instance and also a keyword key
    Then TypeError says no keyword is allowed
    """
    with pytest.raises(TypeError, match="no keyword"):
        store.delete(_quartz(), qid="Q43010")


def test_deleting_a_new_entity_reports_it_deleted(store: dc.Store) -> None:
    """Given quartz stored but never committed
    When quartz is deleted
    Then delete returns True
    """
    quartz = _quartz()
    store.store(quartz)

    deleted = store.delete(quartz)

    assert deleted is True


def test_deleting_a_new_entity_leaves_nothing_to_commit(store: dc.Store) -> None:
    """Given quartz stored, never committed, then deleted
    When the store commits
    Then nothing is committed
    """
    quartz = _quartz()
    store.store(quartz)
    store.delete(quartz)

    tid = store.commit()

    assert tid is None


def test_deleting_a_new_entity_never_creates_it(store: dc.Store) -> None:
    """Given quartz stored, never committed, then deleted
    When quartz is fetched by its qid
    Then it is not found
    """
    quartz = _quartz()
    store.store(quartz)
    store.delete(quartz)

    found = store.get(Mineral, qid="Q43010")

    assert found is None


# -- the DELETED lifecycle barrier ------------------------------------------


def test_deleted_instance_stays_readable(store: dc.Store, committed_quartz: Mineral) -> None:
    """Given committed quartz deleted
    When its name is read
    Then it is "quartz", a detached plain object
    """
    store.delete(committed_quartz)

    name = committed_quartz.name

    assert name == "quartz"


def test_deleted_instance_rejects_field_writes(store: dc.Store, committed_quartz: Mineral) -> None:
    """Given committed quartz deleted
    When its mohs is assigned
    Then DeletedEntityError is raised
    """
    store.delete(committed_quartz)

    with pytest.raises(dc.DeletedEntityError):
        committed_quartz.mohs = 6.5


def test_deleted_instance_rejects_container_writes(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Containers share the write barrier.

    Given committed quartz deleted
    When "doomed" is appended to its tags
    Then DeletedEntityError is raised
    """
    store.delete(committed_quartz)

    with pytest.raises(dc.DeletedEntityError):
        committed_quartz.tags.append("doomed")


def test_deleted_instance_rejects_writes_after_the_commit(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz deleted and the deletion committed
    When its name is assigned
    Then DeletedEntityError is raised
    """
    store.delete(committed_quartz)
    store.commit()

    with pytest.raises(dc.DeletedEntityError):
        committed_quartz.name = "phoenix"


def test_deleted_instance_cannot_be_stored_again(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz deleted
    When quartz is stored
    Then DeletedEntityError is raised
    """
    store.delete(committed_quartz)

    with pytest.raises(dc.DeletedEntityError):
        store.store(committed_quartz)


def test_deleted_instance_cannot_be_marked_dirty(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz deleted
    When quartz is marked dirty
    Then DeletedEntityError is raised
    """
    store.delete(committed_quartz)

    with pytest.raises(dc.DeletedEntityError):
        store.mark_dirty(committed_quartz)


def test_delete_by_key_reports_it_deleted(store: dc.Store, committed_quartz: Mineral) -> None:
    """Given committed quartz
    When quartz is deleted by its qid
    Then delete returns True
    """
    deleted = store.delete(Mineral, qid="Q43010")

    assert deleted is True


def test_delete_by_key_write_bars_the_live_instance_after_commit(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz deleted by its qid and the deletion committed
    When the live instance's mohs is assigned
    Then DeletedEntityError is raised
    """
    store.delete(Mineral, qid="Q43010")
    store.commit()

    with pytest.raises(dc.DeletedEntityError):
        committed_quartz.mohs = 1.0


def test_delete_wins_over_a_buffered_write(store: dc.Store, committed_quartz: Mineral) -> None:
    """The ADR-003 precedence: a buffered write loses to the delete.

    Given committed quartz with mohs 6.0 assigned and then quartz deleted
    When the store commits
    Then quartz is not found
    """
    committed_quartz.mohs = 6.0
    store.delete(committed_quartz)
    store.commit()

    found = store.get(Mineral, qid="Q43010")

    assert found is None


@pytest.fixture
def committed_entry(store: dc.Store) -> LogEntry:
    """Commit a frozen LogEntry and return it."""
    entry = LogEntry(note="to be expunged")
    store.store(entry)
    store.commit()
    return entry


def test_frozen_entity_delete_reports_it_deleted(store: dc.Store, committed_entry: LogEntry) -> None:
    """Given a committed frozen LogEntry
    When it is deleted
    Then delete returns True
    """
    deleted = store.delete(committed_entry)

    assert deleted is True


def test_frozen_entity_is_gone_after_the_commit(store: dc.Store, committed_entry: LogEntry) -> None:
    """Given a committed frozen LogEntry deleted and the deletion committed
    When kind == "misc" is queried
    Then nothing matches
    """
    store.delete(committed_entry)
    store.commit()

    hits = store.query(dc.fields(LogEntry).kind == "misc")

    assert hits == []


# -- guards -------------------------------------------------------------------


def test_the_root_holder_cannot_be_deleted(store: dc.Store) -> None:
    """Given a committed root list
    When the root holder is deleted
    Then DataCrystalError says it is the root holder
    """
    store.root = [_quartz()]
    store.commit()
    assert store._root_oid is not None
    holder = store._registry.get(store._root_oid)
    assert holder is not None

    with pytest.raises(dc.DataCrystalError, match="root holder"):
        store.delete(holder)


def test_foreign_thread_delete_raises_wrong_thread(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz
    When a foreign thread deletes it
    Then WrongThreadError is raised, before anything is buffered
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        attempt = pool.submit(store.delete, committed_quartz)

        with pytest.raises(dc.WrongThreadError):
            attempt.result(timeout=10)


def test_foreign_thread_delete_buffers_nothing(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given a foreign thread's rejected delete of committed quartz
    When the owner commits
    Then nothing is committed
    And quartz is still found
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        attempt = pool.submit(store.delete, committed_quartz)
        with pytest.raises(dc.WrongThreadError):
            attempt.result(timeout=10)

    tid = store.commit()

    assert tid is None
    assert store.get(Mineral, qid="Q43010") is committed_quartz


# -- unique keys and indexes ---------------------------------------------------


def test_unique_key_freed_by_delete_is_reusable_in_the_same_commit(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """The delete freed the key, so no UniqueViolationError.

    Given committed quartz deleted
    When a new Mineral with quartz's qid is stored and the store commits
    Then fetching that qid returns the new instance
    """
    store.delete(committed_quartz)
    fresh = Mineral(qid="Q43010", name="quartz (resampled)")
    store.store(fresh)
    store.commit()

    found = store.get(Mineral, qid="Q43010")

    assert found is fresh


@pytest.fixture
def cabinet_without_quartz(store_factory: StoreFactory) -> tuple[StoreFactory, dc.Store, Mineral]:
    """Commit quartz (trigonal) and azurite (monoclinic), delete quartz, commit, and return."""
    store = store_factory()
    quartz, azurite = _quartz(), _azurite()
    store.store(quartz)
    store.store(azurite)
    store.commit()
    store.delete(quartz)
    store.commit()
    return store_factory, store, azurite


def test_query_forgets_the_deleted_entity(
    cabinet_without_quartz: tuple[StoreFactory, dc.Store, Mineral],
) -> None:
    """Given quartz (trigonal) and azurite (monoclinic) committed and quartz then deleted
    When crystal_system in ["trigonal", "monoclinic"] is queried
    Then only azurite matches
    """
    _, store, azurite = cabinet_without_quartz

    hits = store.query(M.crystal_system.in_(["trigonal", "monoclinic"]))

    assert hits == [azurite]
    store.close()


def test_reopened_index_forgets_the_deleted_entity(
    cabinet_without_quartz: tuple[StoreFactory, dc.Store, Mineral],
) -> None:
    """A fresh index build scans post-delete records.

    Given quartz and azurite committed and quartz then deleted, and the store reopened
    When crystal_system == "trigonal" is queried
    Then nothing matches
    """
    store_factory, store, _ = cabinet_without_quartz
    store.close()
    reopened = store_factory()

    hits = reopened.query(M.crystal_system == "trigonal")

    assert hits == []
    reopened.close()


def test_reopened_index_keeps_the_surviving_entity(
    cabinet_without_quartz: tuple[StoreFactory, dc.Store, Mineral],
) -> None:
    """Given quartz and azurite committed and quartz then deleted, and the store reopened
    When crystal_system == "monoclinic" is queried
    Then one mineral matches
    """
    store_factory, store, _ = cabinet_without_quartz
    store.close()
    reopened = store_factory()

    hits = reopened.query(M.crystal_system == "monoclinic")

    assert len(hits) == 1
    reopened.close()


# -- physical removal and dangling references ----------------------------------


def test_deletion_survives_reopen(store_factory: StoreFactory) -> None:
    """Given committed quartz deleted by its qid and the deletion committed
    When the store is reopened and quartz is fetched by its qid
    Then it is not found
    """
    store = store_factory()
    store.store(_quartz())
    store.commit()
    store.delete(Mineral, qid="Q43010")
    store.commit()
    store.close()
    reopened = store_factory()

    found = reopened.get(Mineral, qid="Q43010")

    assert found is None
    reopened.close()


def test_following_a_stale_lazy_ref_raises_dangling(store_factory: StoreFactory) -> None:
    """Deletes are unchecked: azurite still points at the dead OID.

    Given azurite lazily pointing at Tsumeb Mine, which was then deleted, and the store reopened
    When the stale type_locality is fetched
    Then DanglingRefError says the reference is stale
    """
    store = store_factory()
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.store(Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(tsumeb)))
    store.commit()
    store.delete(tsumeb)
    store.commit()
    store.close()
    reopened = store_factory()
    survivor = reopened.get(Mineral, qid="Q193563")
    assert survivor is not None
    assert survivor.type_locality is not None

    with pytest.raises(dc.DanglingRefError, match="stale"):
        survivor.type_locality.get()

    reopened.close()


def test_get_many_with_a_dead_oid_raises_dangling(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz deleted and the deletion committed
    When quartz's old oid is passed to get_many
    Then DanglingRefError is raised
    """
    dead_oid = _oid(committed_quartz)
    store.delete(committed_quartz)
    store.commit()

    with pytest.raises(dc.DanglingRefError):
        store.get_many([dead_oid])


def test_snapshot_of_a_dead_oid_raises_dangling(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz deleted and the deletion committed
    When quartz's old oid is read from a snapshot
    Then DanglingRefError is raised
    """
    dead_oid = _oid(committed_quartz)
    store.delete(committed_quartz)
    store.commit()

    with store.snapshot() as snap, pytest.raises(dc.DanglingRefError):
        snap.get(dead_oid)


@pytest.fixture
def root_with_deleted_entity(store_factory: StoreFactory) -> None:
    """Commit a root list holding quartz, then delete quartz and commit."""
    store = store_factory()
    quartz = _quartz()
    store.root = [quartz]
    store.commit()
    store.delete(quartz)
    store.commit()
    store.close()


@pytest.mark.usefixtures("root_with_deleted_entity")
def test_root_referencing_a_deleted_entity_raises_dangling(store_factory: StoreFactory) -> None:
    """Given a root list holding a deleted quartz, reopened
    When store.root is read
    Then DanglingRefError says assignment recovers
    """
    reopened = store_factory()

    with pytest.raises(dc.DanglingRefError, match="recovers"):
        _ = reopened.root

    reopened.close()


@pytest.mark.usefixtures("root_with_deleted_entity")
def test_root_assignment_recovers_from_a_deleted_entity(store_factory: StoreFactory) -> None:
    """The documented recovery path (ADR-003).

    Given a root list holding a deleted quartz, reopened
    When an empty list is assigned to store.root and committed
    Then store.root equals []
    """
    reopened = store_factory()
    reopened.root = []
    reopened.commit()

    root = reopened.root

    assert root == []
    reopened.close()


@pytest.mark.usefixtures("root_with_deleted_entity")
def test_root_recovery_is_durable(store_factory: StoreFactory) -> None:
    """Given a root recovered by assigning [] and committing, and the store closed
    When the store is reopened
    Then store.root equals []
    """
    recovering = store_factory()
    recovering.root = []
    recovering.commit()
    recovering.close()
    third = store_factory()

    root = third.root

    assert root == []
    third.close()


# -- the delta stream (COMMIT-DELTA-v1 section 3.1) -----------------------------


class _Collector:
    """A consumer keeping every delta."""

    def __init__(self) -> None:
        self.deltas: list[dict[str, Any]] = []
        self._watermark = 0

    @property
    def watermark(self) -> int:
        return self._watermark

    def apply(self, delta: dict[str, Any]) -> None:
        self.deltas.append(delta)
        self._watermark = delta["tid"]


class _Tombstoned:
    """What the tombstone scenario leaves behind for the tests to inspect."""

    def __init__(
        self,
        applier: ReferenceApplier,
        collector: _Collector,
        doomed_oid: int,
        azurite: Mineral,
        payload_before_update: bytes,
    ) -> None:
        self.applier = applier
        self.collector = collector
        self.doomed_oid = doomed_oid
        self.azurite = azurite
        self.payload_before_update = payload_before_update


@pytest.fixture
def tombstoned(store: dc.Store) -> _Tombstoned:
    """Stream quartz's create, its update with azurite's insert, then its delete.

    The reference applier verifies priors strictly and rejects deletes of unknown OIDs, so
    it passing is the proof the engine emits per spec section 3.1.
    """
    applier = ReferenceApplier()
    collector = _Collector()
    store.attach(applier)
    store.attach(collector)
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    doomed_oid = _oid(quartz)
    payload_before_update = applier.objects[doomed_oid]
    quartz.mohs = 6.9
    azurite = _azurite()
    store.store(azurite)
    store.commit()
    store.delete(Mineral, qid="Q43010")
    store.commit()
    return _Tombstoned(applier, collector, doomed_oid, azurite, payload_before_update)


def test_applier_drops_the_deleted_object(tombstoned: _Tombstoned) -> None:
    """Given a stream of create, update and delete of quartz
    When the strict reference applier has consumed it
    Then it no longer holds quartz
    """
    assert tombstoned.doomed_oid not in tombstoned.applier.objects


def test_applier_keeps_the_surviving_object(tombstoned: _Tombstoned) -> None:
    """Given a stream that inserted azurite and deleted quartz
    When the strict reference applier has consumed it
    Then it still holds azurite
    """
    assert oid_of(tombstoned.azurite) in tombstoned.applier.objects


def test_tombstone_delta_holds_one_delete_op(tombstoned: _Tombstoned) -> None:
    """Given a stream ending in the delete of quartz
    When the last delta is read
    Then it holds exactly one op
    And the op is a delete
    """
    tombstone_ops = tombstoned.collector.deltas[-1]["ops"]

    assert len(tombstone_ops) == 1
    assert tombstone_ops[0]["op"] == "delete"


def test_tombstone_op_names_the_deleted_oid_without_a_payload(tombstoned: _Tombstoned) -> None:
    """Given a stream ending in the delete of quartz
    When the tombstone op is read
    Then its oid is quartz's oid
    And its payload is None
    """
    (op,) = tombstoned.collector.deltas[-1]["ops"]

    assert op["oid"] == tombstoned.doomed_oid
    assert op["payload"] is None


def test_tombstone_prior_is_the_post_update_payload(tombstoned: _Tombstoned) -> None:
    """Given a stream where quartz was updated before it was deleted
    When the tombstone op is read
    Then its prior is present
    And it differs from the payload before the update
    """
    (op,) = tombstoned.collector.deltas[-1]["ops"]

    assert op["prior"] is not None
    assert op["prior"] != tombstoned.payload_before_update


def test_delete_only_commit_takes_the_next_tid(store: dc.Store, committed_quartz: Mineral) -> None:
    """Given committed quartz as the first TID
    When quartz is deleted and the store commits
    Then the TID is the first plus one, gapless
    """
    created = store.last_tid
    store.delete(committed_quartz)

    tid_delete = store.commit()

    assert tid_delete == created + 1


def test_delete_only_commit_leaves_nothing_pending(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz deleted and the deletion committed
    When the store commits again
    Then nothing is committed
    """
    store.delete(committed_quartz)
    store.commit()

    tid = store.commit()

    assert tid is None
