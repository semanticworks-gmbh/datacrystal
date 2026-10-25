"""aopen()/AsyncStore — ADR-001 owner-loop confinement and the async commit.

The load-bearing test here is the racing write: while P2 is in flight in
the IO executor, another task on the owner loop mutates a captured entity.
The P1 flip re-armed its hook, so the write re-dirties and lands in the
NEXT commit — neither lost nor torn. That is bound decision 2 verbatim.

A function-scoped ``asyncio.Runner`` fixture drives the loop one ``runner.run`` per
When, which keeps the dev dependencies unchanged (no pytest-asyncio) and each test
straight-line.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path
from typing import NamedTuple

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import (
    BootInfo,
    CommitBatch,
    StorageReadView,
    StoredBlob,
    StoredRecord,
)
from tests.conftest import Mineral


class _GatedApply:
    """Backend wrapper that parks apply() until released, recording its thread.

    Lets a test hold P2 open while the owner loop keeps running.
    """

    def __init__(self, inner: MemoryBackend) -> None:
        self.inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()  # gating is opt-in per test
        self.apply_threads: list[int] = []

    def boot(self) -> BootInfo:
        return self.inner.boot()

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        return self.inner.load_many(oids)

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        return self.inner.scan_type(cid)

    def load_blob(self, oid: int) -> StoredBlob | None:
        return self.inner.load_blob(oid)

    def apply(self, batch: CommitBatch) -> None:
        self.apply_threads.append(threading.get_ident())
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test forgot to release P2")
        self.inner.apply(batch)

    def read_view(self) -> StorageReadView:
        return self.inner.read_view()

    def close(self) -> None:
        self.inner.close()


class _ParkedCommit(NamedTuple):
    """A commit whose P2 is parked in the executor, with the pieces a test needs."""

    astore: dc.AsyncStore
    backend: _GatedApply
    quartz: Mineral
    commit_task: asyncio.Task[int | None]


@pytest.fixture
def runner() -> Iterator[asyncio.Runner]:
    """Give the test its own event loop, closed afterwards."""
    with asyncio.Runner() as runner:
        yield runner


async def _open_async_store(backend: _GatedApply | MemoryBackend) -> dc.AsyncStore:
    return dc.AsyncStore(dc.Store._from_backend(backend))


async def _commit_is_pending_after_a_loop_turn(commit_task: asyncio.Task[int | None]) -> bool:
    finished, _ = await asyncio.wait([commit_task], timeout=0)
    return not finished


async def _result_of(future: Future[int]) -> int:
    return await asyncio.wrap_future(future)


async def _store_in_transaction(astore: dc.AsyncStore, mineral: Mineral) -> None:
    async with astore.transaction():
        astore.store(mineral)


async def _store_in_failing_transaction(astore: dc.AsyncStore, mineral: Mineral) -> None:
    async with astore.transaction():
        astore.store(mineral)
        raise ValueError("boom")


async def _scope_a(astore: dc.AsyncStore, order: list[str], b_started: asyncio.Event) -> None:
    """Enter a transaction and stay inside it until scope b has started."""
    async with astore.transaction():
        order.append("a-in")
        await b_started.wait()
        order.append("a-out")


async def _scope_b(astore: dc.AsyncStore, order: list[str], b_started: asyncio.Event) -> None:
    """Announce itself, then enter a transaction (which waits for scope a to leave)."""
    b_started.set()
    async with astore.transaction():
        order.append("b-in")
        order.append("b-out")


async def _run_both_scopes(astore: dc.AsyncStore) -> list[str]:
    order: list[str] = []
    b_started = asyncio.Event()
    await asyncio.gather(
        _scope_a(astore, order, b_started), _scope_b(astore, order, b_started)
    )
    return order


@pytest.fixture
def parked_commit(runner: asyncio.Runner) -> Iterator[_ParkedCommit]:
    """Start a commit of quartz and hold its P2 inside the backend until the test releases it."""
    backend = _GatedApply(MemoryBackend())
    astore = runner.run(_open_async_store(backend))
    quartz = Mineral(qid="Q1", name="quartz")
    astore.store(quartz)
    backend.release.clear()
    commit_task = runner.get_loop().create_task(astore.commit())
    entered = runner.run(asyncio.to_thread(backend.entered.wait, 10))
    if not entered:
        raise TimeoutError("the commit's P2 never entered the backend")
    yield _ParkedCommit(astore, backend, quartz, commit_task)
    backend.release.set()
    runner.run(asyncio.wait_for(commit_task, 10))
    astore.close()


@pytest.fixture
def async_dir_store(runner: asyncio.Runner, tmp_path: Path) -> dc.AsyncStore:
    """Open an async store on a real directory with trigonal quartz stored, not committed."""
    astore = runner.run(dc.aopen(tmp_path / "s", lock_ttl=0.5))
    astore.store(Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"))
    return astore


def test_aopen_commit_returns_a_tid(
    runner: asyncio.Runner, async_dir_store: dc.AsyncStore
) -> None:
    """Given an async store opened on a real directory with quartz stored
    When the store commits
    Then a TID is returned
    """
    tid = runner.run(async_dir_store.commit())

    assert tid is not None
    async_dir_store.close()


def test_aopen_query_finds_the_committed_mineral(
    runner: asyncio.Runner, async_dir_store: dc.AsyncStore
) -> None:
    """Given an async store on a real directory with trigonal quartz committed
    When crystal_system == "trigonal" is queried
    Then only Q43010 is returned
    """
    runner.run(async_dir_store.commit())

    hits = async_dir_store.query(dc.fields(Mineral).crystal_system == "trigonal")

    assert [m.qid for m in hits] == ["Q43010"]
    async_dir_store.close()


def test_sync_engine_reads_what_the_async_facade_wrote(
    runner: asyncio.Runner, async_dir_store: dc.AsyncStore, tmp_path: Path
) -> None:
    """Given quartz committed through an async store on a real directory, then closed
    When a sync store opens the same directory
    Then quartz is there
    """
    runner.run(async_dir_store.commit())
    async_dir_store.close()

    with dc.Store.open(tmp_path / "s", lock_ttl=0.5) as store:
        found = store.get(Mineral, qid="Q43010")

        assert found is not None
        assert found.name == "quartz"


def test_loop_stays_free_while_p2_is_parked(
    runner: asyncio.Runner, parked_commit: _ParkedCommit
) -> None:
    """Given a commit whose P2 is parked in the executor
    When the owner loop runs a task
    Then the task completes while the commit is still pending
    """
    pending = runner.run(_commit_is_pending_after_a_loop_turn(parked_commit.commit_task))

    assert pending is True


def test_async_commit_completes_once_p2_is_released(
    runner: asyncio.Runner, parked_commit: _ParkedCommit
) -> None:
    """Given a commit whose P2 is parked in the executor
    When P2 is released and the commit awaited
    Then the commit returns a TID
    """
    parked_commit.backend.release.set()

    tid = runner.run(asyncio.wait_for(parked_commit.commit_task, 10))

    assert tid is not None
    parked_commit.astore.close()


def test_async_p2_runs_off_the_owner_thread(
    runner: asyncio.Runner, parked_commit: _ParkedCommit
) -> None:
    """Given a commit whose P2 is parked in the executor
    When P2 is released and the commit awaited
    Then exactly one apply ran
    And it ran on a thread other than the loop's
    """
    owner = threading.get_ident()
    parked_commit.backend.release.set()

    runner.run(asyncio.wait_for(parked_commit.commit_task, 10))

    assert len(parked_commit.backend.apply_threads) == 1
    assert parked_commit.backend.apply_threads[0] != owner
    parked_commit.astore.close()


def test_write_racing_p2_is_not_in_the_first_batch(
    runner: asyncio.Runner, parked_commit: _ParkedCommit
) -> None:
    """Given a commit of quartz whose P2 is parked, with quartz renamed meanwhile
    When P2 is released and the commit awaited, then the store commits again
    Then the second TID is the first plus one
    """
    parked_commit.quartz.name = "rock crystal"
    parked_commit.backend.release.set()
    first_tid = runner.run(asyncio.wait_for(parked_commit.commit_task, 10))
    assert first_tid is not None

    second_tid = runner.run(parked_commit.astore.commit())

    assert second_tid == first_tid + 1
    parked_commit.astore.close()


def test_write_racing_p2_lands_durably_in_the_next_commit(
    runner: asyncio.Runner, parked_commit: _ParkedCommit
) -> None:
    """Given a commit of quartz whose P2 is parked, with quartz renamed meanwhile
    When P2 is released, both commits finish, and an unwarmed reader opens the backend
    Then quartz is named "rock crystal"
    """
    parked_commit.quartz.name = "rock crystal"
    parked_commit.backend.release.set()
    runner.run(asyncio.wait_for(parked_commit.commit_task, 10))
    runner.run(parked_commit.astore.commit())
    parked_commit.astore.close()

    store = dc.Store._from_backend(parked_commit.backend.inner)
    found = store.get(Mineral, qid="Q1")

    assert found is not None
    assert found.name == "rock crystal"
    store.close()


def test_transaction_commits_on_clean_exit(runner: asyncio.Runner) -> None:
    """Given an async store
    When a transaction scope that stored quartz exits cleanly
    Then quartz is found
    """
    astore = runner.run(_open_async_store(MemoryBackend()))

    runner.run(_store_in_transaction(astore, Mineral(qid="Q1", name="quartz")))

    assert astore.get(Mineral, qid="Q1") is not None
    astore.close()


def test_transaction_does_not_commit_when_the_scope_raises(runner: asyncio.Runner) -> None:
    """Live objects have no rollback, so the store stays buffered but uncommitted.

    Given an async store
    When a transaction scope that stored azurite raises ValueError("boom")
    Then ValueError is raised out of the scope
    And azurite is not found
    """
    astore = runner.run(_open_async_store(MemoryBackend()))

    with pytest.raises(ValueError, match="boom"):
        runner.run(_store_in_failing_transaction(astore, Mineral(qid="Q2", name="azurite")))

    assert astore.get(Mineral, qid="Q2") is None
    astore.close()


def test_buffered_write_of_a_failed_transaction_commits_on_demand(
    runner: asyncio.Runner,
) -> None:
    """Given a transaction scope that stored azurite and raised, leaving it buffered
    When the caller commits after all
    Then azurite is found
    """
    astore = runner.run(_open_async_store(MemoryBackend()))
    with pytest.raises(ValueError, match="boom"):
        runner.run(_store_in_failing_transaction(astore, Mineral(qid="Q2", name="azurite")))

    runner.run(astore.commit())

    assert astore.get(Mineral, qid="Q2") is not None
    astore.close()


def test_transaction_scopes_serialize(runner: asyncio.Runner) -> None:
    """Scope a stays inside its transaction until scope b has started and queued behind it.

    Given an async store and scope a inside a transaction
    When scope b starts and enters its own transaction
    Then b enters only after a has left
    """
    astore = runner.run(_open_async_store(MemoryBackend()))

    order = runner.run(_run_both_scopes(astore))

    assert order == ["a-in", "a-out", "b-in", "b-out"]
    astore.close()


@pytest.fixture
def submitted_from_a_thread(runner: asyncio.Runner) -> tuple[dc.AsyncStore, Future[int]]:
    """Submit a store of quartz from a foreign thread, with no owner API call afterwards."""
    astore = runner.run(_open_async_store(MemoryBackend()))
    store_quartz = functools.partial(astore.store, Mineral(qid="Q1", name="quartz"))
    future = runner.run(asyncio.to_thread(astore.submit, store_quartz))
    return astore, future


def test_foreign_thread_submit_wakes_the_loop(
    runner: asyncio.Runner, submitted_from_a_thread: tuple[dc.AsyncStore, Future[int]]
) -> None:
    """The wake hook pumps through the loop itself, with no owner API call in between.

    Given a store of quartz submitted from a foreign thread
    When the loop awaits the submitted future
    Then it resolves to an int oid
    """
    astore, future = submitted_from_a_thread

    result = runner.run(asyncio.wait_for(_result_of(future), 10))

    assert isinstance(result, int)
    astore.close()


def test_foreign_thread_submitted_store_commits(
    runner: asyncio.Runner, submitted_from_a_thread: tuple[dc.AsyncStore, Future[int]]
) -> None:
    """Given a store of quartz submitted from a foreign thread and resolved
    When the store commits
    Then quartz is found
    """
    astore, future = submitted_from_a_thread
    runner.run(asyncio.wait_for(_result_of(future), 10))

    runner.run(astore.commit())

    assert astore.get(Mineral, qid="Q1") is not None
    astore.close()


def test_foreign_thread_direct_access_raises_wrong_thread(runner: asyncio.Runner) -> None:
    """Given an async store owned by the loop's thread
    When a foreign thread queries it directly
    Then WrongThreadError points to submit
    """
    astore = runner.run(_open_async_store(MemoryBackend()))
    condition = dc.fields(Mineral).crystal_system == "trigonal"

    with pytest.raises(dc.WrongThreadError, match="submit"):
        runner.run(asyncio.to_thread(astore.query, condition))

    astore.close()
