"""Proving ground #7 — the web tier (FastAPI + Strawberry) over a real graph.

Runs the *real* ``datacrystal[web]`` stack — a FastAPI REST boundary and a
Strawberry GraphQL boundary, both reflected off the same ``@entity`` surface —
over the **Gene Ontology** (~38k terms, ~58k ``is_a`` edges, a deep
polyhierarchy). The deep poly-hierarchy is the point: a nested GraphQL query
that walks ``term → parents → parents → …`` is *exactly* the shape that triggers
the N+1 read amplification a naive resolver would pay, so it is the honest
real-shape proof of the per-request DataLoader (#100) and the #101 op-count gate.

It reports honest absolutes and asserts two correctness oracles:

* **REST** — a list endpoint's p50/p99 latency + throughput (the Pydantic/REST
  tax end-to-end, #97/#98);
* **GraphQL (nested)** — a ``term → parents…`` query's p50/p99 latency, and:
  * the **N+1 ORACLE** — the store-load COUNT per GraphQL request, asserted
    **O(depth), not O(nodes)**: a depth-``D`` walk fanning out across the term's
    ancestors issues exactly ``D`` ``load_many`` batches (one per relation
    *level*), proving the per-request DataLoader actually batches on *real*
    shape, not just in the micro-bench (the property pinned by
    ``tests/fitness/test_graphql_no_n_plus_1.py``, here on the real graph);
  * the **zero-Pydantic ORACLE** — the GraphQL path resolves off frozen
    :class:`~datacrystal._snapshot.EntityView`\\s via ``getattr`` and constructs
    **zero** Pydantic models (only REST pays the DTO tax).

On-demand eval, NOT a unit test (it downloads + ingests tens of MB). It needs
the ``web`` extra (``fastapi`` / ``strawberry`` / ``pydantic``) and ``httpx``
(the ``TestClient`` transport). Run it during an evaluation phase:

    curl -sL --create-dirs -o evals/data/go-basic.obo \
      https://current.geneontology.org/ontology/go-basic.obo
    uv run --extra web python evals/proving_grounds/web_api.py

A FAST self-check (no download) verifies the harness — the app boots, REST + the
nested-GraphQL endpoint respond, and both oracles fire — over a tiny synthetic
GO-shaped graph (deep ``is_a`` chains). It runs automatically when the OBO file
is absent, and on demand via ``WEB_SMOKE=1``:

    uv run --extra web python evals/proving_grounds/web_api.py            # smoke if no OBO
    WEB_SMOKE=1 uv run --extra web python evals/proving_grounds/web_api.py  # force smoke

(see evals/README.md §#7 for all proving grounds + fetch commands)

Gene Ontology is CC BY 4.0 (http://geneontology.org).
"""

# The magic-query ``Term.go_id == go_id`` returns an untypeable Condition (pyright
# reads it as ``bool``), and the dynamically built Strawberry/Query types have
# fields pyright cannot see — the same file-scoped pragmas the web e2e tests carry
# (tests/web/test_app_wiring.py, tests/web/test_rest_e2e.py).
# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import asyncio
import gc
import os
import shutil
import statistics
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import field
from pathlib import Path
from typing import Annotated, Any

import datacrystal as dc

# The framework deps live ONLY in the web extra + this proving ground — never in
# core. A clear message beats an ImportError traceback when the extra is absent.
try:
    import pydantic
    import strawberry
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from strawberry.fastapi import GraphQLRouter
    from strawberry.tools import create_type
    from strawberry.types import Info
except ImportError as exc:  # pragma: no cover - on-demand harness guard
    sys.exit(
        f"missing a web dependency ({exc.name}) — run with the web extra + httpx:\n"
        "  uv run --extra web python evals/proving_grounds/web_api.py"
    )

from datacrystal._snapshot import Ref, Snapshot  # noqa: E402  (after the extra guard)
from datacrystal._storage.memory import MemoryBackend  # noqa: E402
from datacrystal._storage.protocol import (  # noqa: E402
    BootInfo,
    CommitBatch,
    StoredBlob,
    StoredRecord,
)
from datacrystal.web import (  # noqa: E402
    LOADER_CONTEXT_KEY,
    SNAPSHOT_CONTEXT_KEY,
    SnapshotLoader,
    graphql_context_getter,
    read_snapshot,
    snapshot_context,
)

DATA = Path(__file__).resolve().parent.parent / "data"
OBO = DATA / "go-basic.obo"
STORE = DATA / "go-web.store"
BIOLOGICAL_PROCESS = "GO:0008150"  # the root with the most descendants


# The one @entity reflected into BOTH boundaries — same shape as the #1 Gene
# Ontology ground so the reflection is exercised on a real navigational graph.
@dc.entity
class Term:
    go_id: Annotated[str, dc.Unique]
    name: str
    namespace: Annotated[str | None, dc.Index] = None
    parents: list[dc.Lazy["Term"]] = field(default_factory=list)  # is_a — lazy adjacency (#30)


# --- the load-counting storage harness (the N+1 oracle) -----------------------
#
# The KICKOFF 'counting storage wrapper' pattern (tests/fitness/test_scale_shape.py,
# reused by tests/fitness/test_graphql_no_n_plus_1.py): count the ``load_many``
# round-trips the snapshot issues — the exact seam the GraphQL DataLoader reads
# through (``SnapshotLoader.load`` → ``Snapshot.get_many`` → ``read_view.load_many``).
# ``load_calls`` is the number of storage round-trips one GraphQL request paid;
# the oracle asserts it scales with relation LEVELS (depth), never node count.


class _CountingReadView:
    """A backend read view that counts the ``load_many`` round-trips the snapshot
    issues. Index builds go through ``scan_type`` and are deliberately NOT counted
    (the documented one-time cost), exactly like the fitness-gate wrapper."""

    def __init__(self, inner: Any, counter: "CountingBackend") -> None:
        self._inner = inner
        self._counter = counter

    def boot(self) -> BootInfo:
        return self._inner.boot()

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        out = self._inner.load_many(oids)
        self._counter.load_calls += 1
        self._counter.records_loaded += len(out)
        return out

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        return self._inner.scan_type(cid)

    def load_blob(self, oid: int) -> StoredBlob | None:
        return self._inner.load_blob(oid)

    def open_blob_stream(
        self, oid: int, on_close: Callable[[], None] | None = None
    ) -> Any:
        return self._inner.open_blob_stream(oid, on_close)

    def close(self) -> None:
        self._inner.close()


class CountingBackend:
    """A storage wrapper counting batch reads on its read views (KICKOFF pattern),
    pointed at the snapshot read path the GraphQL DataLoader drives. ``load_calls``
    is the number of ``load_many`` round-trips since the last :meth:`reset`."""

    def __init__(self, inner: MemoryBackend) -> None:
        self._inner = inner
        self.load_calls = 0
        self.records_loaded = 0

    def reset(self) -> None:
        self.load_calls = 0
        self.records_loaded = 0

    def boot(self) -> BootInfo:
        return self._inner.boot()

    def load_many(self, oids: list[int]) -> dict[int, StoredRecord]:
        return self._inner.load_many(oids)

    def scan_type(self, cid: int) -> Iterator[StoredRecord]:
        return self._inner.scan_type(cid)

    def load_blob(self, oid: int) -> StoredBlob | None:
        return self._inner.load_blob(oid)

    def apply(self, batch: CommitBatch) -> None:
        return self._inner.apply(batch)

    def read_view(self) -> _CountingReadView:
        return _CountingReadView(self._inner.read_view(), self)

    def close(self) -> None:
        self._inner.close()


# --- the zero-Pydantic oracle -------------------------------------------------
#
# The GraphQL path must construct ZERO Pydantic models — it resolves scalars off
# the frozen EntityView via getattr (no DTO), and refs through the DataLoader.
# We arm a counter over the two ways a DTO is built — ``BaseModel(**data)`` (which
# funnels through ``__init__``) and ``BaseModel.model_validate`` (the FastAPI
# response path, which builds via the core validator, NOT ``__init__``) — and
# assert the GraphQL request fires neither. REST validates a DTO per row through
# ``model_validate``, so the counter is non-zero there: the tax is REST-only.


class _PydanticConstructionCounter:
    """Counts ``pydantic.BaseModel`` constructions while armed.

    Wraps BOTH construction entries: ``BaseModel.__init__`` (the ``Model(**data)``
    path) and ``BaseModel.model_validate`` (the ``model_validate`` path FastAPI's
    ``response_model`` and our REST handler use — it builds through the core
    validator and never touches ``__init__``). Catching only one would miss the
    other, so the oracle would falsely read zero on whichever path was used."""

    __slots__ = ("count", "_orig_init", "_orig_validate")

    def __init__(self) -> None:
        self.count = 0
        self._orig_init: Any = None
        self._orig_validate: Any = None

    def __enter__(self) -> "_PydanticConstructionCounter":
        counter = self
        self._orig_init = pydantic.BaseModel.__init__
        self._orig_validate = pydantic.BaseModel.model_validate.__func__  # the classmethod

        def counting_init(model_self: Any, /, **data: Any) -> None:
            counter.count += 1
            counter._orig_init(model_self, **data)

        def counting_validate(cls: Any, *args: Any, **kwargs: Any) -> Any:
            counter.count += 1
            return counter._orig_validate(cls, *args, **kwargs)

        pydantic.BaseModel.__init__ = counting_init  # type: ignore[method-assign]
        pydantic.BaseModel.model_validate = classmethod(counting_validate)  # type: ignore[assignment]
        self.count = 0
        return self

    def __exit__(self, *exc: object) -> None:
        pydantic.BaseModel.__init__ = self._orig_init  # type: ignore[method-assign]
        pydantic.BaseModel.model_validate = classmethod(self._orig_validate)  # type: ignore[assignment]


# --- the FastAPI + Strawberry app over the Term graph -------------------------


class TermPublic(pydantic.BaseModel):
    """The REST list endpoint's boundary DTO — the scalar projection of a ``Term``.

    A purpose-built scalar DTO (not ``entity_model(Term)``) because the list-of-
    ``Lazy`` adjacency (``parents``) is a *graph* shape the GraphQL boundary owns;
    REST serves the flat row. ``model_config = from_attributes`` lets
    ``model_validate`` read the fields straight off a frozen
    :class:`~datacrystal._snapshot.EntityView` — the same projection the shipped
    ``to_pydantic(face="public")`` does for a scalar entity (#97/#98), so the
    per-row ``model_validate`` here IS the honest REST/Pydantic tax."""

    model_config = pydantic.ConfigDict(from_attributes=True)
    oid: int
    go_id: str
    name: str
    namespace: str | None = None


def build_app(store: dc.Store) -> tuple[FastAPI, dict[str, Any]]:
    """Build the REST + GraphQL app over an ALREADY-OPEN ``store``.

    Returns the app and a small handle dict (the built GraphQL ``schema``). The
    store is injected (not opened by a lifespan) so the proving ground owns the
    one process store and can swap in the counting backend for the oracle;
    ``read_snapshot`` / ``graphql_context_getter`` reach it through ``app.state``
    exactly as the lifespan would have pinned it.
    """
    from datacrystal.web._app import STORE_STATE_KEY

    app = FastAPI()
    setattr(app.state, STORE_STATE_KEY, store)  # the lifespan's pin, done by hand

    # --- REST: a list endpoint (the Pydantic/REST tax end to end) -------------
    @app.get("/terms", response_model=list[TermPublic])
    def list_terms(  # noqa: ANN202
        namespace: str | None = None,
        limit: int = 50,
        snap: Snapshot = Depends(read_snapshot),  # noqa: B008 - FastAPI dep marker
    ):
        target = Term.namespace == namespace if namespace else Term
        views = snap.query(target, limit=limit)
        # model_validate validates one DTO per row off the EntityView — the REST
        # tax FastAPI then serializes through response_model=list[TermPublic].
        return [TermPublic.model_validate(v) for v in views]

    # --- GraphQL: a nested term → parents → … query --------------------------
    schema = build_graphql_schema()
    app.include_router(
        GraphQLRouter(schema, context_getter=graphql_context_getter), prefix="/graphql"
    )

    return app, {"schema": schema}


def _loader_from(info: Info[Any, Any]) -> SnapshotLoader:
    """Pull the per-request :class:`SnapshotLoader` off the GraphQL context — the
    public N+1-killer the request wiring (:func:`graphql_context_getter` / #100)
    put there under :data:`LOADER_CONTEXT_KEY`."""
    loader = info.context[LOADER_CONTEXT_KEY]
    assert isinstance(loader, SnapshotLoader)
    return loader


@strawberry.type
class TermGQL:
    """The GraphQL ``Term`` — scalar fields resolve off the frozen EntityView via
    getattr (NO Pydantic), and the ``is_a`` adjacency (``parents``) resolves as a
    batched list edge through the per-request DataLoader.

    A hand-written self-referential Strawberry type (the shipped
    :class:`~datacrystal.web.StrawberryReflector` reflects *single*-ref edges,
    #99/#100; a ``list[Lazy]`` adjacency is the graph shape this proving ground
    drives the loader over). The ``parents`` resolver pulls the public
    :class:`~datacrystal.web.SnapshotLoader` off the context and ``.load()``\\s
    every parent OID — every sibling ``.load()`` in one resolver tick coalesces
    into ONE :meth:`~datacrystal._snapshot.Snapshot.get_many` (the N+1 killer),
    so a depth-``D`` walk costs ``D`` batches, not one read per ancestor node."""

    go_id: str
    name: str
    namespace: str | None

    @strawberry.field
    async def parents(self, info: Info[Any, Any]) -> list["TermGQL"]:
        # ``self`` is the frozen EntityView (the default getattr resolver handed it
        # straight through); its ``parents`` field is a tuple of Ref tokens.
        refs = getattr(self, "parents", None) or ()
        loader = _loader_from(info)
        # One .load() per parent ref; the tick coalesces them into a single
        # get_many — O(siblings)→1 batch per level (the per-request DataLoader).
        views = await asyncio.gather(
            *(loader.load(r.oid if isinstance(r, Ref) else r) for r in refs)
        )
        return [v for v in views if v is not None]  # type: ignore[misc]


def build_graphql_schema() -> "strawberry.Schema":
    """A ``{ term(goId) { … parents { … } } }`` schema over the snapshot context.

    The ``term`` root reads the per-request snapshot off the context and returns
    the matching frozen EntityView; ``TermGQL.parents`` then drives the
    per-request DataLoader down the ``is_a`` polyhierarchy."""

    @strawberry.type
    class Query:
        @strawberry.field
        def term(self, go_id: str, info: Info[Any, Any]) -> TermGQL | None:
            snap: Snapshot = info.context[SNAPSHOT_CONTEXT_KEY]
            matches = snap.query(Term.go_id == go_id)
            return matches[0] if matches else None  # type: ignore[return-value]

    return strawberry.Schema(query=Query)


def nested_parents_query(go_id: str, depth: int) -> str:
    """A GraphQL selection walking ``term → parents → parents → …`` ``depth`` deep."""
    inner = "goId name"
    for _ in range(depth):
        inner = f"goId name parents {{ {inner} }}"
    return f'{{ term(goId: "{go_id}") {{ {inner} }} }}'


# --- timing helper ------------------------------------------------------------


def percentiles(samples_ms: list[float]) -> tuple[float, float, float]:
    """Return ``(p50, p99, throughput-per-s)`` for a list of per-call ms."""
    p50 = statistics.median(samples_ms)
    ordered = sorted(samples_ms)
    p99 = ordered[min(len(ordered) - 1, int(round(0.99 * (len(ordered) - 1))))]
    total_s = sum(samples_ms) / 1000.0
    throughput = len(samples_ms) / total_s if total_s > 0 else float("inf")
    return p50, p99, throughput


# --- OBO parsing (mirrors proving ground #1) ----------------------------------


def parse_obo(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None

    def finalize(c: dict[str, Any] | None) -> None:
        if c and c.get("id") and not c["obsolete"]:
            out[c["id"]] = c

    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line == "[Term]":
                finalize(cur)
                cur = {"id": None, "name": "", "namespace": None, "is_a": [], "obsolete": False}
            elif line.startswith("["):
                finalize(cur)
                cur = None
            elif cur is not None and line:
                if line.startswith("id: "):
                    cur["id"] = line[4:].strip()
                elif line.startswith("name: "):
                    cur["name"] = line[6:].strip()
                elif line.startswith("namespace: "):
                    cur["namespace"] = line[11:].strip()
                elif line.startswith("is_a: "):
                    cur["is_a"].append(line[6:].split("!")[0].strip())
                elif line == "is_obsolete: true":
                    cur["obsolete"] = True
        finalize(cur)
    return out


def deepest_term(parsed: dict[str, dict[str, Any]]) -> tuple[str, int]:
    """The term with the longest ``is_a`` chain to a root — the deep nesting that
    makes the N+1 oracle bite. Returns ``(go_id, depth)``."""
    memo: dict[str, int] = {}

    def depth(gid: str) -> int:
        if gid in memo:
            return memo[gid]
        memo[gid] = 0  # cycle guard (GO is a DAG, but be safe)
        parents = [p for p in parsed[gid]["is_a"] if p in parsed]
        memo[gid] = 1 + max((depth(p) for p in parents), default=0)
        return memo[gid]

    best = max(parsed, key=depth)
    return best, memo[best]


def ingest(store: dc.Store, parsed: dict[str, dict[str, Any]]) -> None:
    """Store every term free-floating (not root-pinned, so reopen memory is
    bounded) and wire the lazy ``is_a`` parent edges, dropping orphan refs."""
    terms = {gid: Term(go_id=gid, name=d["name"], namespace=d["namespace"])
             for gid, d in parsed.items()}
    for gid, d in parsed.items():
        for pid in d["is_a"]:
            if pid in terms:
                terms[gid].parents.append(dc.Lazy.of(terms[pid]))
    for t in terms.values():
        store.store(t)
    store.root = {"source": "Gene Ontology go-basic", "terms": len(terms)}
    store.commit()


# --- the synthetic GO-shaped graph (the no-download smoke) --------------------


def synthetic_go(depth: int = 6, fanout: int = 4) -> dict[str, dict[str, Any]]:
    """A tiny GO-shaped polyhierarchy: a balanced ``is_a`` tree ``depth`` levels
    deep fanning out ``fanout`` ways, every node also linking to its prior
    sibling so a level's ancestor refs fan out — the same shape the N+1 oracle
    bites, without a multi-MB download. ``GO:0008150`` is the synthetic root so
    the REST query has a known anchor."""
    parsed: dict[str, dict[str, Any]] = {}
    parsed[BIOLOGICAL_PROCESS] = {
        "id": BIOLOGICAL_PROCESS, "name": "biological_process",
        "namespace": "biological_process", "is_a": [], "obsolete": False,
    }
    frontier = [BIOLOGICAL_PROCESS]
    counter = 1
    for _level in range(depth):
        nxt: list[str] = []
        for parent in frontier:
            prev: str | None = None
            for _f in range(fanout):
                gid = f"GO:{counter:07d}"
                counter += 1
                # Two parents per node (the real is_a parent + the prior sibling)
                # so a level's parents-of fan out > 1 — the batchable shape.
                is_a = [parent] if prev is None else [parent, prev]
                parsed[gid] = {
                    "id": gid, "name": f"term-{gid}",
                    "namespace": "biological_process", "is_a": is_a, "obsolete": False,
                }
                prev = gid
                nxt.append(gid)
        frontier = nxt
    return parsed


# --- the oracles + the run ----------------------------------------------------


def run_rest(client: TestClient, namespace: str | None, n: int) -> None:
    """Hit the REST list endpoint ``n`` times; report p50/p99 + throughput."""
    params: dict[str, Any] = {"limit": 50}
    if namespace:
        params["namespace"] = namespace
    # one warm-up (build the index, JIT the route) outside the measured window
    warm = client.get("/terms", params=params)
    assert warm.status_code == 200
    rows = len(warm.json())
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        resp = client.get("/terms", params=params)
        samples.append((time.perf_counter() - t0) * 1000)
        assert resp.status_code == 200
    p50, p99, tput = percentiles(samples)
    print(f"\nREST  GET /terms  (limit 50, {rows} rows, {n} calls):")
    print(f"  p50 {p50:7.2f} ms   p99 {p99:7.2f} ms   {tput:8.0f} req/s")


def run_graphql_nested(
    client: TestClient, go_id: str, depth: int, n: int
) -> None:
    """Hit the nested GraphQL ``term → parents…`` query ``n`` times; report
    p50/p99 + throughput, and assert the result is well-formed."""
    query = nested_parents_query(go_id, depth)
    body = client.post("/graphql", json={"query": query}).json()  # warm-up
    assert body.get("errors") is None, body.get("errors")
    assert body["data"]["term"]["goId"] == go_id
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        resp = client.post("/graphql", json={"query": query})
        samples.append((time.perf_counter() - t0) * 1000)
        assert resp.status_code == 200
    p50, p99, tput = percentiles(samples)
    print(f"\nGraphQL  nested term→parents (depth {depth}, {n} calls):")
    print(f"  p50 {p50:7.2f} ms   p99 {p99:7.2f} ms   {tput:8.0f} req/s")


def assert_n_plus_1_oracle(
    store: dc.Store, go_id: str, depth: int
) -> tuple[int, int]:
    """THE N+1 ORACLE — on real shape.

    Execute the nested ``term → parents…`` GraphQL query against a schema over a
    snapshot whose backend counts ``load_many`` round-trips, and assert the count
    is **O(depth), not O(nodes)**: exactly ``depth`` batches (one
    ``Snapshot.get_many`` per relation LEVEL) regardless of how many ancestor
    terms each level fans out to. Returns ``(load_calls, records_loaded)``.

    The store passed here MUST be backed by a :class:`CountingBackend` (the
    proving ground swaps it in). The level-0 term is materialized and the index
    build is paid BEFORE the reset, so the count isolates exactly the
    reference-following reads — mirroring ``tests/fitness/test_graphql_no_n_plus_1.py``
    (the same gate, here on the real Gene Ontology graph). A list-root over the
    pre-loaded view drives :class:`TermGQL.parents` so only the parent-following
    batches are counted, not the root lookup.
    """
    backend = store._backend  # pyright: ignore[reportPrivateUsage]
    assert isinstance(backend, CountingBackend), (
        "the N+1 oracle needs a CountingBackend-backed store"
    )

    def root() -> object:
        return roots

    with store.snapshot() as snap:
        roots = snap.query(Term.go_id == go_id)  # materialize the level-0 term(s)
        assert roots, f"{go_id} not found"
        backend.reset()  # isolate the reference-following reads

        root_field: Any = strawberry.field(
            resolver=root, graphql_type=list[TermGQL], name="root"
        )
        schema = strawberry.Schema(query=create_type("Query", [root_field]))
        # Walk parents depth levels: inner selection mirrors nested_parents_query.
        inner = "goId name"
        for _ in range(depth):
            inner = f"goId name parents {{ {inner} }}"
        result = asyncio.run(
            schema.execute(f"{{ root {{ {inner} }} }}", context_value=snapshot_context(snap))
        )

    assert result.errors is None, result.errors
    load_calls = backend.load_calls
    records = backend.records_loaded
    # The non-negotiable assertion: one load_many per relation LEVEL, not per node.
    assert load_calls == depth, (
        f"N+1 REGRESSION on real shape: a depth-{depth} term→parents query "
        f"issued {load_calls} get_many batches over {records} records — expected "
        f"exactly {depth} (one per relation LEVEL). The per-request DataLoader is "
        "no longer coalescing sibling ancestor resolves into one Snapshot.get_many; "
        "reads scale with NODE COUNT, not depth."
    )
    assert records > load_calls, (
        f"the oracle is only meaningful when a level fans out: {records} records "
        f"loaded in {load_calls} batches — pick a term whose ancestors branch"
    )
    print(f"\nN+1 ORACLE (real shape, depth {depth}):")
    print(f"  {records} ancestor records resolved in {load_calls} get_many batches "
          f"→ O(depth) ✓  (O(nodes) would be {records} reads)")
    return load_calls, records


def assert_zero_pydantic_oracle(
    client: TestClient, go_id: str, depth: int
) -> None:
    """THE ZERO-PYDANTIC ORACLE.

    The GraphQL path resolves off frozen EntityViews via getattr and must build
    ZERO Pydantic models; REST validates one DTO per row, so the same counter is
    non-zero there. We count constructions across each request to prove the tax
    is REST-only.
    """
    gql_query = nested_parents_query(go_id, depth)
    with _PydanticConstructionCounter() as counter:
        resp = client.post("/graphql", json={"query": gql_query})
        gql_pydantic = counter.count
    assert resp.status_code == 200 and resp.json().get("errors") is None

    with _PydanticConstructionCounter() as counter:
        rest = client.get("/terms", params={"limit": 50})
        rest_pydantic = counter.count
    assert rest.status_code == 200
    rest_rows = len(rest.json())

    assert gql_pydantic == 0, (
        f"ZERO-PYDANTIC ORACLE FAILED: the GraphQL request constructed "
        f"{gql_pydantic} Pydantic model(s) — it must resolve off EntityViews via "
        "getattr with NO DTO conversion (#99/#100). A Pydantic build on the "
        "GraphQL path is the REST tax leaking into GraphQL."
    )
    assert rest_pydantic > 0, (
        "the REST path built no Pydantic DTO — the counter is not wired correctly"
    )
    print("\nZERO-PYDANTIC ORACLE:")
    print(f"  GraphQL nested request: {gql_pydantic} Pydantic models built ✓ "
          "(resolves off EntityViews via getattr)")
    print(f"  REST list of {rest_rows} rows:   {rest_pydantic} Pydantic models built "
          "(the DTO tax is REST-only)")


def main() -> None:
    smoke = os.environ.get("WEB_SMOKE") == "1" or not OBO.exists()
    if smoke:
        reason = "WEB_SMOKE=1" if os.environ.get("WEB_SMOKE") == "1" else f"no {OBO.name}"
        print(f"datacrystal proving ground #7: web tier — SMOKE ({reason}, {sys.platform})\n")
        parsed = synthetic_go(depth=6, fanout=4)
        n_calls = 20
    else:
        print(f"datacrystal proving ground #7: web tier — Gene Ontology ({sys.platform})\n")
        t0 = time.perf_counter()
        parsed = parse_obo(OBO)
        print(f"parsed OBO:  {len(parsed):>8,} terms   {time.perf_counter() - t0:6.2f}s")
        n_calls = 200

    deep_id, deep_depth = deepest_term(parsed)
    # Walk a fixed number of levels (capped by the deepest real chain) — enough to
    # make the N+1 oracle meaningful while keeping the GraphQL selection bounded.
    walk_depth = min(deep_depth, 8 if not smoke else 6)

    # --- INGEST over a CountingBackend (so the same store serves the oracle) --
    shutil.rmtree(STORE, ignore_errors=True)
    backend = CountingBackend(MemoryBackend())
    store = dc.Store._from_backend(backend)  # pyright: ignore[reportPrivateUsage]
    t0 = time.perf_counter()
    ingest(store, parsed)
    n = store.count(Term)
    print(f"ingested:    {n:>8,} terms   {time.perf_counter() - t0:6.2f}s")
    print(f"deepest term: {deep_id} (is_a depth {deep_depth}); nested walk depth {walk_depth}")
    gc.collect()

    app, _handles = build_app(store)
    namespace = "biological_process"

    with TestClient(app) as client:
        run_rest(client, namespace, n=n_calls)
        run_graphql_nested(client, deep_id, depth=walk_depth, n=n_calls)
        assert_zero_pydantic_oracle(client, deep_id, depth=walk_depth)

    # The N+1 oracle runs against the schema directly (over the counting backend),
    # outside the TestClient, so the load count is exactly the request's reads.
    assert_n_plus_1_oracle(store, deep_id, depth=walk_depth)

    store.close()
    print("\ncorrectness: REST + nested GraphQL respond; "
          "N+1 is O(depth) not O(nodes) ✓; GraphQL builds zero Pydantic DTOs ✓")
    if smoke:
        print("\n(SMOKE run — synthetic GO-shaped graph; download go-basic.obo for the "
              "real-data eval. See evals/README.md §#7.)")


if __name__ == "__main__":
    main()
