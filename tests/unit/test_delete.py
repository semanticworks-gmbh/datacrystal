"""store.delete(): unchecked deletes per ADR-003.

Covers the API shapes (instance / unique key), buffering through the one
commit path, idempotency, the DELETED lifecycle barrier, precedence over
buffered writes, unique-key reuse, index/unique-map maintenance, physical
removal across reopen, tombstone delta emission (verified by the strict
reference applier), dangling-ref loudness, and the root recovery path.
"""

from __future__ import annotations

import threading

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of
from datacrystal.contract import ReferenceApplier
from tests.conftest import Locality, Mineral


def _quartz() -> Mineral:
    return Mineral(qid="Q43010", name="quartz", crystal_system="trigonal", mohs=7.0)


def _azurite() -> Mineral:
    return Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic", mohs=3.8)


# -- API shapes and idempotency ---------------------------------------------


def test_delete_by_instance_removes_the_record(store):
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    assert store.delete(quartz) is True
    store.commit()
    assert store.get(Mineral, qid="Q43010") is None
    assert store.query(dc.fields(Mineral).crystal_system == "trigonal") == []


def test_delete_by_unique_key_needs_no_instance(store_factory):
    store = store_factory()
    store.store(_quartz())
    store.commit()
    store.close()

    reopened = store_factory()  # nothing hydrated in this incarnation
    assert reopened.delete(Mineral, qid="Q43010") is True
    reopened.commit()
    assert reopened.get(Mineral, qid="Q43010") is None
    reopened.close()


def test_delete_is_idempotent_never_raises_on_miss(store):
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    assert store.delete(Mineral, qid="no-such-key") is False
    assert store.delete(quartz) is True
    assert store.delete(quartz) is False        # delete-twice ≡ delete-once
    assert store.delete(Mineral, qid="Q43010") is False  # buffered already
    store.commit()
    assert store.delete(Mineral, qid="Q43010") is False
    assert store.delete(Mineral(qid="x", name="unregistered")) is False


def test_delete_by_key_requires_a_unique_field(store):
    store.store(_quartz())
    store.commit()
    with pytest.raises(dc.QueryError, match="not a Unique field"):
        store.delete(Mineral, crystal_system="trigonal")
    with pytest.raises(TypeError, match="exactly one"):
        store.delete(Mineral, qid="Q43010", name="quartz")
    with pytest.raises(TypeError, match="no keyword"):
        store.delete(_quartz(), qid="Q43010")


def test_deleting_a_new_entity_cancels_the_pending_insert(store):
    quartz = _quartz()
    store.store(quartz)
    assert store.delete(quartz) is True
    assert store.commit() is None  # nothing left to commit
    assert store.get(Mineral, qid="Q43010") is None


# -- the DELETED lifecycle barrier ------------------------------------------


def test_deleted_instance_reads_work_writes_raise(store):
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    store.delete(quartz)
    assert quartz.name == "quartz"  # a detached plain object stays readable
    with pytest.raises(dc.DeletedEntityError):
        quartz.mohs = 6.5
    with pytest.raises(dc.DeletedEntityError):
        quartz.tags.append("doomed")  # containers share the write barrier
    store.commit()
    with pytest.raises(dc.DeletedEntityError):
        quartz.name = "phoenix"


def test_deleted_instance_cannot_be_stored_or_marked_again(store):
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    store.delete(quartz)
    with pytest.raises(dc.DeletedEntityError):
        store.store(quartz)
    with pytest.raises(dc.DeletedEntityError):
        store.mark_dirty(quartz)


def test_delete_by_key_write_bars_the_live_instance_after_commit(store):
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    assert store.delete(Mineral, qid="Q43010") is True
    store.commit()
    with pytest.raises(dc.DeletedEntityError):
        quartz.mohs = 1.0


def test_delete_wins_over_a_buffered_write(store):
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    quartz.mohs = 6.0  # buffered write...
    store.delete(quartz)  # ...the delete wins (ADR-003 precedence)
    store.commit()
    assert store.get(Mineral, qid="Q43010") is None


def test_frozen_entities_are_deletable(store):
    from tests.conftest import LogEntry

    entry = LogEntry(note="to be expunged")
    store.store(entry)
    store.commit()
    assert store.delete(entry) is True
    store.commit()
    assert store.query(dc.fields(LogEntry).kind == "misc") == []


# -- guards -------------------------------------------------------------------


def test_the_root_holder_cannot_be_deleted(store):
    store.root = [_quartz()]
    store.commit()
    holder_oid = store._root_oid
    holder = store._registry.get(holder_oid)
    assert holder is not None
    with pytest.raises(dc.DataCrystalError, match="root holder"):
        store.delete(holder)


def test_foreign_thread_delete_raises_before_any_buffering(store):
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    caught: list[BaseException] = []

    def attempt() -> None:
        try:
            store.delete(quartz)
        except BaseException as exc:  # noqa: BLE001 — recorded for assertion
            caught.append(exc)

    t = threading.Thread(target=attempt)
    t.start()
    t.join()
    assert len(caught) == 1 and isinstance(caught[0], dc.WrongThreadError)
    assert not store._deleted  # nothing was buffered


# -- unique keys and indexes ---------------------------------------------------


def test_unique_key_freed_by_delete_is_reusable_in_the_same_commit(store):
    old = _quartz()
    store.store(old)
    store.commit()
    store.delete(old)
    fresh = Mineral(qid="Q43010", name="quartz (resampled)")
    store.store(fresh)
    store.commit()  # no UniqueViolationError: the delete freed the key
    found = store.get(Mineral, qid="Q43010")
    assert found is fresh


def test_indexes_and_extent_forget_the_deleted_entity(store_factory):
    store = store_factory()
    quartz, azurite = _quartz(), _azurite()
    store.store(quartz)
    store.store(azurite)
    store.commit()
    M = dc.fields(Mineral)
    assert len(store.query(M.crystal_system.in_(["trigonal", "monoclinic"]))) == 2
    store.delete(quartz)
    store.commit()
    assert store.query(M.crystal_system.in_(["trigonal", "monoclinic"])) == [azurite]
    store.close()

    reopened = store_factory()  # a fresh index build scans post-delete records
    assert reopened.query(M.crystal_system == "trigonal") == []
    assert len(reopened.query(M.crystal_system == "monoclinic")) == 1
    reopened.close()


# -- physical removal and dangling references ----------------------------------


def test_deletion_survives_reopen(store_factory):
    store = store_factory()
    store.store(_quartz())
    store.commit()
    assert store.delete(Mineral, qid="Q43010") is True
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.get(Mineral, qid="Q43010") is None
    reopened.close()


def test_following_a_stale_lazy_ref_raises_dangling(store_factory):
    store = store_factory()
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    azurite = Mineral(qid="Q193563", name="azurite",
                      type_locality=dc.Lazy.of(tsumeb))
    store.store(azurite)
    store.commit()
    store.delete(tsumeb)
    store.commit()  # unchecked: azurite still points at the dead OID
    store.close()

    reopened = store_factory()
    survivor = reopened.get(Mineral, qid="Q193563")
    assert survivor is not None
    with pytest.raises(dc.DanglingRefError, match="stale"):
        survivor.type_locality.get()
    reopened.close()


def test_get_many_with_a_dead_oid_raises_dangling(store):
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    dead_oid = oid_of(quartz)
    store.delete(quartz)
    store.commit()
    with pytest.raises(dc.DanglingRefError):
        store.get_many([dead_oid])


def test_snapshot_of_a_dead_oid_raises_dangling(store):
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    dead_oid = oid_of(quartz)
    store.delete(quartz)
    store.commit()
    with store.snapshot() as snap:
        with pytest.raises(dc.DanglingRefError):
            snap.get(dead_oid)


def test_root_referencing_a_deleted_entity_recovers_via_assignment(store_factory):
    store = store_factory()
    quartz = _quartz()
    store.root = [quartz]
    store.commit()
    store.delete(quartz)
    store.commit()
    store.close()

    reopened = store_factory()
    with pytest.raises(dc.DanglingRefError, match="recovers"):
        _ = reopened.root
    reopened.root = []  # the documented recovery path (ADR-003)
    reopened.commit()
    assert reopened.root == []
    reopened.close()

    third = store_factory()  # the recovery is durable
    assert third.root == []
    third.close()


# -- the delta stream (COMMIT-DELTA-v1 §3.1) ------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.deltas: list[dict] = []
        self._watermark = 0

    @property
    def watermark(self) -> int:
        return self._watermark

    def apply(self, delta: dict) -> None:
        self.deltas.append(delta)
        self._watermark = delta["tid"]


def test_delete_emits_a_tombstone_the_strict_applier_accepts(store):
    # The reference applier verifies priors strictly and rejects deletes of
    # unknown OIDs — it passing IS the proof the engine emits per spec §3.1.
    applier = ReferenceApplier()
    collector = _Collector()
    store.attach(applier)
    store.attach(collector)

    quartz = _quartz()
    store.store(quartz)
    store.commit()
    doomed_oid = oid_of(quartz)
    assert doomed_oid in applier.objects
    last_payload = applier.objects[doomed_oid]

    quartz.mohs = 6.9  # an update in the same commit as a delete of another
    azurite = _azurite()
    store.store(azurite)
    store.commit()

    store.delete(Mineral, qid="Q43010")
    store.commit()
    assert doomed_oid not in applier.objects
    assert oid_of(azurite) in applier.objects

    tombstone_delta = collector.deltas[-1]
    (op,) = tombstone_delta["ops"]
    assert op["op"] == "delete"
    assert op["oid"] == doomed_oid
    assert op["payload"] is None
    assert op["prior"] is not None and op["prior"] != last_payload  # post-update prior


def test_delete_only_commit_advances_the_watermark(store):
    quartz = _quartz()
    store.store(quartz)
    tid_create = store.commit()
    store.delete(quartz)
    tid_delete = store.commit()
    assert tid_delete == tid_create + 1  # gapless, a real commit
    assert store.commit() is None
