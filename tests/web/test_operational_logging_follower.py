"""#195 — a follower that pulls new deltas reports the resync as a log record."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from datacrystal._follower import open_follower
from datacrystal._storage.memory import MemoryBackend
from datacrystal._store import Store
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router
from tests.conftest import Mineral
from tests.log_capture import logged


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
    coord = Store._from_backend(MemoryBackend())
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
    Then an info record names the watermark before the sync and the one after it
    """
    follower = open_follower("http://coord", client=client, path=follower_path)
    before = follower.sync()
    coord, _log = coordinator
    coord.store(Mineral(qid="Q2", name="azurite"))
    coord.commit()

    after = follower.sync()

    record = logged(caplog)["follower_resynced"]
    assert record.level == logging.INFO
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
