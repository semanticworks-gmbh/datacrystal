"""Live federation follower — watch the coordinator's changes, or poke it in a REPL.

Run it (terminal 2, while coordinator.py runs)::

    uv run python examples/federation/follower.py watch          # poll + print the heartbeat
    uv run python examples/federation/follower.py repl           # connect, drop into a REPL
    uv run python examples/federation/follower.py watch --url http://127.0.0.1:9000

``watch`` opens a real local replica (``Store.follower``) and every second syncs
and prints the coordinator's ``heartbeat`` counter — you watch it climb as the
coordinator ticks. ``repl`` connects and drops you into an interactive Python REPL
with helpers (``edge``, ``show()``, ``sync()``, ``bump()``, ``contribute()``)
pre-loaded so you can test behaviour by hand and edit the helpers below.

Declares the SAME ``@entity`` classes as coordinator.py — run both as scripts so
both are ``__main__`` and the persisted typenames match. **Keep the model block in
sync with coordinator.py.** Needs ``datacrystal[follower]`` (httpx + pydantic).
"""

from __future__ import annotations

import argparse
import code
import time
from typing import Annotated

import datacrystal as dc

# --- the model (KEEP IN SYNC WITH coordinator.py) -----------------------------


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
    name: Annotated[str, dc.Unique]
    value: int = 0


# --- follower -----------------------------------------------------------------


def _connect(url: str) -> dc.Store:
    """Open a follower replica, waiting briefly for the coordinator to come up."""
    import httpx

    for attempt in range(20):
        try:
            return dc.Store.follower(url)
        except httpx.HTTPError:
            if attempt == 0:
                print(f"[follower] waiting for a coordinator at {url} ...")
            time.sleep(0.5)
    raise SystemExit(f"[follower] no coordinator at {url} — is coordinator.py running?")


def _show(edge: dc.Store) -> None:
    beat = edge.get(Counter, name="heartbeat")
    minerals = {m.qid: m.mohs for m in edge.query(Mineral)}
    hb = beat.value if beat is not None else "?"
    print(f"watermark={edge.last_tid}  heartbeat={hb}  minerals={minerals}")


def watch(url: str, period: float, count: int) -> None:
    edge = _connect(url)
    print(f"[follower] watching {url} — Ctrl-C to stop")
    rounds = 0
    try:
        while count == 0 or rounds < count:
            edge.sync()
            _show(edge)
            rounds += 1
            if count and rounds >= count:
                break
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        edge.close()


def repl(url: str) -> None:
    edge = _connect(url)

    def show() -> None:
        """Print the heartbeat + minerals at the current local watermark."""
        _show(edge)

    def sync() -> int:
        """Pull the coordinator's latest, then show()."""
        watermark = edge.sync()
        _show(edge)
        return watermark

    def bump(qid: str, by: float = 0.5) -> None:
        """Contribute: increment a mineral's mohs via committing() (retries on conflict)."""
        for txn in edge.committing(retries=5):
            with txn:
                mineral = edge.get(Mineral, qid=qid)
                if mineral is None:
                    raise KeyError(f"no Mineral {qid!r} — try sync() first")
                mineral.mohs = (mineral.mohs or 0) + by
        fresh = edge.get(Mineral, qid=qid)
        print(f"contributed: {qid}.mohs -> {fresh.mohs if fresh else '?'}")

    def contribute(qid: str, name: str, mohs: float | None = None) -> None:
        """Contribute a mineral via committing(), so re-sending an existing key
        converges (updates) instead of raising a bare ConflictError."""
        for txn in edge.committing(retries=5):
            with txn:
                edge.upsert(Mineral(qid=qid, name=name, mohs=mohs))
        print(f"contributed Mineral {qid!r}")

    banner = (
        "\ndatacrystal live follower REPL — in scope:\n"
        "  edge                          the follower Store (a real local replica)\n"
        "  show()                        heartbeat + minerals at the current watermark\n"
        "  sync()                        pull the coordinator's latest, then show()\n"
        "  bump(qid, by=0.5)             increment a mineral's mohs (contribute via committing())\n"
        "  contribute(qid, name, mohs)   add a new mineral to the coordinator\n"
        "  Mineral / Locality / Counter / dc\n"
        "try:  sync(); bump('Q1'); sync()\n"
    )
    ns: dict[str, object] = {
        "edge": edge, "show": show, "sync": sync, "bump": bump, "contribute": contribute,
        "dc": dc, "Mineral": Mineral, "Locality": Locality, "Counter": Counter,
    }
    try:
        code.interact(banner=banner, local=ns, exitmsg="[follower] bye")
    finally:
        edge.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="live federation follower")
    ap.add_argument("mode", choices=["watch", "repl"], help="watch = poll+print; repl = interactive")
    ap.add_argument("--url", default="http://127.0.0.1:8848")
    ap.add_argument("--period", type=float, default=1.0, help="watch poll period (seconds)")
    ap.add_argument("--count", type=int, default=0, help="watch: stop after N polls (0 = forever)")
    args = ap.parse_args()
    if args.mode == "watch":
        watch(args.url, args.period, args.count)
    else:
        repl(args.url)


if __name__ == "__main__":
    main()
