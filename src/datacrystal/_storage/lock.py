"""Single-writer lease lock file (ROADMAP item 2).

``used.lock`` holds ``{pid, token, ts}``; a daemon thread refreshes ``ts``.
A fresh lock makes a second opener fail with ``StoreLockedError``; a stale lock is taken over.
A refresher that finds another owner flags the lease lost, so the next commit raises
``LeaseLostError``; why a lock at all: ADR-001, "Implementation notes".
TTL and clock are injectable, so the lock suite needs no real-time sleeps (fitness function #8).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

from datacrystal._errors import StoreLockedError

LOCK_FILENAME = "used.lock"

logger = logging.getLogger(__name__)


class LeaseLock:
    def __init__(self, path: Path, *, ttl: float = 10.0,
                 clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._ttl = ttl
        self._clock = clock
        self._token = uuid.uuid4().hex
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def acquire(self) -> None:
        while True:
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self._read()
                reason = "unreadable" if holder is None else self._stale_reason(holder)
                if holder is not None and reason is None:
                    raise StoreLockedError(
                        f"store is in use by pid {holder.get('pid', '?')} "
                        f"(lock file {self._path}); datacrystal is single-writer — "
                        "run one process per store, or see SCALING.md for "
                        "multi-process recipes"
                    ) from None
                # Stale (holder died or never refreshed): take it over.
                try:
                    os.unlink(self._path)
                except FileNotFoundError:
                    pass
                else:
                    logger.warning("stale_lease_reclaimed", extra={
                        "reason": reason, "lease_ttl_seconds": self._ttl})
                continue
            with os.fdopen(fd, "w") as f:
                f.write(self._payload())
            break
        self._thread = threading.Thread(
            target=self._refresh_loop, name="datacrystal-lease", daemon=True
        )
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._ttl)
            self._thread = None
        if not self._lost.is_set():
            try:
                holder = self._read()
                if holder is not None and holder.get("token") == self._token:
                    os.unlink(self._path)
            except OSError:
                pass

    # -- internals ----------------------------------------------------------

    def _payload(self) -> str:
        return json.dumps(
            {"pid": os.getpid(), "token": self._token, "ts": self._clock(),
             "host": socket.gethostname()}
        )

    def _read(self) -> dict[str, object] | None:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return None
        if isinstance(data, dict):
            return cast("dict[str, object]", data)
        return None

    def _stale_reason(self, holder: dict[str, object]) -> str | None:
        """Return why the lock is stale (a reason code), or None while its holder is alive."""
        ts = holder.get("ts")
        if not isinstance(ts, (int, float)):
            return "unreadable"  # unreadable/corrupt lock counts as stale
        if (self._clock() - ts) > (self._ttl * 2):
            return "ttl_expired"  # the refresher stopped long enough ago: holder is gone
        # A same-host holder whose pid is gone is stale at once — a crashed or
        # -9'd writer must not lock the store for the whole ~2*ttl window before a
        # restart can reclaim it. Only probed when the lock was taken on THIS host
        # (a pre-host lock file, or one from another host/NFS, falls back to the
        # ttl rule above — we can't meaningfully probe a foreign pid).
        return "holder_process_gone" if self._holder_pid_is_dead(holder) else None

    def _holder_pid_is_dead(self, holder: dict[str, object]) -> bool:
        if holder.get("host") != socket.gethostname():
            return False  # different host (or no host recorded): probe is unsafe
        pid = holder.get("pid")
        if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
            return False  # unknown, or our own (pid-reuse): never claim dead
        try:
            os.kill(pid, 0)  # signal 0: existence check, delivers nothing
        except ProcessLookupError:
            return True  # no such process — the holder really is gone
        except OSError:
            return False  # exists but unsignalable (PermissionError), or can't tell
        return False  # signal accepted: the holder is alive

    def _refresh_loop(self) -> None:
        interval = self._ttl / 3
        while not self._stop.wait(interval):
            holder = self._read()
            if holder is not None and holder.get("token") not in (None, self._token):
                # Someone took over while we were paused: never write again.
                self._lost.set()
                return
            tmp = self._path.with_suffix(".lock.tmp")
            try:
                tmp.write_text(self._payload())
                os.replace(tmp, self._path)
            except OSError:
                pass
