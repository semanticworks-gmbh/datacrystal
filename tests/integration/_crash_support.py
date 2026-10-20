"""What the crash-test subprocess apps share: reopen a killed store, report facts as JSON."""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any

import datacrystal as dc

STALE_LEASE_DEADLINE_SECONDS = 10.0
_LEASE_POLL = threading.Event()  # never set: waiting on it is a bounded pause between retries


def open_store_when_lease_is_stale(path: str, *, cache_index: bool = False) -> dc.Store:
    """Open the store of a killed writer as soon as the dead writer's lease has gone stale.

    The lease is wall-clock based by design, so the retry is the one place a crash test waits on
    time: it waits for the store to open, not for a guessed number of seconds.
    """
    deadline = time.monotonic() + STALE_LEASE_DEADLINE_SECONDS
    while True:
        try:
            return dc.Store.open(path, lock_ttl=0.1, cache_index=cache_index)
        except dc.StoreLockedError:
            if time.monotonic() > deadline:
                raise
            _LEASE_POLL.wait(0.05)


def emit_report(report: dict[str, Any]) -> None:
    """Print the facts the verifier found as the last stdout line, one JSON object."""
    sys.stdout.write(json.dumps(report) + "\n")
    sys.stdout.flush()


def ack(value: int) -> None:
    """Tell the test one acked batch or TID, on its own stdout line, flushed at once."""
    sys.stdout.write(f"{value}\n")
    sys.stdout.flush()
