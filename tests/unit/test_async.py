"""aopen()/AsyncStore — ADR-001 owner-loop confinement and the async commit.

The load-bearing test here is the racing write: while P2 is in flight in
the IO executor, another task on the owner loop mutates a captured entity.
The P1 flip re-armed its hook, so the write re-dirties and lands in the
NEXT commit — neither lost nor torn. That is bound decision 2 verbatim.

Plain ``asyncio.run`` keeps the dev dependencies unchanged (no
pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import datacrystal as dc
from datacrystal._state import STATE_DIRTY
from datacrystal._storage.memory import MemoryBackend
from tests.conftest import Mineral


class _GatedApply:
    """Backend wrapper that parks apply() until released, recording its
    thread — lets a test hold P2 open while the owner loop keeps running."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()  # gating is opt-in per test
        self.apply_threads: list[int] = []

    def boot(self):
        return self._inner.boot()

    def load_many(self, oids):
        return self._inner.load_many(oids)

    def scan_type(self, cid):
        return self._inner.scan_type(cid)

    def apply(self, batch):
        self.apply_threads.append(threading.get_ident())
        self.entered.set()
        assert self.release.wait(timeout=10), "test forgot to release P2"
        self._inner.apply(batch)

    def read_view(self):
        return self._inner.read_view()

    def close(self):
        self._inner.close()


def test_aopen_round_trips_against_a_real_directory(tmp_path):
    async def main():
        async with await dc.aopen(tmp_path / "s", lock_ttl=0.5) as astore:
            astore.store(Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"))
            tid = await astore.commit()
            assert tid is not None
            hits = astore.query(dc.fields(Mineral).crystal_system == "trigonal")
            assert [m.qid for m in hits] == ["Q43010"]

    asyncio.run(main())
    # the sync engine reads what the async facade wrote
    with dc.Store.open(tmp_path / "s", lock_ttl=0.5) as store:
        found = store.get(Mineral, qid="Q43010")
        assert found is not None and found.name == "quartz"


def test_async_p2_runs_off_the_loop_and_the_loop_stays_free():
    backend = _GatedApply(MemoryBackend())

    async def main():
        astore = dc.AsyncStore(dc.Store._from_backend(backend))
        astore.store(Mineral(qid="Q1", name="quartz"))
        backend.release.clear()
        commit_task = asyncio.create_task(astore.commit())
        await asyncio.to_thread(backend.entered.wait, 10)
        # P2 is parked in the executor; the owner loop still runs tasks:
        beat = await asyncio.sleep(0, result="alive")
        assert beat == "alive"
        backend.release.set()
        assert await commit_task is not None
        astore.close()

    asyncio.run(main())
    owner = threading.get_ident()  # asyncio.run ran the loop on this thread
    assert backend.apply_threads and all(t != owner for t in backend.apply_threads)


def test_write_racing_p2_lands_in_the_next_commit():
    backend = _GatedApply(MemoryBackend())

    async def main():
        astore = dc.AsyncStore(dc.Store._from_backend(backend))
        m = Mineral(qid="Q1", name="quartz")
        astore.store(m)
        backend.release.clear()
        commit_task = asyncio.create_task(astore.commit())
        await asyncio.to_thread(backend.entered.wait, 10)
        # P1 flipped + re-armed; this owner-loop write races P2:
        m.name = "rock crystal"
        assert object.__getattribute__(m, "__dc_state__") == STATE_DIRTY
        backend.release.set()
        tid1 = await commit_task
        assert tid1 is not None
        # the racing write was NOT in tid1's batch and is still buffered
        tid2 = await astore.commit()
        assert tid2 == tid1 + 1
        astore.close()

    asyncio.run(main())
    # an unwarmed reader sees the racing write — it landed durably in tid2
    store = dc.Store._from_backend(backend._inner)
    found = store.get(Mineral, qid="Q1")
    assert found is not None and found.name == "rock crystal"
    store.close()


def test_transaction_commits_on_clean_exit_only():
    async def main():
        astore = dc.AsyncStore(dc.Store._from_backend(MemoryBackend()))
        async with astore.transaction():
            astore.store(Mineral(qid="Q1", name="quartz"))
        assert astore.get(Mineral, qid="Q1") is not None  # committed by the scope

        with pytest.raises(ValueError, match="boom"):
            async with astore.transaction():
                astore.store(Mineral(qid="Q2", name="azurite"))
                raise ValueError("boom")
        # not committed — still buffered (live objects have no rollback)
        assert astore.get(Mineral, qid="Q2") is None
        await astore.commit()  # the caller decided to keep it after all
        assert astore.get(Mineral, qid="Q2") is not None
        astore.close()

    asyncio.run(main())


def test_transaction_scopes_serialize():
    async def main():
        astore = dc.AsyncStore(dc.Store._from_backend(MemoryBackend()))
        order: list[str] = []

        async def scoped(tag: str):
            async with astore.transaction():
                order.append(f"{tag}-in")
                await asyncio.sleep(0.01)  # interleaving point without the lock
                order.append(f"{tag}-out")

        await asyncio.gather(scoped("a"), scoped("b"))
        assert order in (["a-in", "a-out", "b-in", "b-out"],
                         ["b-in", "b-out", "a-in", "a-out"])
        astore.close()

    asyncio.run(main())


def test_foreign_thread_submit_wakes_the_loop():
    async def main():
        astore = dc.AsyncStore(dc.Store._from_backend(MemoryBackend()))
        future = await asyncio.to_thread(
            astore.submit, lambda: astore.store(Mineral(qid="Q1", name="quartz"))
        )
        # no owner API call here: the wake hook pumps via the loop itself
        result = await asyncio.wrap_future(future)
        assert isinstance(result, int)
        await astore.commit()
        assert astore.get(Mineral, qid="Q1") is not None
        astore.close()

    asyncio.run(main())


def test_foreign_thread_direct_access_raises_wrong_thread():
    async def main():
        astore = dc.AsyncStore(dc.Store._from_backend(MemoryBackend()))

        def trespass():
            with pytest.raises(dc.WrongThreadError, match="submit"):
                astore.query(dc.fields(Mineral).crystal_system == "trigonal")

        await asyncio.to_thread(trespass)
        astore.close()

    asyncio.run(main())
