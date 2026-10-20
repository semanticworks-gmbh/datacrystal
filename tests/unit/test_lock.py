"""Single-writer lease lock (ROADMAP item 2; fitness #8: injectable clock)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

import datacrystal as dc
from datacrystal._storage.lock import LeaseLock

LockBuilder = Callable[..., LeaseLock]


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _write_holder_file(path: Path, holder: Mapping[str, object]) -> None:
    path.write_text(json.dumps(holder))


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    """Return where the lease lock of the tests lives."""
    return tmp_path / "used.lock"


@pytest.fixture
def new_lock(lock_path: Path) -> Iterator[LockBuilder]:
    """Yield a builder for locks on lock_path and release every lock it built."""
    built: list[LeaseLock] = []

    def build(ttl: float, clock: Callable[[], float] = time.time) -> LeaseLock:
        lock = LeaseLock(lock_path, ttl=ttl, clock=clock)
        built.append(lock)
        return lock

    yield build
    for lock in built:
        lock.release()


@pytest.fixture
def dead_pid() -> int:
    """Return the pid of a process that has started and been reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@pytest.fixture
def live_pid() -> Iterator[int]:
    """Yield the pid of a process that runs until its stdin closes."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    yield process.pid
    process.terminate()
    process.wait()


@pytest.fixture
def usurped_lease(lock_path: Path, new_lock: LockBuilder) -> LeaseLock:
    """Return an acquired lease whose lock file another process took over and that noticed."""
    lease = new_lock(0.15)
    lease.acquire()
    _write_holder_file(lock_path, {"pid": 2, "token": "intruder", "ts": time.time()})
    if not lease._lost.wait(timeout=2.0):
        pytest.fail("the refresher did not notice the takeover within 2 seconds")
    return lease


def test_second_open_fails_loudly_while_the_first_is_open(tmp_path: Path) -> None:
    """Given a store open in this process
    When the same directory is opened a second time
    Then the open raises StoreLockedError naming the single-writer rule
    """
    with dc.Store.open(tmp_path / "s"), pytest.raises(dc.StoreLockedError, match="single-writer"):
        dc.Store.open(tmp_path / "s")


def test_store_opens_again_after_a_clean_close(tmp_path: Path) -> None:
    """Given a store that was opened and closed cleanly
    When the same directory is opened again
    Then a store is returned
    """
    dc.Store.open(tmp_path / "s").close()
    with dc.Store.open(tmp_path / "s") as reopened:
        assert isinstance(reopened, dc.Store)


def test_fresh_lease_is_refused(lock_path: Path, new_lock: LockBuilder) -> None:
    """Given a lock file written by another pid at the current instant
    When a lock is acquired on it
    Then acquiring raises StoreLockedError
    """
    clock = _Clock(1000.0)
    _write_holder_file(lock_path, {"pid": 1, "token": "dead", "ts": clock.now})
    lock = new_lock(1.0, clock)
    with pytest.raises(dc.StoreLockedError):
        lock.acquire()


def test_stale_lease_is_taken_over(lock_path: Path, new_lock: LockBuilder) -> None:
    """Given a lock file last refreshed more than two ttls ago
    When a lock is acquired on it
    Then the lock file names this process
    """
    _write_holder_file(lock_path, {"pid": 1, "token": "dead", "ts": 1000.0})
    lock = new_lock(1.0, _Clock(1005.0))
    lock.acquire()
    assert json.loads(lock_path.read_text())["pid"] == os.getpid()


def test_release_removes_the_lock_file(lock_path: Path, new_lock: LockBuilder) -> None:
    """Given an acquired lock
    When it is released
    Then the lock file is gone
    """
    lock = new_lock(10.0)
    lock.acquire()
    lock.release()
    assert not lock_path.exists()


def test_dead_holder_on_this_host_is_reclaimed_immediately(
    lock_path: Path, new_lock: LockBuilder, dead_pid: int
) -> None:
    """Given a fresh lock file held by a dead pid on this host
    When a lock with a ttl of ten seconds is acquired on it
    Then the lock file names this process
    """
    holder = {"pid": dead_pid, "token": "dead", "ts": time.time(), "host": socket.gethostname()}
    _write_holder_file(lock_path, holder)
    lock = new_lock(10.0)
    lock.acquire()
    assert json.loads(lock_path.read_text())["pid"] == os.getpid()


def test_live_holder_on_this_host_stays_locked(
    lock_path: Path, new_lock: LockBuilder, live_pid: int
) -> None:
    """Given a fresh lock file held by a running pid on this host
    When a lock is acquired on it
    Then acquiring raises StoreLockedError
    """
    holder = {"pid": live_pid, "token": "alive", "ts": time.time(), "host": socket.gethostname()}
    _write_holder_file(lock_path, holder)
    lock = new_lock(10.0)
    with pytest.raises(dc.StoreLockedError):
        lock.acquire()


def test_fresh_foreign_host_lock_falls_back_to_ttl_and_stays_locked(
    lock_path: Path, new_lock: LockBuilder
) -> None:
    """Given a fresh lock file written on another host
    When a lock is acquired on it
    Then acquiring raises StoreLockedError
    """
    holder = {"pid": 999999, "token": "remote", "ts": time.time(), "host": "some-other-host"}
    _write_holder_file(lock_path, holder)
    lock = new_lock(10.0)
    with pytest.raises(dc.StoreLockedError):
        lock.acquire()


def test_lost_lease_is_detected(lock_path: Path, new_lock: LockBuilder) -> None:
    """Given an acquired lock with a short ttl
    When another process takes the lock file over
    Then the lock reports its lease lost
    """
    lease = new_lock(0.15)
    lease.acquire()
    _write_holder_file(lock_path, {"pid": 2, "token": "intruder", "ts": time.time()})
    lease._lost.wait(timeout=2.0)
    assert lease.lost


def test_release_after_a_lost_lease_keeps_the_intruders_lock_file(
    lock_path: Path, usurped_lease: LeaseLock
) -> None:
    """Given a lease whose lock file another process took over
    When the lease is released
    Then the intruder's lock file is still there
    """
    usurped_lease.release()
    assert json.loads(lock_path.read_text())["token"] == "intruder"
