"""Sprint 13 / #156 — the federation conformance capstone.

This is the END-TO-END proof of the fractal-followers surface: a REAL uvicorn
HTTP/1.1 server runs the coordinator on a dedicated thread, and real
``open_follower`` replicas on the test thread talk to it over real localhost
TCP. Everything below #156 (read endpoints, submit fan-in, follower bootstrap/
sync/contribute, the OCC + schema-skew + gap guards) is unit-tested in-process;
this file proves they compose under a production-shaped runtime.

The harness requirement (ADR-001 owner-confinement): ``POST /v1/submit`` does
owner-confined writes — its async handler runs ``store.submit(fan_in)``, which
executes INLINE only when the calling thread is the store's owner. The handler
runs on the uvicorn event-loop thread, so the coordinator's :class:`Store` MUST
be opened on that same thread. (A sync ``TestClient`` against an in-thread store
deadlocks: the test thread blocks in the POST while the closure waits for the
owner to pump it — which is the test thread.) So: open + seed the coordinator
INSIDE the server thread before serving; followers live on the test thread.

Parametrized over BOTH backends (memory + sqlite) — the C12 fitness meta-gate
(``tests/fitness/test_federation_gate.py``) requires it, and a memory-only
follower test silently never exercises the sqlite read/commit path. Every
fail-closed scenario asserts the POST-STATE (the bad value did NOT land), not
merely that an exception was raised.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="pip install httpx (federation transport)")
pytest.importorskip("uvicorn", reason="pip install uvicorn (real-server harness)")
pytest.importorskip("pydantic", reason="contribute serializes via to_pydantic")

import httpx
import uvicorn
from fastapi import FastAPI

import datacrystal as dc
from datacrystal import ConflictError
from datacrystal._storage.memory import MemoryBackend
from datacrystal.contract.applier import DeltaGapError
from datacrystal.deltalog import DeltaLog
from datacrystal.testing import CountingConsumer, check_delta_consumer
from datacrystal.web import federation_router
from tests.conftest import Locality, Mineral
from tests.web._fake_coordinator import FrameDroppingClient, decode_frames

_MINERAL = f"{Mineral.__module__}:{Mineral.__qualname__}"


def _apply_all(consumer: CountingConsumer, deltas: list[dict]) -> None:
    for delta in deltas:
        consumer.apply(delta)


def _free_port() -> int:
    """Bind ``:0`` to claim a free TCP port, then release it for uvicorn."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _ReadyServer(uvicorn.Server):
    """A uvicorn server that sets an event once it accepts connections."""

    def __init__(self, config: uvicorn.Config, ready: threading.Event) -> None:
        super().__init__(config)
        self._ready = ready

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        self._ready.set()


class _CoordinatorServer:
    """A real uvicorn server running the coordinator on a dedicated thread.

    The store + DeltaLog are opened and seeded ON the server thread (so that
    thread is the store's ADR-001 owner and the async ``/v1/submit`` handler's
    ``store.submit`` runs inline). The DeltaLog is exposed for the
    ``check_delta_consumer`` scenario, which reads the served change-feed.
    """

    def __init__(self, open_store: Callable[[], dc.Store], log_dir: str) -> None:
        self._open_store = open_store
        self._log_dir = log_dir
        self.port = _free_port()
        self.store: dc.Store | None = None
        self.log: DeltaLog | None = None
        self._server: uvicorn.Server | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="coordinator", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise TimeoutError("coordinator server did not become ready within 10s")
        if self._error is not None:
            raise RuntimeError("coordinator server failed to start") from self._error

    def stop(self) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True
        self._thread.join(timeout=10)

    def _run(self) -> None:
        try:
            store = self._open_store()
            log = DeltaLog(self._log_dir)
            store.attach(log)
            # Seed coordinator data on the owner thread BEFORE serving.
            store.store(Locality(qid="LZ", name="Zomba", country="MW"))
            store.store(Locality(qid="LT", name="Tsumeb", country="NA"))
            store.store(Mineral(qid="Q1", name="Quartz", crystal_system="trigonal", mohs=7.0))
            store.store(Mineral(qid="Q2", name="Calcite", crystal_system="trigonal", mohs=3.0))
            store.commit()
            self.store = store
            self.log = log

            app = FastAPI()
            app.include_router(federation_router(store, log))
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                lifespan="off",
            )
            server = _ReadyServer(config, self._ready)
            self._server = server
            try:
                server.run()  # owns the event loop on THIS thread until should_exit
            finally:
                store.close()  # close on the owner thread (ADR-001)
        except BaseException as exc:  # noqa: BLE001 - surface the startup failure to start()
            self._error = exc
            self._ready.set()


def _started_server(
    backend_kind: str, tmp_path: Path, principal: dc.Principal | None = None
) -> _CoordinatorServer:
    """Start a coordinator over the memory or the sqlite backend, opened on the server thread."""
    if backend_kind == "memory":
        backend = MemoryBackend()

        def open_store() -> dc.Store:
            return dc.Store._from_backend(backend, principal=principal)

    else:

        def open_store() -> dc.Store:
            return dc.Store.open(tmp_path / "coord", principal=principal)

    server = _CoordinatorServer(open_store, log_dir=str(tmp_path / "coordlog"))
    server.start()
    return server


@pytest.fixture(params=["memory", "sqlite"])
def coordinator(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[_CoordinatorServer]:
    """Start a live coordinator over BOTH backends, opened on the server thread."""
    server = _started_server(request.param, tmp_path)
    yield server
    server.stop()


@pytest.fixture(params=["memory", "sqlite"])
def operator_coordinator(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[_CoordinatorServer]:
    """Start a live coordinator over BOTH backends whose ambient principal has uid 42."""
    server = _started_server(request.param, tmp_path, principal=dc.Principal(uid=42))
    yield server
    server.stop()


@pytest.fixture
def connect_follower() -> Iterator[Callable[[_CoordinatorServer], dc.Store]]:
    """Provide a factory opening in-memory followers of a coordinator, all closed at teardown."""
    opened: list[dc.Store] = []

    def connect(server: _CoordinatorServer) -> dc.Store:
        follower = dc.open_follower(server.base_url, path=None)
        opened.append(follower)
        return follower

    yield connect
    for follower in opened:
        follower.close()


@pytest.fixture
def connect_http() -> Iterator[Callable[[_CoordinatorServer], httpx.Client]]:
    """Provide a factory opening raw HTTP clients of a coordinator, all closed at teardown."""
    opened: list[httpx.Client] = []

    def connect(server: _CoordinatorServer) -> httpx.Client:
        client = httpx.Client(base_url=server.base_url)
        opened.append(client)
        return client

    yield connect
    for client in opened:
        client.close()


@pytest.fixture
def lagging_follower(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
    connect_http: Callable[[_CoordinatorServer], httpx.Client],
) -> dc.Store:
    """Open a follower whose catch-up loses its first frame, two commits behind the coordinator."""
    lossy = FrameDroppingClient(connect_http(coordinator))
    follower = dc.open_follower(coordinator.base_url, client=lossy, path=None)
    contributor = connect_follower(coordinator)
    contributor.upsert(Mineral(qid="QG1", name="Gapstone1"))
    contributor.commit()
    contributor.upsert(Mineral(qid="QG2", name="Gapstone2"))
    contributor.commit()
    lossy.drop_first_frame = True
    return follower


# --- §3.a bootstrap-read -----------------------------------------------------


def test_bootstrap_reads_seeded_entities_by_key_locally(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given a coordinator seeded with Quartz and the locality Zomba
    When a follower bootstraps by replaying from 0
    Then the follower reads Quartz by key with its name and hardness
    And the follower reads Zomba by key with its name and country
    """
    follower = connect_follower(coordinator)

    quartz = follower.get(Mineral, qid="Q1")
    zomba = follower.get(Locality, qid="LZ")

    assert quartz is not None
    assert quartz.name == "Quartz"
    assert quartz.mohs == 7.0
    assert zomba is not None
    assert zomba.name == "Zomba"
    assert zomba.country == "MW"


def test_bootstrap_queries_every_seeded_entity_locally(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given a coordinator seeded with two minerals and two localities
    When a follower bootstraps by replaying from 0
    Then a local query finds both minerals
    And a local query finds both localities
    """
    follower = connect_follower(coordinator)

    assert {m.qid for m in follower.query(Mineral)} == {"Q1", "Q2"}
    assert {loc.qid for loc in follower.query(Locality)} == {"LZ", "LT"}


def test_bootstrap_lands_the_follower_on_the_coordinator_head(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
    connect_http: Callable[[_CoordinatorServer], httpx.Client],
) -> None:
    """Given a coordinator seeded by one commit
    When a follower bootstraps by replaying from 0
    Then the follower's watermark equals the coordinator's head
    """
    follower = connect_follower(coordinator)
    http = connect_http(coordinator)

    head = int(http.get("/v1/head").json()["tid"])

    assert follower.last_tid == head


# --- §3.b contribute-new -----------------------------------------------------


def test_follower_contribution_advances_the_coordinator_head(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
    connect_http: Callable[[_CoordinatorServer], httpx.Client],
) -> None:
    """Given a follower and the coordinator's head before any contribution
    When the follower upserts a new topaz and commits
    Then the commit returns the coordinator's new head
    And the head is later than before
    """
    follower = connect_follower(coordinator)
    http = connect_http(coordinator)
    before = int(http.get("/v1/head").json()["tid"])

    follower.upsert(Mineral(qid="Q3", name="Topaz", crystal_system="orthorhombic", mohs=8.0))
    applied = follower.commit()

    after = int(http.get("/v1/head").json()["tid"])
    assert applied == after
    assert after > before


def test_follower_reads_its_own_contribution_back(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given a follower that contributed a new topaz
    When the follower reads the topaz by key
    Then it finds the topaz with its name and hardness
    """
    follower = connect_follower(coordinator)
    follower.upsert(Mineral(qid="Q3", name="Topaz", crystal_system="orthorhombic", mohs=8.0))
    follower.commit()

    topaz = follower.get(Mineral, qid="Q3")

    assert topaz is not None
    assert topaz.name == "Topaz"
    assert topaz.mohs == 8.0


# --- §3.c round-trip-to-2nd-follower -----------------------------------------


def test_second_follower_does_not_see_a_contribution_before_it_syncs(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given two followers opened before the first contributes a beryl
    When the first follower commits the beryl
    Then the second follower does not hold the beryl yet
    """
    f1 = connect_follower(coordinator)
    f2 = connect_follower(coordinator)

    f1.upsert(Mineral(qid="Q4", name="Beryl", crystal_system="hexagonal", mohs=7.5))
    f1.commit()

    beryl_before_sync = f2.get(Mineral, qid="Q4")
    assert beryl_before_sync is None


def test_second_follower_sees_a_contribution_after_sync(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given two followers opened before the first contributes a beryl
    When the second follower syncs
    Then its watermark advances
    And it holds the beryl with its name and hardness
    """
    f1 = connect_follower(coordinator)
    f2 = connect_follower(coordinator)
    f1.upsert(Mineral(qid="Q4", name="Beryl", crystal_system="hexagonal", mohs=7.5))
    f1.commit()
    wm_before = f2.last_tid

    wm_after = f2.sync()

    beryl = f2.get(Mineral, qid="Q4")
    assert wm_after > wm_before
    assert beryl is not None
    assert beryl.name == "Beryl"
    assert beryl.mohs == 7.5


# --- §3.d CROSS-ENTITY-REF (highest value) -----------------------------------


def test_cross_entity_reference_resolves_to_the_right_locality(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given a mineral contributed with a reference to the coordinator's committed Tsumeb
    When a second follower syncs and follows the mineral's type locality
    Then it resolves Tsumeb by qid and name, not the other seeded locality
    """
    f1 = connect_follower(coordinator)
    f2 = connect_follower(coordinator)
    # Tsumeb's OID is the coordinator's own (one in-process registry): the ref is valid on the wire
    tsumeb = f1.get(Locality, qid="LT")
    assert tsumeb is not None
    f1.upsert(Mineral(qid="Q5", name="Smithsonite", mohs=4.5, type_locality=dc.Lazy.of(tsumeb)))
    f1.commit()

    f2.sync()

    smithsonite = f2.get(Mineral, qid="Q5")
    assert smithsonite is not None
    assert smithsonite.name == "Smithsonite"
    handle = smithsonite.type_locality
    assert handle is not None
    resolved = handle.get()
    assert resolved is not None
    assert resolved.qid == "LT"
    assert resolved.name == "Tsumeb"


def test_reference_to_an_uncommitted_locality_is_refused_and_never_lands(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
    connect_http: Callable[[_CoordinatorServer], httpx.Client],
) -> None:
    """Given a batch holding a new locality and a new mineral referencing it
    When the follower commits the batch
    Then the commit raises NotImplementedError
    And the coordinator's head is unchanged
    And a fresh follower finds neither the mineral nor the locality
    """
    f1 = connect_follower(coordinator)
    http = connect_http(coordinator)
    head_before = int(http.get("/v1/head").json()["tid"])
    new_loc = Locality(qid="LX", name="Phantom", country="ZZ")
    f1.store(new_loc)
    f1.store(Mineral(qid="Q6", name="Ghostite", type_locality=dc.Lazy.of(new_loc)))

    with pytest.raises(NotImplementedError):
        f1.commit()

    head_after = int(http.get("/v1/head").json()["tid"])
    fresh = connect_follower(coordinator)
    assert head_after == head_before
    assert fresh.get(Mineral, qid="Q6") is None
    assert fresh.get(Locality, qid="LX") is None


# --- §3.e OCC conflict -------------------------------------------------------


def test_occ_stale_base_commit_raises_conflict_and_the_winner_stays(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given f1 holds Quartz at hardness 7.0 and f2 has since committed 7.2
    When f1 commits its stale edit to 9.9
    Then the commit raises ConflictError
    And f2 still reads 7.2
    And a fresh follower reads 7.2
    """
    f1 = connect_follower(coordinator)
    f2 = connect_follower(coordinator)
    m1 = f1.get(Mineral, qid="Q1")
    assert m1 is not None
    assert m1.mohs == 7.0
    m2 = f2.get(Mineral, qid="Q1")
    assert m2 is not None
    m2.mohs = 7.2
    f2.commit()
    m1.mohs = 9.9

    with pytest.raises(ConflictError):
        f1.commit()

    winner = f2.get(Mineral, qid="Q1")
    authoritative = connect_follower(coordinator).get(Mineral, qid="Q1")
    assert winner is not None
    assert winner.mohs == 7.2
    assert authoritative is not None
    assert authoritative.mohs == 7.2


def test_discard_then_sync_after_a_conflict_pulls_the_winning_value(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given f1's stale edit of Calcite was rejected after f2 committed 3.5
    When f1 discards its buffer and syncs
    Then f1 re-reads Calcite at 3.5
    """
    f1 = connect_follower(coordinator)
    f2 = connect_follower(coordinator)
    m1 = f1.get(Mineral, qid="Q2")
    assert m1 is not None
    assert m1.mohs == 3.0
    m2 = f2.get(Mineral, qid="Q2")
    assert m2 is not None
    m2.mohs = 3.5
    f2.commit()
    m1.mohs = 9.9
    with pytest.raises(ConflictError):
        f1.commit()

    f1.discard()
    f1.sync()

    reread = f1.get(Mineral, qid="Q2")
    assert reread is not None
    assert reread.mohs == 3.5


def test_recommit_after_conflict_recovery_converges_on_the_new_value(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given f1 discarded and synced after a rejected stale edit of Calcite
    When f1 re-applies hardness 4.0 and commits
    Then f2 reads 4.0 after a sync
    And a fresh follower reads 4.0, never the stale 9.9
    """
    f1 = connect_follower(coordinator)
    f2 = connect_follower(coordinator)
    m1 = f1.get(Mineral, qid="Q2")
    assert m1 is not None
    m2 = f2.get(Mineral, qid="Q2")
    assert m2 is not None
    m2.mohs = 3.5
    f2.commit()
    m1.mohs = 9.9
    with pytest.raises(ConflictError):
        f1.commit()
    f1.discard()
    f1.sync()
    reread = f1.get(Mineral, qid="Q2")
    assert reread is not None
    reread.mohs = 4.0

    f1.commit()

    f2.sync()
    winner = f2.get(Mineral, qid="Q2")
    authoritative = connect_follower(coordinator).get(Mineral, qid="Q2")
    assert winner is not None
    assert winner.mohs == 4.0
    assert authoritative is not None
    assert authoritative.mohs == 4.0


def test_committing_recovers_a_conflict_without_a_manual_loop(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given f2 moved Calcite from 3.0 to 5.0 so f1's replica is stale
    When f1 increments Calcite by 1.0 through two committing() attempts
    Then Calcite converges on 6.0, the winning 5.0 plus 1.0
    """
    f1 = connect_follower(coordinator)
    f2 = connect_follower(coordinator)
    m2 = f2.get(Mineral, qid="Q2")
    assert m2 is not None
    m2.mohs = 5.0
    f2.commit()

    attempts = f1.committing(retries=5)
    first_attempt = next(attempts)
    with first_attempt:  # conflicts on the stale base, discards and syncs
        m1 = f1.get(Mineral, qid="Q2")
        assert m1 is not None
        m1.mohs = (m1.mohs or 0) + 1.0
    second_attempt = next(attempts)
    with second_attempt:  # re-reads f2's 5.0 and applies +1.0 again
        m1 = f1.get(Mineral, qid="Q2")
        assert m1 is not None
        m1.mohs = (m1.mohs or 0) + 1.0

    f2.sync()
    winner = f2.get(Mineral, qid="Q2")
    assert winner is not None
    assert winner.mohs == 6.0


def test_committing_with_no_retries_raises_the_unresolved_conflict(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given f2 moved Calcite to 5.0 so f1's next commit is stale
    When f1 makes its single committing(retries=0) attempt
    Then the next step raises ConflictError
    And a fresh follower still reads 5.0, never f1's 9.9
    """
    f1 = connect_follower(coordinator)
    f2 = connect_follower(coordinator)
    m2 = f2.get(Mineral, qid="Q2")
    assert m2 is not None
    m2.mohs = 5.0
    f2.commit()
    attempts = f1.committing(retries=0)
    only_attempt = next(attempts)
    with only_attempt:
        m1 = f1.get(Mineral, qid="Q2")
        assert m1 is not None
        m1.mohs = 9.9

    with pytest.raises(ConflictError):
        next(attempts)

    authoritative = connect_follower(coordinator).get(Mineral, qid="Q2")
    assert authoritative is not None
    assert authoritative.mohs == 5.0


def test_committing_raises_the_conflict_once_its_retries_are_exhausted(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
) -> None:
    """Given f2 moves Calcite again during each of f1's attempts
    When f1 makes both attempts of committing(retries=1)
    Then the step after the second attempt raises ConflictError
    And a fresh follower reads f2's last 6.0, never f1's 9.9
    """
    f1 = connect_follower(coordinator)
    f2 = connect_follower(coordinator)
    attempts = f1.committing(retries=1)
    first_attempt = next(attempts)
    with first_attempt:
        m1 = f1.get(Mineral, qid="Q2")
        assert m1 is not None
        m1.mohs = 9.9
        m2 = f2.get(Mineral, qid="Q2")
        assert m2 is not None
        m2.mohs = 5.0
        f2.commit()
    second_attempt = next(attempts)
    with second_attempt:
        m1 = f1.get(Mineral, qid="Q2")
        assert m1 is not None
        m1.mohs = 9.9
        m2 = f2.get(Mineral, qid="Q2")
        assert m2 is not None
        m2.mohs = 6.0
        f2.commit()

    with pytest.raises(ConflictError):
        next(attempts)

    authoritative = connect_follower(coordinator).get(Mineral, qid="Q2")
    assert authoritative is not None
    assert authoritative.mohs == 6.0


def test_lost_ack_reinsert_with_base_none_raises_conflict(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
    connect_http: Callable[[_CoordinatorServer], httpx.Client],
) -> None:
    """Given an untracked Quartz with base None, the wire shape of a lost-ack re-insert
    When it is POSTed to /v1/submit although Q1 is already present
    Then the response is a 409 conflict
    And the coordinator's head is unchanged
    And exactly one Quartz remains, with the original hardness
    """
    http = connect_http(coordinator)
    follower = connect_follower(coordinator)
    head_before = int(http.get("/v1/head").json()["tid"])
    fields = {"qid": "Q1", "name": "Quartz", "mohs": 7.0}

    resp = http.post(
        "/v1/submit",
        json={"ops": [{"type": _MINERAL, "key": "qid", "fields": fields, "base": None}]},
    )

    head_after = int(http.get("/v1/head").json()["tid"])
    follower.sync()
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "conflict"
    assert head_after == head_before
    assert [m.mohs for m in follower.query(Mineral) if m.qid == "Q1"] == [7.0]


# --- §3.f schema-skew --------------------------------------------------------


def test_raw_submit_with_unknown_field_is_refused_as_schema_skew(
    coordinator: _CoordinatorServer,
    connect_http: Callable[[_CoordinatorServer], httpx.Client],
) -> None:
    """Given a raw submit carrying a field the coordinator's Mineral lacks
    When it is POSTed to /v1/submit
    Then the response is 409
    And the error detail is schema-skew
    """
    http = connect_http(coordinator)
    fields = {"qid": "QSK", "name": "Skewstone", "bogus_field": 1}

    resp = http.post(
        "/v1/submit",
        json={"ops": [{"type": _MINERAL, "key": "qid", "fields": fields, "base": None}]},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "schema-skew"


def test_schema_skew_submit_writes_nothing(
    coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
    connect_http: Callable[[_CoordinatorServer], httpx.Client],
) -> None:
    """Given a raw submit carrying a field the coordinator's Mineral lacks
    When it is POSTed to /v1/submit and refused
    Then the coordinator's head is unchanged
    And a synced follower does not hold the skewed mineral
    """
    follower = connect_follower(coordinator)
    http = connect_http(coordinator)
    head_before = int(http.get("/v1/head").json()["tid"])
    fields = {"qid": "QSK", "name": "Skewstone", "bogus_field": 1}

    http.post(
        "/v1/submit",
        json={"ops": [{"type": _MINERAL, "key": "qid", "fields": fields, "base": None}]},
    )

    head_after = int(http.get("/v1/head").json()["tid"])
    follower.sync()
    assert head_after == head_before
    assert follower.get(Mineral, qid="QSK") is None


# --- §3.g delta-gap on catch-up (over the wire) ------------------------------


def test_delta_gap_on_catchup_refuses_no_partial_apply(lagging_follower: dc.Store) -> None:
    """Given a follower two commits behind whose catch-up frames lose the first one
    When the follower syncs
    Then the sync raises DeltaGapError
    And the follower's watermark is unchanged
    And the follower still holds only the two seeded minerals
    And the second gapstone did not land
    """
    wm_before = lagging_follower.last_tid

    with pytest.raises(DeltaGapError):
        lagging_follower.sync()

    assert lagging_follower.last_tid == wm_before
    assert {m.qid for m in lagging_follower.query(Mineral)} == {"Q1", "Q2"}
    assert lagging_follower.get(Mineral, qid="QG2") is None


# --- §3.h ride check_delta_consumer against the served DeltaLog --------------


def test_served_deltalog_is_a_gapless_change_feed(coordinator: _CoordinatorServer) -> None:
    """Given a coordinator whose seeding was one commit
    When its served DeltaLog is replayed from genesis
    Then the replayed transaction ids are exactly 1
    """
    log = coordinator.log
    assert log is not None

    served = list(log.replay(after_tid=0))

    assert [delta["tid"] for delta in served] == [1]


def test_counting_consumer_certifies_against_the_conformance_kit(
    coordinator: _CoordinatorServer,
) -> None:
    """Given a fresh CountingConsumer factory, the canonical fully-obligated consumer
    When the public COMMIT-DELTA consumer conformance kit drives it
    Then the kit ran the prior un-index scenario
    And the kit ran the delete totality scenario
    """
    ran = check_delta_consumer(CountingConsumer, content=CountingConsumer.content)

    assert "§3 prior un-index" in ran
    assert "§3.1 delete totality" in ran


def test_served_feed_replays_cleanly_through_a_conforming_consumer(
    coordinator: _CoordinatorServer,
) -> None:
    """Given the coordinator's served change-feed
    When a CountingConsumer applies every served delta
    Then its watermark is the last served transaction id
    And it counted at least the two seeded minerals
    """
    log = coordinator.log
    assert log is not None
    served = list(log.replay(after_tid=0))
    consumer = CountingConsumer()

    _apply_all(consumer, served)

    assert consumer.watermark == 1
    assert consumer.content().get(_MINERAL, 0) >= 2


# --- W1 (epic #168): contributions stamp anonymous, never the operator ---------


def test_operators_own_commit_keeps_its_actor_stamp(
    operator_coordinator: _CoordinatorServer,
    connect_http: Callable[[_CoordinatorServer], httpx.Client],
) -> None:
    """Given a coordinator whose ambient principal has uid 42
    When the served deltas are fetched from 0
    Then the operator's own seeding commit is stamped actor 42
    """
    http = connect_http(operator_coordinator)

    blob = http.get("/v1/deltas", params={"after": 0}).content

    deltas = decode_frames(blob)
    assert deltas[0]["actor"] == 42


def test_contributions_stamp_anonymous_not_the_operator(
    operator_coordinator: _CoordinatorServer,
    connect_follower: Callable[[_CoordinatorServer], dc.Store],
    connect_http: Callable[[_CoordinatorServer], httpx.Client],
) -> None:
    """Given a coordinator whose ambient principal has uid 42
    When a follower contributes a fluorite through POST /v1/submit
    Then the latest served delta carries the fluorite payload
    And that delta is stamped actor 0, anonymous, not the operator
    """
    follower = connect_follower(operator_coordinator)
    http = connect_http(operator_coordinator)
    follower.store(Mineral(qid="Q9", name="Fluorite", crystal_system="cubic"))
    follower.commit()

    blob = http.get("/v1/deltas", params={"after": 0}).content

    contribution = decode_frames(blob)[-1]
    assert any(
        op["payload"] is not None and b"Fluorite" in op["payload"] for op in contribution["ops"]
    )
    assert contribution["actor"] == 0
