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

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from tests.conftest import Locality, Mineral


class _Clock:
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


def test_idle_handles_demote_and_reload():
    clock = _Clock()
    store = dc.Store._from_backend(_seeded_backend(), lazy_timeout=10.0,
                                   lazy_clock=clock)
    azurite = store.get(Mineral, qid="Q1")
    assert azurite is not None
    handle = azurite.type_locality
    assert handle.get().name == "Tsumeb Mine"  # loads + tracks at t=0
    assert handle.loaded

    clock.now = 5.0
    store.get(Mineral, qid="Q1")  # boundary sweep: not idle long enough
    assert handle.loaded

    clock.now = 11.0
    store.get(Mineral, qid="Q1")  # boundary sweep: idle 11s > 10s
    assert not handle.loaded  # demoted — the cut point released its subgraph
    assert handle.oid is not None
    reloaded = handle.get()  # transparent reload through the store
    assert reloaded.name == "Tsumeb Mine"
    manager = store._lazyman
    assert manager is not None
    assert manager.demoted_total == 1
    store.close()


def test_access_refreshes_the_idle_clock():
    clock = _Clock()
    store = dc.Store._from_backend(_seeded_backend(), lazy_timeout=10.0,
                                   lazy_clock=clock)
    azurite = store.get(Mineral, qid="Q1")
    assert azurite is not None
    handle = azurite.type_locality
    handle.get()  # t=0: load
    clock.now = 8.0
    handle.get()  # refresh: idle resets
    clock.now = 16.0
    store.get(Mineral, qid="Q1")  # sweep at t=16: idle is 8s < 10s
    assert handle.loaded
    store.close()


def test_user_made_lazy_of_never_demotes():
    """Lazy.of(entity) without an OID cannot reload itself — the manager
    must leave it alone."""
    clock = _Clock()
    store = dc.Store._from_backend(MemoryBackend(), lazy_timeout=1.0,
                                   lazy_clock=clock)
    keepsake = Locality(qid="L9", name="Broken Hill")
    handle = dc.Lazy.of(keepsake)
    clock.now = 100.0
    manager = store._lazyman
    assert manager is not None
    manager.sweep()
    assert handle.get() is keepsake
    store.close()


def test_demotion_runs_on_the_owner_thread_only():
    clock = _Clock()
    store = dc.Store._from_backend(_seeded_backend(), lazy_timeout=10.0,
                                   lazy_clock=clock)
    azurite = store.get(Mineral, qid="Q1")
    assert azurite is not None
    azurite.type_locality.get()
    clock.now = 11.0
    store.run_pending()  # any owner boundary
    manager = store._lazyman
    assert manager is not None
    assert manager.demoted_total >= 1
    assert manager.last_demotion_thread == threading.get_ident()
    store.close()


def test_async_owner_task_demotes_on_the_loop():
    backend = _seeded_backend()
    demo: dict[str, object] = {}

    async def main():
        store = dc.Store._from_backend(backend, lazy_timeout=0.02)
        astore = dc.AsyncStore(store)
        azurite = astore.get(Mineral, qid="Q1")
        assert azurite is not None
        handle = azurite.type_locality
        handle.get()
        # idle on purpose: only the owner-loop sweeper task is running
        for _ in range(100):
            if not handle.loaded:
                break
            await asyncio.sleep(0.01)
        manager = store._lazyman
        assert manager is not None
        demo["demoted"] = not handle.loaded
        demo["thread_ok"] = manager.last_demotion_thread == threading.get_ident()
        astore.close()

    asyncio.run(main())
    assert demo["demoted"] is True   # the loop task swept without API calls
    assert demo["thread_ok"] is True  # ... on the owner's thread (the loop)
