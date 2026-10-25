"""#195 — a follower that pulls new deltas reports the resync as a log record."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from datacrystal import Store, open_follower
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router
from tests.conftest import Mineral
from tests.log_capture import logged
from tests.web.test_federation_conformance import _CoordinatorServer, _started_server


@pytest.fixture(autouse=True)
def datacrystal_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Capture everything the library logs from INFO up."""
    caplog.set_level(logging.INFO, logger="datacrystal")


@pytest.fixture(params=["memory", "sqlite"])
def follower_path(request: pytest.FixtureRequest, tmp_path: Path) -> Path | None:
    """Place the replica in memory, or sqlite-backed at a path."""
    return None if request.param == "memory" else tmp_path / "follower"


@pytest.fixture
def coordinator(tmp_path: Path) -> Iterator[tuple[Store, DeltaLog]]:
    """Provide a coordinator with a DeltaLog and one committed quartz."""
    coord = Store.open(tmp_path / "coord", durability="never")
    log = DeltaLog(tmp_path / "coordlog")
    coord.attach(log)
    coord.store(Mineral(qid="Q1", name="quartz"))
    coord.commit()
    yield coord, log
    coord.close()


@pytest.fixture
def client(coordinator: tuple[Store, DeltaLog]) -> Iterator[TestClient]:
    """Serve the coordinator through the federation router."""
    coord, log = coordinator
    app = FastAPI()
    app.include_router(federation_router(coord, log))
    with TestClient(app) as test_client:
        yield test_client


def test_a_sync_that_applies_new_deltas_is_logged_with_both_watermarks(
    coordinator: tuple[Store, DeltaLog], client: TestClient, follower_path: Path | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Given a follower opened while the coordinator held one quartz, which then gained an azurite
    When the follower syncs
    Then an info record names the request as the reason and the watermarks before and after
    """
    follower = open_follower("http://coord", client=client, path=follower_path)
    before = follower.sync()
    coord, _log = coordinator
    coord.store(Mineral(qid="Q2", name="azurite"))
    coord.commit()

    after = follower.sync()

    record = logged(caplog)["follower_resynced"]
    assert record.level == logging.INFO
    assert record.fields["reason"] == "requested"
    assert record.fields["from_watermark"] == before
    assert record.fields["to_watermark"] == after
    follower.close()


def test_a_sync_without_new_deltas_logs_nothing(
    client: TestClient, follower_path: Path | None, caplog: pytest.LogCaptureFixture
) -> None:
    """Given a follower that is up to date with the coordinator
    When the follower syncs
    Then no resync is logged
    """
    follower = open_follower("http://coord", client=client, path=follower_path)

    follower.sync()

    assert "follower_resynced" not in logged(caplog)
    follower.close()


@pytest.fixture
def live_coordinator(tmp_path: Path) -> Iterator[_CoordinatorServer]:
    """Serve an in-memory coordinator over real HTTP, which accepts a follower's submit."""
    server = _started_server("memory", tmp_path)
    yield server
    server.stop()


def test_a_follower_commit_logs_no_resync(
    live_coordinator: _CoordinatorServer, caplog: pytest.LogCaptureFixture
) -> None:
    """Given a follower holding one buffered azurite
    When the follower commits it
    Then no resync is logged, since the commit path logs nothing
    """
    follower = open_follower(live_coordinator.base_url, path=None)
    follower.store(Mineral(qid="Q9", name="azurite"))
    caplog.clear()

    follower.commit()

    assert "follower_resynced" not in logged(caplog)
    follower.close()
