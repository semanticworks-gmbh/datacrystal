"""#151 — follower catch-up: Store.sync() refreshes a live replica.

Bootstrap (#150) built a fresh store; catch-up updates an already-open one. The
follower's reads/sync are owner-thread + synchronous, so a sync TestClient (no
event loop needed) drives it directly. Over both follower backends.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from datacrystal import Store, open_follower
from datacrystal.contract.applier import (
    CONTRACT_VERSION,
    DeltaFormatError,
    DeltaGapError,
)
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router
from tests.conftest import Mineral
from tests.web._fake_coordinator import FakeCoordinator


def _stamped_delta() -> dict[str, object]:
    """Build a well-formed empty delta at TID 1 of the current contract version."""
    return {
        "f": "datacrystal-delta",
        "v": CONTRACT_VERSION,
        "tid": 1,
        "actor": 0,
        "at": 0,
        "types": [],
        "ops": [],
        "root": None,
    }


@pytest.fixture(params=["memory", "sqlite"])
def follower_path(request: pytest.FixtureRequest, tmp_path: Path) -> Path | None:
    """Place the replica in memory, or sqlite-backed at a path: both must work."""
    return None if request.param == "memory" else tmp_path / "follower"


@pytest.fixture
def coordinator(tmp_path: Path) -> Iterator[tuple[Store, DeltaLog]]:
    """Provide a coordinator with a DeltaLog and one committed quartz (TID 1)."""
    coord = Store.open(tmp_path / "coord", durability="never")
    log = DeltaLog(tmp_path / "coordlog")
    coord.attach(log)
    coord.store(Mineral(qid="Q1", name="Quartz"))
    coord.commit()
    yield coord, log
    coord.close()


@pytest.fixture
def client(coordinator: tuple[Store, DeltaLog]) -> Iterator[TestClient]:
    """Serve the coordinator through the federation router on a TestClient."""
    coord, log = coordinator
    app = FastAPI()
    app.include_router(federation_router(coord, log))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def open_replica(client: TestClient) -> Iterator[Callable[..., Store]]:
    """Provide a factory opening followers of the coordinator, all closed at teardown."""
    opened: list[Store] = []

    def open_one(path: Path | None = None) -> Store:
        follower = open_follower("http://coord", client=client, path=path)
        opened.append(follower)
        return follower

    yield open_one
    for follower in opened:
        follower.close()


def test_follower_starts_with_the_coordinators_seed(
    open_replica: Callable[..., Store], follower_path: Path | None
) -> None:
    """Given a coordinator holding one quartz
    When a follower opens
    Then a local query finds exactly the quartz
    """
    follower = open_replica(follower_path)

    assert {m.qid for m in follower.query(Mineral)} == {"Q1"}


def test_sync_reports_the_coordinators_new_watermark(
    coordinator: tuple[Store, DeltaLog],
    open_replica: Callable[..., Store],
    follower_path: Path | None,
) -> None:
    """Given a follower and a coordinator that committed two more minerals
    When the follower syncs
    Then sync returns the coordinator's last tid
    And that tid is later than the follower's watermark before the sync
    """
    coord, _ = coordinator
    follower = open_replica(follower_path)
    before = follower.last_tid
    coord.store(Mineral(qid="Q2", name="Calcite"))
    coord.commit()
    coord.store(Mineral(qid="Q3", name="Gold"))
    coord.commit()

    applied = follower.sync()

    assert applied == coord.last_tid
    assert applied > before


def test_sync_pulls_the_new_minerals_into_the_replica(
    coordinator: tuple[Store, DeltaLog],
    open_replica: Callable[..., Store],
    follower_path: Path | None,
) -> None:
    """Given a follower and a coordinator that committed calcite and gold
    When the follower syncs
    Then a local query finds all three minerals
    And the gold is readable by key with its name
    """
    coord, _ = coordinator
    follower = open_replica(follower_path)
    coord.store(Mineral(qid="Q2", name="Calcite"))
    coord.commit()
    coord.store(Mineral(qid="Q3", name="Gold"))
    coord.commit()

    follower.sync()

    got = follower.get(Mineral, qid="Q3")
    assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2", "Q3"}
    assert got is not None
    assert got.name == "Gold"


def test_sync_with_nothing_new_is_a_no_op(
    coordinator: tuple[Store, DeltaLog],
    open_replica: Callable[..., Store],
    follower_path: Path | None,
) -> None:
    """Given a follower that already synced the coordinator's latest commit
    When the follower syncs again
    Then sync returns the same watermark
    """
    coord, _ = coordinator
    follower = open_replica(follower_path)
    coord.store(Mineral(qid="Q2", name="Calcite"))
    coord.commit()
    applied = follower.sync()

    applied_again = follower.sync()

    assert applied_again == applied


def test_sync_catchup_refuses_a_gap_no_partial_apply(
    coordinator: tuple[Store, DeltaLog], follower_path: Path | None
) -> None:
    """Given a follower at TID 1 and a catch-up stream that skips TID 2 and serves TID 3
    When the follower syncs
    Then the sync raises DeltaGapError
    And the follower's watermark stays at 1
    And only the first mineral is stored
    """
    coord, log = coordinator
    coord.store(Mineral(qid="Q2", name="Calcite"))
    coord.commit()
    coord.store(Mineral(qid="Q3", name="Gold"))
    coord.commit()
    deltas = list(log.replay(after_tid=0))
    served = FakeCoordinator([deltas[0]])

    with open_follower("http://coord", client=served, path=follower_path) as follower:
        served.deltas = [deltas[0], deltas[2]]

        with pytest.raises(DeltaGapError):
            follower.sync()

        assert follower.discard() == 1
        assert {m.qid for m in follower.query(Mineral)} == {"Q1"}


def test_sync_on_non_follower_raises(store: Store) -> None:
    """Given an ordinary single-node store
    When sync is called
    Then it raises RuntimeError
    And the store's watermark is still 0
    """
    with pytest.raises(RuntimeError):
        store.sync()

    assert store.last_tid == 0


def test_sync_refuses_with_buffered_writes(
    coordinator: tuple[Store, DeltaLog], open_replica: Callable[..., Store]
) -> None:
    """Given a follower holding a buffered local write
    When the follower syncs
    Then sync raises RuntimeError
    And the follower's watermark equals the coordinator's, unchanged
    """
    coord, _ = coordinator
    follower = open_replica()
    follower.store(Mineral(qid="LOCAL", name="scratch"))

    with pytest.raises(RuntimeError):
        follower.sync()

    assert follower.last_tid == coord.last_tid


def test_discard_drops_uncommitted_writes(store: Store) -> None:
    """Given a quartz edited and a calcite upserted, both uncommitted
    When the store discards
    Then nothing is left to commit
    And the uncommitted calcite is gone
    And the quartz is back at its committed hardness 5.0
    """
    store.upsert(Mineral(qid="Q1", name="Quartz", mohs=5.0))
    store.commit()
    m = store.get(Mineral, qid="Q1")
    assert m is not None
    m.mohs = 9.0
    store.upsert(Mineral(qid="Q2", name="Calcite"))

    store.discard()

    rolled = store.get(Mineral, qid="Q1")
    assert store.commit() is None
    assert store.get(Mineral, qid="Q2") is None
    assert rolled is not None
    assert rolled.mohs == 5.0


def test_store_follower_is_the_follower_constructor(
    client: TestClient, coordinator: tuple[Store, DeltaLog]
) -> None:
    """Given a coordinator holding one quartz
    When Store.follower connects to it
    Then a local query finds exactly the quartz
    And the quartz is readable by key with its name
    And the follower's watermark equals the coordinator's
    """
    coord, _ = coordinator

    with Store.follower("http://coord", client=client) as edge:
        got = edge.get(Mineral, qid="Q1")

        assert {m.qid for m in edge.query(Mineral)} == {"Q1"}
        assert got is not None
        assert got.name == "Quartz"
        assert edge.last_tid == coord.last_tid


def test_committing_commits_once_on_single_node(store: Store) -> None:
    """Given a single-node store, where a commit can never conflict
    When a committing() attempt upserts a quartz
    Then no second attempt is offered
    And the quartz is stored with hardness 7.0
    """
    attempts = store.committing(retries=3)
    txn = next(attempts)

    with txn:
        store.upsert(Mineral(qid="Q1", name="Quartz", mohs=7.0))

    next_attempt = next(attempts, None)
    m = store.get(Mineral, qid="Q1")
    assert next_attempt is None
    assert m is not None
    assert m.mohs == 7.0


def test_committing_propagates_block_errors_without_retrying(store: Store) -> None:
    """Given a committing() attempt whose block raises ValueError
    When the block exits
    Then the ValueError propagates
    And the attempt neither committed nor recorded a conflict
    And nothing was committed
    """
    txn = next(store.committing(retries=5))

    with pytest.raises(ValueError, match="boom"), txn:
        raise ValueError("boom")

    assert txn.committed is False
    assert txn.conflict is None
    assert store.last_tid == 0


def test_discard_unblocks_sync(
    coordinator: tuple[Store, DeltaLog], open_replica: Callable[..., Store]
) -> None:
    """Given a follower whose sync was refused for a buffered write, which it then discards
    When the coordinator commits a calcite and the follower syncs
    Then sync returns the coordinator's last tid
    And a local query finds the quartz and the calcite
    """
    coord, _ = coordinator
    follower = open_replica()
    follower.store(Mineral(qid="LOCAL", name="scratch"))
    with pytest.raises(RuntimeError):
        follower.sync()
    follower.discard()
    coord.store(Mineral(qid="Q2", name="Calcite"))
    coord.commit()

    applied = follower.sync()

    assert applied == coord.last_tid
    assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2"}


def test_catchup_refuses_a_delta_that_predates_the_contract() -> None:
    """Given a delta one version older than the contract
    When a follower syncs it as a catch-up
    Then the sync raises DeltaFormatError naming that it predates
    And the follower still holds no commit
    """
    served = FakeCoordinator()
    with open_follower("http://coord", client=served) as follower:
        served.deltas = [{**_stamped_delta(), "v": 1}]

        with pytest.raises(DeltaFormatError, match="predates"):
            follower.sync()

        assert follower.discard() == 0


def test_catchup_refuses_a_delta_newer_than_the_contract() -> None:
    """Given a delta one version newer than the contract
    When a follower syncs it as a catch-up
    Then the sync raises DeltaFormatError naming that it is newer
    And the follower still holds no commit
    """
    served = FakeCoordinator()
    with open_follower("http://coord", client=served) as follower:
        served.deltas = [{**_stamped_delta(), "v": CONTRACT_VERSION + 1}]

        with pytest.raises(DeltaFormatError, match="newer"):
            follower.sync()

        assert follower.discard() == 0


def test_catchup_refuses_a_frame_that_is_not_a_delta() -> None:
    """Given a frame whose format marker is not datacrystal-delta
    When a follower syncs it as a catch-up
    Then the sync raises DeltaFormatError naming that it is not a datacrystal delta
    And the follower still holds no commit
    """
    served = FakeCoordinator()
    with open_follower("http://coord", client=served) as follower:
        served.deltas = [{**_stamped_delta(), "f": "something-else"}]

        with pytest.raises(DeltaFormatError, match="not a datacrystal delta"):
            follower.sync()

        assert follower.discard() == 0


def test_catchup_still_applies_a_well_formed_empty_delta() -> None:
    """Given a well-formed empty delta of the current contract version
    When a follower syncs it as a catch-up
    Then the sync returns its TID 1
    """
    served = FakeCoordinator()
    with open_follower("http://coord", client=served) as follower:
        served.deltas = [_stamped_delta()]

        applied = follower.sync()

        assert applied == 1
