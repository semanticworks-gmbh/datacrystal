"""#151 — follower catch-up: Store.sync() refreshes a live replica.

Bootstrap (#150) built a fresh store; catch-up updates an already-open one. The
follower's reads/sync are owner-thread + synchronous, so a sync TestClient (no
event loop needed) drives it directly. Over both follower backends.
"""

from __future__ import annotations

import pytest

pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from datacrystal._follower import _apply_catchup, _bootstrap_backend, open_follower
from datacrystal._storage.memory import MemoryBackend
from datacrystal._store import Store
from datacrystal.contract.applier import DeltaGapError
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router
from tests.conftest import Mineral


@pytest.fixture(params=["memory", "sqlite"])
def follower_path(request, tmp_path):
    """The replica lives in memory, or sqlite-backed at a path — both must work."""
    return None if request.param == "memory" else tmp_path / "follower"


def _coordinator(tmp_path) -> tuple[Store, DeltaLog]:
    coord = Store._from_backend(MemoryBackend())
    log = DeltaLog(tmp_path / "coordlog")
    coord.attach(log)
    coord.store(Mineral(qid="Q1", name="Quartz"))
    coord.commit()
    return coord, log


def test_follower_syncs_new_commits(follower_path, tmp_path) -> None:
    coord, log = _coordinator(tmp_path)
    app = FastAPI()
    app.include_router(federation_router(coord, log))
    try:
        with TestClient(app) as client:
            follower = open_follower("http://coord", client=client, path=follower_path)
            try:
                assert {m.qid for m in follower.query(Mineral)} == {"Q1"}
                before = follower.last_tid
                # coordinator moves on
                coord.store(Mineral(qid="Q2", name="Calcite"))
                coord.commit()
                coord.store(Mineral(qid="Q3", name="Gold"))
                coord.commit()
                # the follower catches up in place
                applied = follower.sync()
                assert applied == coord.last_tid and applied > before
                assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2", "Q3"}
                got = follower.get(Mineral, qid="Q3")
                assert got is not None and got.name == "Gold"
                # syncing again with nothing new is a no-op
                assert follower.sync() == applied
            finally:
                follower.close()
    finally:
        coord.close()


def test_sync_catchup_refuses_a_gap_no_partial_apply(tmp_path) -> None:
    coord, log = _coordinator(tmp_path)
    coord.store(Mineral(qid="Q2", name="Calcite"))
    coord.commit()
    coord.store(Mineral(qid="Q3", name="Gold"))
    coord.commit()
    deltas = list(log.replay(after_tid=0))  # TIDs 1, 2, 3
    try:
        backend = MemoryBackend()
        backend.boot()
        _bootstrap_backend(backend, [deltas[0]])  # the replica sits at TID 1
        with pytest.raises(DeltaGapError):
            _apply_catchup(backend, [deltas[2]])  # TID 3 over watermark 1 — a gap
        # fail closed: watermark unchanged, the gapped delta's entity never landed
        assert int(backend.boot().meta["next_tid"]) - 1 == 1
        replica = Store._from_backend(backend)
        try:
            assert {m.qid for m in replica.query(Mineral)} == {"Q1"}
        finally:
            replica.close()
    finally:
        coord.close()


def test_sync_on_non_follower_raises(store) -> None:
    with pytest.raises(RuntimeError):
        store.sync()
    assert store.last_tid == 0  # not a follower — nothing happened


def test_sync_refuses_with_buffered_writes(tmp_path) -> None:
    coord, log = _coordinator(tmp_path)
    app = FastAPI()
    app.include_router(federation_router(coord, log))
    try:
        with TestClient(app) as client:
            follower = open_follower("http://coord", client=client)
            try:
                follower.store(Mineral(qid="LOCAL", name="scratch"))  # buffer a write
                with pytest.raises(RuntimeError):
                    follower.sync()
                assert follower.last_tid == coord.last_tid  # sync refused, unchanged
            finally:
                follower.close()
    finally:
        coord.close()
