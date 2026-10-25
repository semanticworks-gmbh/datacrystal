"""#150 — open_follower bootstrap: replay-from-0 into a real local store.

The load-bearing chain-B story. A follower turns the coordinator's
COMMIT-DELTA-v1 stream into a queryable local store that reproduces the
coordinator's committed state exactly — over BOTH backends — and every cut fails
closed: a gapped stream raises ``DeltaGapError`` with no partial apply, a
duplicate delta is an idempotent no-op.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import datacrystal as dc
from datacrystal.contract.applier import DeltaGapError
from datacrystal.deltalog import DeltaLog
from tests.conftest import Locality, Mineral
from tests.web._fake_coordinator import Delta, FakeCoordinator


def _seed_coordinator(tmp_path: Path) -> tuple[dc.Store, DeltaLog]:
    """Build a coordinator with a DeltaLog and three commits (TIDs 1, 2, 3)."""
    coord = dc.Store.open(tmp_path / "coord", durability="never")
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


@pytest.fixture(params=["memory", "sqlite"])
def follower_path(request: pytest.FixtureRequest, tmp_path: Path) -> Path | None:
    """Place the replica in memory, or sqlite-backed at a path: both must agree."""
    return None if request.param == "memory" else tmp_path / "follower"


@pytest.fixture
def coordinator(tmp_path: Path) -> Iterator[tuple[dc.Store, DeltaLog]]:
    """Provide a seeded coordinator with its delta log, closed at teardown."""
    coord, log = _seed_coordinator(tmp_path)
    yield coord, log
    coord.close()


@pytest.fixture
def deltas(coordinator: tuple[dc.Store, DeltaLog]) -> list[Delta]:
    """Provide the coordinator's three deltas, TIDs 1, 2 and 3."""
    _, log = coordinator
    return list(log.replay(after_tid=0))


def test_follower_watermark_matches_the_coordinator_after_bootstrap(
    coordinator: tuple[dc.Store, DeltaLog], deltas: list[Delta], follower_path: Path | None
) -> None:
    """Given a coordinator with three commits and a fresh follower
    When the follower bootstraps from the delta stream replayed from 0
    Then the follower's last_tid equals the coordinator's
    """
    coord, _ = coordinator

    with dc.open_follower("http://coord", client=FakeCoordinator(deltas), path=follower_path) as f:
        assert f.last_tid == coord.last_tid


def test_follower_reads_the_replicated_mineral_and_its_referent(
    deltas: list[Delta], follower_path: Path | None
) -> None:
    """Given a coordinator whose quartz references St Gotthard
    When the follower bootstraps from the delta stream replayed from 0
    Then the follower reads the quartz with its name and hardness
    And the quartz still has its type locality
    And the follower reads St Gotthard by key
    """
    with dc.open_follower("http://coord", client=FakeCoordinator(deltas), path=follower_path) as f:
        q1 = f.get(Mineral, qid="Q1")
        loc = f.get(Locality, qid="L1")

        assert q1 is not None
        assert q1.name == "Quartz"
        assert q1.mohs == 7.0
        assert q1.type_locality is not None
        assert loc is not None
        assert loc.name == "St Gotthard"


def test_follower_queries_exactly_the_coordinators_minerals(
    deltas: list[Delta], follower_path: Path | None
) -> None:
    """Given a coordinator with three minerals
    When the follower bootstraps from the delta stream replayed from 0
    Then a local query finds exactly those three minerals
    """
    with dc.open_follower("http://coord", client=FakeCoordinator(deltas), path=follower_path) as f:
        assert {m.qid for m in f.query(Mineral)} == {"Q1", "Q2", "Q3"}


def test_follower_refuses_a_gap_with_no_partial_apply(
    deltas: list[Delta], tmp_path: Path
) -> None:
    """Given a delta stream of TIDs 1 and 3 with a gap at 2 and a sqlite follower path
    When the follower tries to open from it
    Then the open raises DeltaGapError
    And the file left behind reopens at the watermark of delta 1
    And only the first mineral landed
    """
    gapped = FakeCoordinator([deltas[0], deltas[2]])
    follower_dir = tmp_path / "follower"
    follower_dir.mkdir()

    with pytest.raises(DeltaGapError):
        dc.open_follower("http://coord", client=gapped, path=follower_dir / "data.sqlite")

    with dc.Store.open(follower_dir) as reopened:
        assert reopened.last_tid == deltas[0]["tid"]
        assert {m.qid for m in reopened.query(Mineral)} == {"Q1"}


def test_follower_ignores_a_duplicate_delta(
    coordinator: tuple[dc.Store, DeltaLog], deltas: list[Delta], follower_path: Path | None
) -> None:
    """Given a delta stream that delivers delta 1 twice
    When the follower bootstraps from it
    Then the follower holds exactly the coordinator's three minerals
    And the follower's last_tid equals the coordinator's
    """
    coord, _ = coordinator
    stream = FakeCoordinator([deltas[0], deltas[0], *deltas[1:]])

    with dc.open_follower("http://coord", client=stream, path=follower_path) as f:
        assert {m.qid for m in f.query(Mineral)} == {"Q1", "Q2", "Q3"}
        assert f.last_tid == coord.last_tid


def test_open_follower_over_http_reproduces_the_coordinator(
    coordinator: tuple[dc.Store, DeltaLog],
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

    with TestClient(app) as client, dc.open_follower("http://coordinator", client=client) as follower:
        quartz = follower.get(Mineral, qid="Q1")

        assert quartz is not None
        assert quartz.name == "Quartz"
        assert follower.last_tid == coord.last_tid
        assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2", "Q3"}
