"""Three-phase commit scheduling (ADR-001 bound decision 2).

P2 runs off the owner thread and touches bytes only; a failed P2
compensates — captured entities return to their buffers, the TID is reused
(gapless sequence, invariant 5), and a retry leaves the store
indistinguishable from one whose first commit succeeded, *including* the
type-lineage rows (a failed first commit of a type must not orphan its
records on retry).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

import pytest

import datacrystal as dc
from datacrystal._ids import TID_BASE
from datacrystal._state import STATE_DIRTY, STATE_NEW
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import (
    BootInfo,
    CommitBatch,
    StorageReadView,
    StoredBlob,
    StoredRecord,
)
from tests.conftest import Mineral

StoreFactory = Callable[[], dc.Store]


class _Recording:
    """Backend wrapper recording which thread runs each apply()."""

    def __init__(self, inner: MemoryBackend) -> None:
        self._inner = inner
        self.apply_threads: list[int] = []

    def boot(self) -> BootInfo:
        return self._inner.boot()

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        return self._inner.load_many(oids)

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        return self._inner.scan_type(cid)

    def load_blob(self, oid: int) -> StoredBlob | None:
        return self._inner.load_blob(oid)

    def apply(self, batch: CommitBatch) -> None:
        self.apply_threads.append(threading.get_ident())
        self._inner.apply(batch)

    def read_view(self) -> StorageReadView:
        return self._inner.read_view()

    def close(self) -> None:
        self._inner.close()


class _FailNTimes(_Recording):
    """Recording backend whose next `failures` apply() calls raise OSError."""

    def __init__(self, inner: MemoryBackend, failures: int) -> None:
        super().__init__(inner)
        self.failures = failures

    def apply(self, batch: CommitBatch) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise OSError("injected P2 fault")
        super().apply(batch)


def test_first_commit_applies_off_the_owner_thread() -> None:
    """Given a store on a recording backend with one stored mineral
    When the store commits
    Then exactly one apply ran
    And it ran on a thread other than the owner's
    """
    backend = _Recording(MemoryBackend())
    owner = threading.get_ident()
    with dc.Store._from_backend(backend) as store:
        store.store(Mineral(qid="Q1", name="quartz"))

        store.commit()

    assert len(backend.apply_threads) == 1
    assert backend.apply_threads[0] != owner


def test_update_commit_applies_off_the_owner_thread() -> None:
    """Given a store on a recording backend with a committed mineral renamed
    When the store commits again
    Then a second apply ran
    And it ran on a thread other than the owner's
    """
    backend = _Recording(MemoryBackend())
    owner = threading.get_ident()
    with dc.Store._from_backend(backend) as store:
        store.store(Mineral(qid="Q1", name="quartz"))
        store.commit()
        found = store.get(Mineral, qid="Q1")
        assert found is not None
        found.name = "rock crystal"

        store.commit()

    assert len(backend.apply_threads) == 2
    assert backend.apply_threads[1] != owner


def test_failed_p2_returns_a_new_entity_to_new_state() -> None:
    """Given a stored mineral and a backend failing its first apply
    When the store commits
    Then OSError is raised
    And the mineral is back in the NEW state
    And no TID was consumed
    """
    backend = _FailNTimes(MemoryBackend(), failures=1)
    with dc.Store._from_backend(backend) as store:
        quartz = Mineral(qid="Q1", name="quartz")
        store.store(quartz)

        with pytest.raises(OSError, match="injected P2 fault"):
            store.commit()

        assert object.__getattribute__(quartz, "__dc_state__") == STATE_NEW
        assert store.last_tid == 0


def test_retry_after_failed_p2_reuses_the_first_tid() -> None:
    """Given a stored mineral whose first commit failed in P2
    When the store commits again
    Then the commit returns the very first TID
    And the mineral is the store's live instance
    """
    backend = _FailNTimes(MemoryBackend(), failures=1)
    with dc.Store._from_backend(backend) as store:
        quartz = Mineral(qid="Q1", name="quartz")
        store.store(quartz)
        with pytest.raises(OSError, match="injected P2 fault"):
            store.commit()

        tid = store.commit()

        assert tid == TID_BASE
        assert store.get(Mineral, qid="Q1") is quartz


def test_failed_p2_returns_a_dirty_entity_to_dirty_state() -> None:
    """Given a committed mineral renamed and a backend failing its next apply
    When the store commits
    Then OSError is raised
    And the mineral is back in the DIRTY state
    """
    backend = _FailNTimes(MemoryBackend(), failures=0)
    with dc.Store._from_backend(backend) as store:
        quartz = Mineral(qid="Q1", name="quartz")
        store.store(quartz)
        store.commit()
        backend.failures = 1
        quartz.name = "rock crystal"

        with pytest.raises(OSError, match="injected P2 fault"):
            store.commit()

        assert object.__getattribute__(quartz, "__dc_state__") == STATE_DIRTY


def test_retry_after_failed_dirty_p2_persists_the_rename() -> None:
    """Given a committed mineral renamed whose commit failed in P2
    When the store commits again and is reopened
    Then the mineral is named "rock crystal"
    """
    backend = _FailNTimes(MemoryBackend(), failures=0)
    with dc.Store._from_backend(backend) as store:
        quartz = Mineral(qid="Q1", name="quartz")
        store.store(quartz)
        store.commit()
        backend.failures = 1
        quartz.name = "rock crystal"
        with pytest.raises(OSError, match="injected P2 fault"):
            store.commit()

        store.commit()

    reopened = dc.Store._from_backend(backend)
    found = reopened.get(Mineral, qid="Q1")
    assert found is not None
    assert found.name == "rock crystal"
    reopened.close()


def test_types_row_survives_a_failed_first_commit_of_a_type() -> None:
    """Regression: the cid stays cached after a failed P2, so the retry batch must still carry
    the types row, or reopening hits an unknown cid.

    Given a first commit of the Mineral type that failed in P2
    When the store commits again and is reopened
    Then the mineral is readable and named "quartz"
    """
    inner = MemoryBackend()
    backend = _FailNTimes(inner, failures=1)
    with dc.Store._from_backend(backend) as store:
        store.store(Mineral(qid="Q1", name="quartz"))
        with pytest.raises(OSError, match="injected P2 fault"):
            store.commit()

        store.commit()

    reopened = dc.Store._from_backend(inner)
    found = reopened.get(Mineral, qid="Q1")
    assert found is not None
    assert found.name == "quartz"
    reopened.close()


def test_unencodable_value_rejects_the_commit() -> None:
    """Found by the stateful machine: an int beyond msgpack's 64-bit range fails P1 encoding.

    Given a stored mineral with the tag 2**64
    When the store commits
    Then OverflowError is raised
    """
    with dc.Store._from_backend(MemoryBackend()) as store:
        store.store(Mineral(qid="Q1", name="quartz", tags=[2**64]))

        with pytest.raises(OverflowError):
            store.commit()


def test_rejected_commit_consumes_no_tid_and_keeps_the_buffers() -> None:
    """The rejection must burn no TID (gapless, invariant 5) and leave a fixed-up retry possible.

    Given a stored mineral whose tag 2**64 made the commit raise OverflowError
    And the tag is replaced by 2**64 - 1, the largest honest msgpack int
    When the store commits again
    Then the commit returns the very first TID
    And the mineral is stored with the tag 2**64 - 1
    """
    with dc.Store._from_backend(MemoryBackend()) as store:
        scan = Mineral(qid="Q1", name="quartz", tags=[2**64])
        store.store(scan)
        with pytest.raises(OverflowError):
            store.commit()
        scan.tags[0] = 2**64 - 1

        tid = store.commit()

        assert tid == TID_BASE
        found = store.get(Mineral, qid="Q1")
        assert found is not None
        assert found.tags == [2**64 - 1]


def test_first_commit_returns_a_tid(store_factory: StoreFactory) -> None:
    """Given two stored minerals
    When the store commits
    Then a TID is returned
    """
    store = store_factory()
    store.store(Mineral(qid="QA", name="azurite", crystal_system="monoclinic"))
    store.store(Mineral(qid="QB", name="beryl", crystal_system="hexagonal"))

    tid = store.commit()

    assert tid is not None
    store.close()


def test_second_commit_returns_the_next_tid(store_factory: StoreFactory) -> None:
    """Given a committed mineral whose mohs was then set
    When the store commits again
    Then the TID is the first TID plus one
    """
    store = store_factory()
    azurite = Mineral(qid="QA", name="azurite", crystal_system="monoclinic")
    store.store(azurite)
    first_tid = store.commit()
    assert first_tid is not None
    azurite.mohs = 3.7

    second_tid = store.commit()

    assert second_tid == first_tid + 1
    store.close()


def test_commit_with_nothing_pending_returns_none(store_factory: StoreFactory) -> None:
    """Given a store whose pending changes were all committed
    When the store commits
    Then None is returned
    """
    store = store_factory()
    store.store(Mineral(qid="QA", name="azurite"))
    store.commit()

    tid = store.commit()

    assert tid is None
    store.close()


def test_committed_update_survives_reopen(store_factory: StoreFactory) -> None:
    """Given two minerals committed and azurite's mohs set to 3.7 and committed
    When the store is reopened
    Then azurite has mohs 3.7
    """
    store = store_factory()
    azurite = Mineral(qid="QA", name="azurite", crystal_system="monoclinic")
    store.store(azurite)
    store.store(Mineral(qid="QB", name="beryl", crystal_system="hexagonal"))
    store.commit()
    azurite.mohs = 3.7
    store.commit()
    store.close()

    reopened = store_factory()
    survivor = reopened.get(Mineral, qid="QA")

    assert survivor is not None
    assert survivor.mohs == 3.7
    reopened.close()


def test_hexagonal_query_after_reopen_finds_beryl_only(store_factory: StoreFactory) -> None:
    """Given azurite (monoclinic) and beryl (hexagonal) committed and the store reopened
    When crystal_system == "hexagonal" is queried
    Then only beryl is returned
    """
    store = store_factory()
    store.store(Mineral(qid="QA", name="azurite", crystal_system="monoclinic"))
    store.store(Mineral(qid="QB", name="beryl", crystal_system="hexagonal"))
    store.commit()
    store.close()
    reopened = store_factory()

    hits = reopened.query(dc.fields(Mineral).crystal_system == "hexagonal")

    assert [m.qid for m in hits] == ["QB"]
    reopened.close()
