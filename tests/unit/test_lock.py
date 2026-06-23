"""Single-writer lease lock (ROADMAP item 2; fitness #8: injectable clock)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

import datacrystal as dc
from datacrystal._storage.lock import LeaseLock


def test_second_open_fails_loudly(tmp_path):
    first = dc.Store.open(tmp_path / "s")
    try:
        with pytest.raises(dc.StoreLockedError, match="single-writer"):
            dc.Store.open(tmp_path / "s")
    finally:
        first.close()
    # After a clean close the store opens again.
    second = dc.Store.open(tmp_path / "s")
    second.close()


def test_stale_lease_is_taken_over(tmp_path):
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    path = tmp_path / "used.lock"
    path.write_text(json.dumps({"pid": 1, "token": "dead", "ts": now[0]}))

    lock = LeaseLock(path, ttl=1.0, clock=clock)
    with pytest.raises(dc.StoreLockedError):
        lock.acquire()  # fresh lease: refused

    now[0] += 5.0  # > ttl * 2: the holder is dead
    lock.acquire()
    assert json.loads(path.read_text())["pid"] != 1
    lock.release()
    assert not path.exists()


def test_dead_holder_same_host_reclaimed_immediately(tmp_path):
    """A crashed/killed holder (dead pid, same host, FRESH timestamp) is stale at
    once via the pid-liveness check — no ~2*ttl wait before a restart reclaims it.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # spawn + reap → a dead pid
    proc.wait()
    path = tmp_path / "used.lock"
    path.write_text(json.dumps(
        {"pid": proc.pid, "token": "dead", "ts": time.time(),  # ts FRESH (ttl rule would NOT reclaim)
         "host": socket.gethostname()}
    ))
    lock = LeaseLock(path, ttl=10.0)  # ttl*2 = 20s; only liveness can reclaim a fresh lock
    lock.acquire()
    assert json.loads(path.read_text())["pid"] == os.getpid()
    lock.release()
    assert not path.exists()


def test_live_holder_same_host_stays_locked(tmp_path):
    """A live same-host holder is never stolen by the liveness check (only a dead
    pid is); a fresh lease held by a running process stays locked.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        path = tmp_path / "used.lock"
        path.write_text(json.dumps(
            {"pid": proc.pid, "token": "alive", "ts": time.time(), "host": socket.gethostname()}
        ))
        lock = LeaseLock(path, ttl=10.0)
        with pytest.raises(dc.StoreLockedError):
            lock.acquire()
    finally:
        proc.terminate()
        proc.wait()


def test_foreign_host_lock_falls_back_to_ttl(tmp_path):
    """A lock taken on another host (or a pre-host lock file) cannot be probed, so
    it falls back to the ttl staleness rule — a fresh foreign lock stays locked.
    """
    path = tmp_path / "used.lock"
    path.write_text(json.dumps(
        {"pid": 999999, "token": "remote", "ts": time.time(), "host": "some-other-host"}
    ))
    lock = LeaseLock(path, ttl=10.0)
    with pytest.raises(dc.StoreLockedError):
        lock.acquire()


def test_lost_lease_is_detected(tmp_path):
    path = tmp_path / "used.lock"
    lock = LeaseLock(path, ttl=0.15)
    lock.acquire()
    try:
        # Another process "takes over" while we are paused.
        path.write_text(json.dumps({"pid": 2, "token": "intruder", "ts": time.time()}))
        deadline = time.time() + 2.0
        while not lock.lost and time.time() < deadline:
            time.sleep(0.02)
        assert lock.lost
    finally:
        lock.release()
    # The intruder's lock file must not be deleted by our release.
    assert json.loads(path.read_text())["token"] == "intruder"
