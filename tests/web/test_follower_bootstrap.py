"""#150 — open_follower bootstrap: replay-from-0 into a real local store.

The load-bearing chain-B story. A follower turns the coordinator's
COMMIT-DELTA-v1 stream into a queryable local store that reproduces the
coordinator's committed state exactly — over BOTH backends — and every cut fails
closed: a gapped stream raises ``DeltaGapError`` with no partial apply, a
duplicate delta is an idempotent no-op.
"""

from __future__ import annotations

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


@pytest.fixture(params=["memory", "sqlite"])
def follower_backend(request, tmp_path) -> StorageBackend:
    """A fresh follower backend over both storage backends (the replica is a
    real Store, so it must behave identically on memory and sqlite)."""
    if request.param == "memory":
        return MemoryBackend()
    return SqliteBackend(tmp_path / "follower")


def _seed_coordinator(tmp_path) -> tuple[Store, DeltaLog]:
    """A coordinator with a DeltaLog, three commits (TIDs 1, 2, 3)."""
    coord = Store._from_backend(MemoryBackend())
    log = DeltaLog(tmp_path / "coordlog")
    coord.attach(log)
    gotthard = Locality(qid="L1", name="St Gotthard", country="CH")
    coord.store(
        Mineral(qid="Q1", name="Quartz", mohs=7.0, type_locality=dc.Lazy.of(gotthard))
    )
    coord.commit()
    coord.store(Mineral(qid="Q2", name="Calcite", mohs=3.0))
    coord.commit()
    coord.store(Mineral(qid="Q3", name="Gold", mohs=2.5))
    coord.commit()
    return coord, log


def test_follower_replays_leader_exactly(follower_backend, tmp_path) -> None:
    coord, log = _seed_coordinator(tmp_path)
    leader = ReferenceApplier()
    for delta in log.replay(after_tid=0):
        leader.apply(delta)
    try:
        follower_backend.boot()
        applier = _bootstrap_backend(follower_backend, log.replay(after_tid=0))
        # the delta stream was consumed identically (gaplessly, in order)
        assert applier.state_digest() == leader.state_digest()
        follower = Store._from_backend(follower_backend)
        try:
            assert follower.last_tid == coord.last_tid
            q1 = follower.get(Mineral, qid="Q1")
            assert q1 is not None
            assert q1.name == "Quartz" and q1.mohs == 7.0
            assert q1.type_locality is not None  # the ref field replicated
            loc = follower.get(Locality, qid="L1")  # the referent rode along
            assert loc is not None and loc.name == "St Gotthard"
            assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2", "Q3"}
        finally:
            follower.close()
    finally:
        coord.close()


def test_follower_refuses_a_gap_with_no_partial_apply(tmp_path) -> None:
    coord, log = _seed_coordinator(tmp_path)
    deltas = list(log.replay(after_tid=0))  # TIDs 1, 2, 3
    gapped = [deltas[0], deltas[2]]  # 1 then 3 — a gap at the apply of 3
    backend = MemoryBackend()
    backend.boot()
    try:
        with pytest.raises(DeltaGapError):
            _bootstrap_backend(backend, gapped)
        # fail closed: only delta 1 landed — watermark did NOT advance past the
        # gap, and delta 3's entity never appeared (no partial apply)
        follower = Store._from_backend(backend)
        try:
            assert follower.last_tid == deltas[0]["tid"]
            assert {m.qid for m in follower.query(Mineral)} == {"Q1"}
        finally:
            follower.close()
    finally:
        coord.close()


def test_follower_idempotent_on_duplicate_delta(tmp_path) -> None:
    coord, log = _seed_coordinator(tmp_path)
    deltas = list(log.replay(after_tid=0))
    stream = [deltas[0], deltas[0], *deltas[1:]]  # delta 1 delivered twice
    backend = MemoryBackend()
    backend.boot()
    try:
        applier = _bootstrap_backend(backend, stream)
        assert applier.watermark == coord.last_tid
        follower = Store._from_backend(backend)
        try:
            # the duplicate did not double-apply: exactly the leader's entities
            assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2", "Q3"}
            assert follower.last_tid == coord.last_tid
        finally:
            follower.close()
    finally:
        coord.close()


def test_open_follower_over_http(tmp_path) -> None:
    pytest.importorskip("httpx", reason="pip install httpx (TestClient transport)")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from datacrystal.web import federation_router

    coord, log = _seed_coordinator(tmp_path)
    app = FastAPI()
    app.include_router(federation_router(coord, log))
    try:
        with TestClient(app) as client:
            follower = open_follower("http://coordinator", client=client)
            try:
                m = follower.get(Mineral, qid="Q1")
                assert m is not None and m.name == "Quartz"
                assert follower.last_tid == coord.last_tid
                assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2", "Q3"}
            finally:
                follower.close()
    finally:
        coord.close()
