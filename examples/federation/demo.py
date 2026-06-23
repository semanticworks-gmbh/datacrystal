"""Fractal followers, live: one coordinator + two edge followers over real HTTP.

Run it::

    uv run python examples/federation/demo.py

This is a *real* application, not a unit test: it stands up a genuine uvicorn
coordinator (the single writer) on a background thread, opens two
``dc.open_follower`` replicas that talk to it over localhost TCP, and walks the
whole fractal-followers surface — bootstrap, local reads, contribute, sync,
cross-entity references, the OCC conflict + **recovery loop**, and every
fail-closed guard — printing ``✓``/``✗`` for each behaviour. Exit code ``0``
means every check passed.

The followers run the SAME codebase as the coordinator; role is config. Each is
a real local datacrystal store that bootstraps by replaying the coordinator's
COMMIT-DELTA-v1 stream from TID 0, then reads at full local speed and contributes
writes back through the single writer (ADR-001 owner-confined fan-in).

Needs ``datacrystal[web]`` (FastAPI) plus ``uvicorn`` and ``httpx`` on the
coordinator side, and ``datacrystal[follower]`` (httpx + pydantic) on a follower.
The repo's ``uv sync --all-extras`` provides all of them.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any

import httpx
import uvicorn
from fastapi import FastAPI

import datacrystal as dc
from datacrystal import ConflictError
from datacrystal.deltalog import DeltaLog
from datacrystal.web import federation_router

# --- the model: a mineral cabinet (the one datacrystal domain) ----------------


@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str
    country: Annotated[str | None, dc.Index] = None


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None
    tags: list = field(default_factory=list)  # a bare list — used by a guard demo


@dc.entity
class Specimen:
    """A specimen with an out-of-line scanned label (a ``dc.Blob`` field)."""

    qid: Annotated[str, dc.Unique]
    scan: Annotated[bytes, dc.Blob] = b""


# --- a tiny ✓/✗ check harness (exit non-zero if anything fails) ---------------

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def mineral_graph(store: dc.Store) -> dict[str, tuple[str, float | None, str | None]]:
    """A comparable snapshot of every Mineral: qid → (name, mohs, type-locality qid)."""
    out: dict[str, tuple[str, float | None, str | None]] = {}
    for m in store.query(Mineral):
        tloc = m.type_locality.get() if m.type_locality is not None else None
        out[m.qid] = (m.name, m.mohs, tloc.qid if tloc is not None else None)
    return out


# --- a real uvicorn coordinator on a background thread ------------------------


class Coordinator:
    """A real single-writer coordinator: a Store + DeltaLog + federation_router,
    opened and seeded ON the server thread (so that thread owns the store and the
    async ``/v1/submit`` fan-in runs inline — ADR-001), served by uvicorn.
    """

    def __init__(self, store_path: Path, log_path: Path) -> None:
        self._store_path = store_path
        self._log_path = log_path
        self.port = _free_port()
        self._server: uvicorn.Server | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="coordinator", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._error is not None:
                raise RuntimeError("coordinator failed to start") from self._error
            if self._server is not None and self._server.started:
                return
            time.sleep(0.02)
        raise TimeoutError("coordinator did not become ready in 10s")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        self._thread.join(timeout=10)

    def _run(self) -> None:
        try:
            store = dc.Store.open(self._store_path)  # opened HERE → this thread owns it
            log = DeltaLog(self._log_path)
            store.attach(log)
            # seed the coordinator's collection on the owner thread, before serving.
            # The localities and the minerals that reference them commit in ONE batch:
            # P1 allocates an OID for every new entity before encoding, so a Lazy.of()
            # to a sibling-in-the-same-commit resolves (the engine's own idiom).
            tsumeb = Locality(qid="LT", name="Tsumeb", country="NA")
            store.store(tsumeb)
            store.store(Locality(qid="LZ", name="Zomba", country="MW"))
            store.store(Mineral(qid="Q1", name="Quartz", crystal_system="trigonal", mohs=7.0,
                                type_locality=dc.Lazy.of(tsumeb)))
            store.store(Mineral(qid="Q2", name="Calcite", crystal_system="trigonal", mohs=3.0))
            store.commit()

            app = FastAPI()
            app.include_router(federation_router(store, log))
            server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=self.port,
                               log_level="error", lifespan="off")
            )
            self._server = server
            try:
                server.run()  # owns the event loop on THIS thread until should_exit
            finally:
                store.close()
        except BaseException as exc:  # noqa: BLE001 - surface startup failure to start()
            self._error = exc


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _expect_raises(label: str, exc_type: type[BaseException], thunk: Any) -> None:
    """Run ``thunk``; ✓ if it raises ``exc_type`` (a fail-closed guard firing)."""
    try:
        thunk()
        check(label, False, f"expected {exc_type.__name__}, nothing raised")
    except exc_type as exc:
        check(label, True, f"{exc_type.__name__}: {str(exc).splitlines()[0][:70]}")
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"raised {type(exc).__name__} instead of {exc_type.__name__}: {exc}")


def main() -> int:
    print("datacrystal — fractal followers (live coordinator + 2 edge followers)\n")
    with TemporaryDirectory(prefix="dc-federation-demo-") as tmp:
        root = Path(tmp)
        coord = Coordinator(root / "coordinator.store", root / "coordinator.log")
        coord.start()
        print(f"coordinator (single writer) serving at {coord.url}")

        edge_a = dc.open_follower(coord.url)            # in-memory replica
        edge_b = dc.open_follower(coord.url, path=root / "edge_b.replica")  # sqlite replica
        try:
            section("1. Bootstrap — each follower is a real local store replayed from TID 0")
            qa = edge_a.get(Mineral, qid="Q1")
            qb = edge_b.get(Mineral, qid="Q1")
            check("follower A read Q1 locally", qa is not None and qa.mohs == 7.0,
                  f"{qa.name} mohs={qa.mohs}" if qa else "missing")
            check("follower B (sqlite) read Q1 locally", qb is not None and qb.name == "Quartz")
            tl = qa.type_locality.get() if qa and qa.type_locality else None
            check("cross-entity reference resolves on the follower",
                  tl is not None and tl.qid == "LT", f"Q1.type_locality → {tl.name if tl else '∅'}")

            section("2. Contribute — a follower writes back through the single writer")
            edge_a.upsert(Mineral(qid="Q3", name="Topaz", crystal_system="orthorhombic", mohs=8.0))
            applied = edge_a.commit()
            edge_b.sync()
            topaz_b = edge_b.get(Mineral, qid="Q3")
            check("A's new Topaz committed on the coordinator", applied is not None and applied > 0,
                  f"applied_tid={applied}")
            check("B sees Topaz after sync()", topaz_b is not None and topaz_b.mohs == 8.0)

            section("3. Cross-entity contribute — a ref to an already-committed entity")
            zomba = edge_a.get(Locality, qid="LZ")
            edge_a.upsert(Mineral(qid="Q4", name="Smithsonite", mohs=4.5,
                                  type_locality=dc.Lazy.of(zomba)))
            edge_a.commit()
            edge_b.sync()
            q4 = edge_b.get(Mineral, qid="Q4")
            q4_loc = q4.type_locality.get() if q4 and q4.type_locality else None
            check("the reference rides the wire as an OID and resolves on B",
                  q4_loc is not None and q4_loc.qid == "LZ", f"Q4 → {q4_loc.name if q4_loc else '∅'}")

            section("4. Concurrent edit of the SAME object — OCC + automatic recovery")
            # First, the raw primitive: a stale commit is DETECTED, never last-writer-wins.
            a_q2 = edge_a.get(Mineral, qid="Q2")  # both read Q2 (mohs=3.0)
            b_q2 = edge_b.get(Mineral, qid="Q2")
            assert a_q2 is not None and b_q2 is not None
            a_q2.mohs = 3.5
            edge_a.commit()                         # A wins
            b_q2.mohs = 9.9
            conflicted = False
            try:
                edge_b.commit()                     # B's stale raw commit must be rejected
            except ConflictError as exc:
                conflicted = True
                check("a stale commit() raises ConflictError (no last-writer-wins)", True,
                      f"actual_base={str(exc.actual_base)[:12]}…")
            if not conflicted:
                check("a stale commit() raises ConflictError (no last-writer-wins)", False)
            edge_b.discard()  # drop B's rejected raw edit, then recover the CLEAN way:
            # store.committing() is the same code on a single-node store and a follower —
            # it re-runs the block against fresh state on a conflict (discard+sync inside),
            # so a read-modify-write applies your INTENT to the winning value, never a clobber.
            for txn in edge_b.committing(retries=5):
                with txn:
                    q2 = edge_b.get(Mineral, qid="Q2")
                    assert q2 is not None
                    q2.mohs = (q2.mohs or 0) + 0.5   # increment: re-read + re-applied each try
            edge_a.sync()
            a_q2_final = edge_a.get(Mineral, qid="Q2")
            check("committing() recovers + converges automatically (no hand-written loop)",
                  a_q2_final is not None and a_q2_final.mohs == 4.0,
                  "Q2: 3.0 → 3.5 (A) → +0.5 applied to A's 3.5 = 4.0 — no lost update")

            section("5. Fail-closed guards — unsafe contributions never cross the wire")
            loc_lt = edge_a.get(Locality, qid="LT")
            _expect_raises(
                "a dc.Blob field is rejected (no create-face wire shape)",
                NotImplementedError,
                lambda: (edge_a.upsert(Specimen(qid="SP", scan=b"\x89PNG binary")),
                         edge_a.commit()),
            )
            edge_a.discard()
            _expect_raises(
                "an @entity nested in a bare list is rejected (would corrupt to an int)",
                NotImplementedError,
                lambda: (edge_a.upsert(Mineral(qid="Q9", name="Beryl", tags=[loc_lt])),
                         edge_a.commit()),
            )
            edge_a.discard()
            new_loc = Locality(qid="LX", name="Uncommitted")
            _expect_raises(
                "an intra-batch new→new reference is rejected",
                NotImplementedError,
                lambda: (edge_a.store(new_loc),
                         edge_a.store(Mineral(qid="Q8", name="Azurite",
                                              type_locality=dc.Lazy.of(new_loc))),
                         edge_a.commit()),
            )
            edge_a.discard()
            with httpx.Client(base_url=coord.url) as raw:
                bad = raw.post("/v1/submit", json={"ops": [{"type": "x", "key": "k", "fields": "NO"}]})
                check("a malformed /v1/submit envelope is a 422, not a 500/409",
                      bad.status_code == 422, f"HTTP {bad.status_code}")

            section("6. Convergence — coordinator and both followers agree on the whole graph")
            edge_a.sync()
            edge_b.sync()
            witness = dc.open_follower(coord.url)  # a fresh replica = the coordinator's truth
            try:
                g_truth = mineral_graph(witness)
                g_a = mineral_graph(edge_a)
                g_b = mineral_graph(edge_b)
                check("follower A == coordinator", g_a == g_truth)
                check("follower B == coordinator", g_b == g_truth)
                print(f"\n  final graph ({len(g_truth)} minerals): " +
                      ", ".join(f"{q}={v[1]}" for q, v in sorted(g_truth.items())))
            finally:
                witness.close()
        finally:
            edge_a.close()
            edge_b.close()
            coord.stop()

    print()
    if _failures:
        print(f"\033[1m✗ {len(_failures)} check(s) FAILED:\033[0m " + "; ".join(_failures))
        return 1
    print("\033[1m✓ all checks passed — the federation surface works end-to-end.\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
