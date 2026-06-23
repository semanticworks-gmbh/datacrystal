"""Live federation coordinator — serves data and ticks a counter you can watch.

Run it (terminal 1)::

    uv run python examples/federation/coordinator.py
    uv run python examples/federation/coordinator.py --port 9000 --interval 5 --path /tmp/coord

It opens ONE single-writer store on the main thread — which also runs the uvicorn
event loop, so the store's owner thread *is* the loop thread (ADR-001) — seeds a
small mineral cabinet plus a ``heartbeat`` :class:`Counter`, mounts the federation
router, and runs a background task that increments the counter every ``--interval``
seconds and commits. So a follower can watch a real record change ride the wire.

Pair it with ``follower.py`` (terminal 2). Both scripts declare the SAME ``@entity``
classes (a follower can only resolve a type it defines under the same module path —
run both as scripts, so both are ``__main__``). **Keep the model block below in sync
with follower.py.** Needs ``datacrystal[web]`` plus an ASGI server (``pip install uvicorn``; the
repo's ``uv sync`` already includes it). Stop it with Ctrl-C — a clean shutdown releases the
single-writer lease, so an immediate restart on the same ``--path`` reclaims it at once (an abrupt
``kill -9`` leaves the lease to age out, ~20 s).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import mkdtemp
from typing import Annotated

import uvicorn
from fastapi import FastAPI

import datacrystal as dc
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router

# --- the model (KEEP IN SYNC WITH follower.py) --------------------------------


@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None


@dc.entity
class Counter:
    """The record the coordinator ticks every few seconds — watch it propagate."""

    name: Annotated[str, dc.Unique]
    value: int = 0


# --- coordinator --------------------------------------------------------------


def _seed(store: dc.Store) -> None:
    """Seed once (a reused on-disk store keeps its data)."""
    if store.root is not None:
        return
    tsumeb = Locality(qid="LT", name="Tsumeb")
    store.store(tsumeb)
    store.store(Mineral(qid="Q1", name="Quartz", mohs=7.0, type_locality=dc.Lazy.of(tsumeb)))
    store.store(Mineral(qid="Q2", name="Calcite", mohs=3.0))
    store.store(Counter(name="heartbeat", value=0))
    store.root = {"seeded": True}
    store.commit()


async def _ticker(store: dc.Store, interval: float) -> None:
    """Increment the heartbeat counter every ``interval`` seconds.

    Runs on the event-loop thread = the store's owner thread, so the mutation and
    commit are owner-confined (ADR-001). ``commit()`` is synchronous and briefly
    blocks the loop — fine at a multi-second interval for a demo.
    """
    while True:
        await asyncio.sleep(interval)
        beat = store.get(Counter, name="heartbeat")
        if beat is None:  # a foreign --path with no heartbeat counter — stop gracefully
            print("[coordinator] no 'heartbeat' counter in this store — ticker stopped "
                  "(use a fresh --path)", flush=True)
            return
        beat.value += 1
        store.commit()
        print(f"[coordinator] heartbeat -> {beat.value}  (tid={store.last_tid})", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="live federation coordinator")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between ticks")
    ap.add_argument("--path", default=None, help="store dir (default: a fresh temp dir)")
    args = ap.parse_args()

    path = Path(args.path) if args.path else Path(mkdtemp(prefix="dc-coord-"))
    store = dc.Store.open(path)  # main thread = owner (and runs the loop below)
    log = DeltaLog(path / "deltalog")
    store.attach(log)
    _seed(store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        task = asyncio.create_task(_ticker(store, args.interval))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(lifespan=lifespan)
    app.include_router(federation_router(store, log))
    print(f"[coordinator] serving http://127.0.0.1:{args.port}   store={path}", flush=True)
    print(f"[coordinator] ticking 'heartbeat' every {args.interval}s — Ctrl-C to stop", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        store.close()  # releases the single-writer lease (a clean stop frees it at once)
        log.close()


if __name__ == "__main__":
    main()
