"""Single-writer lease lock (ROADMAP item 2; fitness #8: injectable clock)."""

from __future__ import annotations

import json
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
