"""Single-writer lease lock file (ROADMAP item 2).

A port of EclipseStore's ``StorageLockFileManager`` idea: a ``used.lock``
file in the store directory holds ``{pid, token, ts}``; a daemon thread
refreshes the timestamp. A second opener finds a *fresh* lock and fails
loudly with ``StoreLockedError`` — ``uvicorn --workers 4`` silently
corrupting is the #1 foreseeable user error, and docs alone don't prevent
it. A *stale* lock (holder crashed) is taken over.

If the refresher ever finds the file owned by someone else (this process was
SIGSTOPped/slept past the TTL and another process took over), it flags the
lease as lost; the next commit raises ``LeaseLostError`` instead of risking
two-writer corruption.

TTL and clock are injectable (fitness function #8: the lock suite runs with
a sub-second TTL and no real-time sleeps).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, cast

from datacrystal._errors import StoreLockedError

LOCK_FILENAME = "used.lock"


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
                if holder is not None and not self._is_stale(holder):
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
            {"pid": os.getpid(), "token": self._token, "ts": self._clock()}
        )

    def _read(self) -> dict[str, object] | None:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return None
        if isinstance(data, dict):
            return cast("dict[str, object]", data)
        return None

    def _is_stale(self, holder: dict[str, object]) -> bool:
        ts = holder.get("ts")
        if not isinstance(ts, (int, float)):
            return True  # unreadable/corrupt lock counts as stale
        return (self._clock() - ts) > (self._ttl * 2)

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
