"""LazyReferenceManager — timeout-only demotion (KICKOFF M2; ADR-001 bound
decision 3, the daemon principle).

Sync sweeps piggyback on owner API calls; async sweeps run as an owner-loop
task. Either way demotion happens on the owner's thread — recorded by the
manager and asserted here. The clock is injectable: no real sleeps in the
sync tests (the async test uses small real timeouts because asyncio.sleep
drives the owner task).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

import datacrystal as dc
from datacrystal._lazy import LazyReferenceManager
from datacrystal._storage.memory import MemoryBackend
from tests.conftest import Locality, Mineral


def _present[T](value: T | None) -> T:
    """Narrow an Optional for the type checker; a fixture cannot assert."""
    if value is None:
        raise LookupError("expected a value, got None")
    return value


class _Clock:
    """A clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _seeded_backend() -> MemoryBackend:
    backend = MemoryBackend()
    store = dc.Store._from_backend(backend)
    tsumeb = Locality(qid="L1", name="Tsumeb Mine")
    store.store(Mineral(qid="Q1", name="azurite", type_locality=dc.Lazy.of(tsumeb)))
    store.commit()
    store.close()
    return backend


@pytest.fixture
def clock() -> _Clock:
    """Provide a manual clock starting at t=0."""
    return _Clock()


@pytest.fixture
def lazy_store(clock: _Clock) -> Iterator[dc.Store]:
    """Open a store with a 10 s lazy timeout on the manual clock, holding azurite -> Tsumeb Mine."""
    store = dc.Store._from_backend(_seeded_backend(), lazy_timeout=10.0, lazy_clock=clock)
    yield store
    store.close()


@pytest.fixture
def loaded_handle(lazy_store: dc.Store) -> dc.Lazy[Locality]:
    """Return azurite's type_locality handle, loaded and tracked at t=0."""
    handle = _present(_present(lazy_store.get(Mineral, qid="Q1")).type_locality)
    handle.get()
    return handle


def test_handle_idle_less_than_the_timeout_stays_loaded(
    lazy_store: dc.Store, clock: _Clock, loaded_handle: dc.Lazy[Locality]
) -> None:
    """Given a handle loaded at t=0 with a 10 s timeout
    When an owner API call sweeps at t=5
    Then the handle is still loaded
    """
    clock.now = 5.0

    lazy_store.get(Mineral, qid="Q1")

    assert loaded_handle.loaded


def test_handle_idle_past_the_timeout_is_demoted(
    lazy_store: dc.Store, clock: _Clock, loaded_handle: dc.Lazy[Locality]
) -> None:
    """Given a handle loaded at t=0 with a 10 s timeout
    When an owner API call sweeps at t=11
    Then the handle is no longer loaded
    And it still carries its oid
    """
    clock.now = 11.0

    lazy_store.get(Mineral, qid="Q1")

    assert not loaded_handle.loaded
    assert loaded_handle.oid is not None


def test_demoted_handle_reloads_through_the_store(
    lazy_store: dc.Store, clock: _Clock, loaded_handle: dc.Lazy[Locality]
) -> None:
    """Given a handle demoted by a sweep at t=11
    When get() is called on it
    Then it returns the locality "Tsumeb Mine"
    """
    clock.now = 11.0
    lazy_store.get(Mineral, qid="Q1")

    reloaded = loaded_handle.get()

    assert reloaded.name == "Tsumeb Mine"


def test_one_demotion_is_counted(
    lazy_store: dc.Store, clock: _Clock, loaded_handle: dc.Lazy[Locality]
) -> None:
    """Given a handle loaded at t=0 with a 10 s timeout
    When an owner API call sweeps at t=11
    Then the manager counts one demotion
    """
    clock.now = 11.0

    lazy_store.get(Mineral, qid="Q1")

    assert lazy_store._lazyman is not None
    assert lazy_store._lazyman.demoted_total == 1


def test_access_refreshes_the_idle_clock(
    lazy_store: dc.Store, clock: _Clock, loaded_handle: dc.Lazy[Locality]
) -> None:
    """Given a handle loaded at t=0 and read again at t=8 with a 10 s timeout
    When an owner API call sweeps at t=16
    Then the handle is still loaded, idle only 8 s
    """
    clock.now = 8.0
    loaded_handle.get()
    clock.now = 16.0

    lazy_store.get(Mineral, qid="Q1")

    assert loaded_handle.loaded


def test_user_made_lazy_of_never_demotes(clock: _Clock) -> None:
    """Lazy.of(entity) without an OID cannot reload itself, so the manager leaves it alone.

    Given a handle made with Lazy.of(Broken Hill) and a 1 s timeout
    When the clock is at t=100 and the manager sweeps
    Then the handle still returns the identical locality
    """
    store = dc.Store._from_backend(MemoryBackend(), lazy_timeout=1.0, lazy_clock=clock)
    keepsake = Locality(qid="L9", name="Broken Hill")
    handle = dc.Lazy.of(keepsake)
    clock.now = 100.0
    assert store._lazyman is not None

    store._lazyman.sweep()

    assert handle.get() is keepsake
    store.close()


def test_demotion_records_the_owner_thread(
    lazy_store: dc.Store, clock: _Clock, loaded_handle: dc.Lazy[Locality]
) -> None:
    """Given a handle loaded at t=0 with a 10 s timeout
    When the owner runs pending work at t=11
    Then the manager demoted at least one handle
    And the last demotion ran on the owner's thread
    """
    clock.now = 11.0

    lazy_store.run_pending()

    assert lazy_store._lazyman is not None
    assert lazy_store._lazyman.demoted_total >= 1
    assert lazy_store._lazyman.last_demotion_thread == threading.get_ident()


@pytest.fixture
def runner() -> Iterator[asyncio.Runner]:
    """Give the test its own event loop, closed afterwards."""
    with asyncio.Runner() as runner:
        yield runner


async def _open_async_store(backend: MemoryBackend) -> dc.AsyncStore:
    return dc.AsyncStore(dc.Store._from_backend(backend, lazy_timeout=0.02))


def _signal_on_demotion(manager: LazyReferenceManager) -> asyncio.Event:
    """Wrap `manager.sweep` so the returned event is set once a sweep demotes a handle."""
    demoted = asyncio.Event()
    real_sweep = manager.sweep

    def sweep_and_signal() -> int:
        count = real_sweep()
        if count:
            demoted.set()
        return count

    manager.sweep = sweep_and_signal
    return demoted


async def _wait_for(event: asyncio.Event, timeout: float) -> None:
    await asyncio.wait_for(event.wait(), timeout)


def test_async_sweeper_task_demotes_an_idle_handle_on_the_loop(runner: asyncio.Runner) -> None:
    """Given an async store with a 0.02 s lazy timeout and a loaded handle left idle
    When the owner-loop sweeper task has demoted a handle
    Then the handle is no longer loaded
    And the demotion ran on the owner's thread
    """
    astore = runner.run(_open_async_store(_seeded_backend()))
    manager = _present(astore._store._lazyman)
    demoted = _signal_on_demotion(manager)
    handle = _present(_present(astore.get(Mineral, qid="Q1")).type_locality)
    handle.get()

    runner.run(_wait_for(demoted, timeout=10))

    assert not handle.loaded
    assert manager.last_demotion_thread == threading.get_ident()
    astore.close()
