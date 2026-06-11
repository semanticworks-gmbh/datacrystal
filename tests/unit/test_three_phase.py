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

import pytest

import datacrystal as dc
from datacrystal._ids import TID_BASE
from datacrystal._state import STATE_DIRTY, STATE_NEW
from datacrystal._storage.memory import MemoryBackend
from tests.conftest import Mineral


class _Recording:
    """Backend wrapper recording which thread runs each apply()."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.apply_threads: list[int] = []

    def boot(self):
        return self._inner.boot()

    def load_many(self, oids):
        return self._inner.load_many(oids)

    def scan_type(self, cid):
        return self._inner.scan_type(cid)

    def apply(self, batch):
        self.apply_threads.append(threading.get_ident())
        self._inner.apply(batch)

    def close(self):
        self._inner.close()


class _FailNTimes(_Recording):
    def __init__(self, inner, failures: int) -> None:
        super().__init__(inner)
        self.failures = failures

    def apply(self, batch):
        if self.failures > 0:
            self.failures -= 1
            raise OSError("injected P2 fault")
        super().apply(batch)


def test_p2_runs_off_the_owner_thread():
    backend = _Recording(MemoryBackend())
    with dc.Store._from_backend(backend) as store:
        store.store(Mineral(qid="Q1", name="quartz"))
        store.commit()
        found = store.get(Mineral, qid="Q1")
        assert found is not None
        found.name = "rock crystal"
        store.commit()
    owner = threading.get_ident()
    assert len(backend.apply_threads) == 2
    assert all(t != owner for t in backend.apply_threads)


def test_failed_p2_rebuffers_new_entities_and_reuses_the_tid():
    backend = _FailNTimes(MemoryBackend(), failures=1)
    with dc.Store._from_backend(backend) as store:
        m = Mineral(qid="Q1", name="quartz")
        store.store(m)
        with pytest.raises(OSError):
            store.commit()
        # compensated: never durable, so back to NEW and buffered
        assert object.__getattribute__(m, "__dc_state__") == STATE_NEW
        assert store.last_tid == 0
        tid = store.commit()
        # gapless: the retry reused the failed commit's TID (the first ever)
        assert tid == TID_BASE
        assert store.get(Mineral, qid="Q1") is m


def test_failed_p2_rebuffers_dirty_entities():
    backend = _FailNTimes(MemoryBackend(), failures=0)
    with dc.Store._from_backend(backend) as store:
        m = Mineral(qid="Q1", name="quartz")
        store.store(m)
        store.commit()
        backend.failures = 1
        m.name = "rock crystal"
        with pytest.raises(OSError):
            store.commit()
        assert object.__getattribute__(m, "__dc_state__") == STATE_DIRTY
        store.commit()
    reopened = dc.Store._from_backend(backend)
    found = reopened.get(Mineral, qid="Q1")
    assert found is not None and found.name == "rock crystal"
    reopened.close()


def test_types_row_survives_a_failed_first_commit_of_a_type():
    """Regression: the cid stays cached after a failed P2; the retry batch
    must still carry the types row, or reopening hits an unknown cid."""
    inner = MemoryBackend()
    backend = _FailNTimes(inner, failures=1)
    with dc.Store._from_backend(backend) as store:
        store.store(Mineral(qid="Q1", name="quartz"))
        with pytest.raises(OSError):
            store.commit()
        store.commit()
    reopened = dc.Store._from_backend(inner)
    found = reopened.get(Mineral, qid="Q1")
    assert found is not None and found.name == "quartz"
    reopened.close()


def test_unencodable_value_rejects_the_commit_without_consuming_a_tid():
    """Found by the stateful machine: an int beyond msgpack's 64-bit range
    fails P1 encoding. The rejection must consume no TID (gapless,
    invariant 5) and leave the buffers intact for a fixed-up retry."""
    with dc.Store._from_backend(MemoryBackend()) as store:
        scan = Mineral(qid="Q1", name="quartz", tags=[2**64])  # too big
        store.store(scan)
        with pytest.raises(OverflowError):
            store.commit()
        scan.tags[0] = 2**64 - 1  # the largest honest msgpack int
        tid = store.commit()
        assert tid == TID_BASE  # the very first TID: the rejection burned none
        found = store.get(Mineral, qid="Q1")
        assert found is not None and found.tags == [2**64 - 1]


def test_commit_remains_atomic_and_correct_across_backends(store_factory):
    """The restructure must not change observable sync semantics."""
    store = store_factory()
    a = Mineral(qid="QA", name="azurite", crystal_system="monoclinic")
    b = Mineral(qid="QB", name="beryl", crystal_system="hexagonal")
    store.store(a)
    store.store(b)
    tid1 = store.commit()
    assert tid1 is not None
    a.mohs = 3.7
    tid2 = store.commit()
    assert tid2 == tid1 + 1
    assert store.commit() is None  # nothing pending
    store.close()

    reopened = store_factory()
    survivor = reopened.get(Mineral, qid="QA")
    assert survivor is not None and survivor.mohs == 3.7
    hits = reopened.query(Mineral.crystal_system == "hexagonal")
    assert [m.qid for m in hits] == ["QB"]
    reopened.close()
