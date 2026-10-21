"""#150 — open_follower bootstrap: replay-from-0 into a real local store.

The load-bearing chain-B story. A follower turns the coordinator's
COMMIT-DELTA-v1 stream into a queryable local store that reproduces the
coordinator's committed state exactly — over BOTH backends — and every cut fails
closed: a gapped stream raises ``DeltaGapError`` with no partial apply, a
duplicate delta is an idempotent no-op.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import datacrystal as dc
from datacrystal._follower import _bootstrap_backend, open_follower
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import StorageBackend
from datacrystal._storage.sqlite import SqliteBackend
from datacrystal._store import Store
from datacrystal.contract.applier import DeltaGapError, ReferenceApplier
from datacrystal.deltalog import DeltaLog
from tests.conftest import Locality, Mineral


def _seed_coordinator(tmp_path: Path) -> tuple[Store, DeltaLog]:
    """Build a coordinator with a DeltaLog and three commits (TIDs 1, 2, 3)."""
    coord = Store._from_backend(MemoryBackend())
    log = DeltaLog(tmp_path / "coordlog")
    coord.attach(log)
    gotthard = Locality(qid="L1", name="St Gotthard", country="CH")
    coord.store(Mineral(qid="Q1", name="Quartz", mohs=7.0, type_locality=dc.Lazy.of(gotthard)))
    coord.commit()
    coord.store(Mineral(qid="Q2", name="Calcite", mohs=3.0))
    coord.commit()
    coord.store(Mineral(qid="Q3", name="Gold", mohs=2.5))
    coord.commit()
    return coord, log


def _reference_digest(log: DeltaLog) -> str:
    """Replay the whole log through the reference applier and return its state digest."""
    leader = ReferenceApplier()
    for delta in log.replay(after_tid=0):
        leader.apply(delta)
    return leader.state_digest()


@pytest.fixture(params=["memory", "sqlite"])
def follower_backend(request: pytest.FixtureRequest, tmp_path: Path) -> StorageBackend:
    """Create a fresh follower backend; the replica is a real Store, so both must agree."""
    if request.param == "memory":
        return MemoryBackend()
    return SqliteBackend(tmp_path / "follower")


@pytest.fixture
def coordinator(tmp_path: Path) -> Iterator[tuple[Store, DeltaLog]]:
    """Provide a seeded coordinator with its delta log, closed at teardown."""
    coord, log = _seed_coordinator(tmp_path)
    yield coord, log
    coord.close()


@pytest.fixture
def replica_of() -> Iterator[Callable[[StorageBackend], Store]]:
    """Provide a factory wrapping a backend in a Store, all closed at teardown."""
    opened: list[Store] = []

    def wrap(backend: StorageBackend) -> Store:
        replica = Store._from_backend(backend)
        opened.append(replica)
        return replica

    yield wrap
    for replica in opened:
        replica.close()


def test_follower_consumes_the_delta_stream_like_the_reference_applier(
    follower_backend: StorageBackend, coordinator: tuple[Store, DeltaLog]
) -> None:
    """Given a coordinator with three commits and a fresh follower backend
    When the follower bootstraps from the log replayed from 0
    Then its applier's state digest equals the reference applier's
    """
    _, log = coordinator
    follower_backend.boot()

    applier = _bootstrap_backend(follower_backend, log.replay(after_tid=0))

    assert applier.state_digest() == _reference_digest(log)


def test_follower_watermark_matches_the_coordinator_after_bootstrap(
    follower_backend: StorageBackend,
    coordinator: tuple[Store, DeltaLog],
    replica_of: Callable[[StorageBackend], Store],
) -> None:
    """Given a coordinator with three commits and a fresh follower backend
    When the follower bootstraps from the log replayed from 0
    Then the follower's last_tid equals the coordinator's
    """
    coord, log = coordinator
    follower_backend.boot()
    _bootstrap_backend(follower_backend, log.replay(after_tid=0))

    follower = replica_of(follower_backend)

    assert follower.last_tid == coord.last_tid


def test_follower_reads_the_replicated_mineral_and_its_referent(
    follower_backend: StorageBackend,
    coordinator: tuple[Store, DeltaLog],
    replica_of: Callable[[StorageBackend], Store],
) -> None:
    """Given a coordinator whose quartz references St Gotthard
    When the follower bootstraps from the log replayed from 0
    Then the follower reads the quartz with its name and hardness
    And the quartz still has its type locality
    And the follower reads St Gotthard by key
    """
    _, log = coordinator
    follower_backend.boot()
    _bootstrap_backend(follower_backend, log.replay(after_tid=0))
    follower = replica_of(follower_backend)

    q1 = follower.get(Mineral, qid="Q1")
    loc = follower.get(Locality, qid="L1")

    assert q1 is not None
    assert q1.name == "Quartz"
    assert q1.mohs == 7.0
    assert q1.type_locality is not None
    assert loc is not None
    assert loc.name == "St Gotthard"


def test_follower_queries_exactly_the_coordinators_minerals(
    follower_backend: StorageBackend,
    coordinator: tuple[Store, DeltaLog],
    replica_of: Callable[[StorageBackend], Store],
) -> None:
    """Given a coordinator with three minerals
    When the follower bootstraps from the log replayed from 0
    Then a local query finds exactly those three minerals
    """
    _, log = coordinator
    follower_backend.boot()
    _bootstrap_backend(follower_backend, log.replay(after_tid=0))

    follower = replica_of(follower_backend)

    assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2", "Q3"}


def test_follower_refuses_a_gap_with_no_partial_apply(
    coordinator: tuple[Store, DeltaLog], replica_of: Callable[[StorageBackend], Store]
) -> None:
    """Given a delta stream of TIDs 1 and 3 with a gap at 2
    When a fresh follower backend bootstraps from it
    Then the bootstrap raises DeltaGapError
    And the watermark stays at delta 1
    And only the first mineral landed
    """
    _, log = coordinator
    deltas = list(log.replay(after_tid=0))
    gapped = [deltas[0], deltas[2]]
    backend = MemoryBackend()
    backend.boot()

    with pytest.raises(DeltaGapError):
        _bootstrap_backend(backend, gapped)

    follower = replica_of(backend)
    assert follower.last_tid == deltas[0]["tid"]
    assert {m.qid for m in follower.query(Mineral)} == {"Q1"}


def test_follower_ignores_a_duplicate_delta(
    coordinator: tuple[Store, DeltaLog], replica_of: Callable[[StorageBackend], Store]
) -> None:
    """Given a delta stream that delivers delta 1 twice
    When a fresh follower backend bootstraps from it
    Then the applier's watermark equals the coordinator's
    And the follower holds exactly the coordinator's three minerals
    And the follower's last_tid equals the coordinator's
    """
    coord, log = coordinator
    deltas = list(log.replay(after_tid=0))
    stream = [deltas[0], deltas[0], *deltas[1:]]
    backend = MemoryBackend()
    backend.boot()

    applier = _bootstrap_backend(backend, stream)

    follower = replica_of(backend)
    assert applier.watermark == coord.last_tid
    assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2", "Q3"}
    assert follower.last_tid == coord.last_tid


def test_open_follower_over_http_reproduces_the_coordinator(
    coordinator: tuple[Store, DeltaLog],
) -> None:
    """Given a coordinator served through the federation router
    When open_follower connects to it over HTTP
    Then the follower reads the quartz with its name
    And its last_tid equals the coordinator's
    And a local query finds the three minerals
    """
    pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from datacrystal.web import federation_router

    coord, log = coordinator
    app = FastAPI()
    app.include_router(federation_router(coord, log))

    with TestClient(app) as client, open_follower("http://coordinator", client=client) as follower:
        quartz = follower.get(Mineral, qid="Q1")

        assert quartz is not None
        assert quartz.name == "Quartz"
        assert follower.last_tid == coord.last_tid
        assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2", "Q3"}
