"""``datacrystal[web]`` app wiring — a FastAPI app over a store (#23 / #49 S; #92).

The deploy/scale glue the rest of the web tier (#98 REST, #100 GraphQL) plugs into:
a **lifespan** that owns the store, **per-request** read/context/write dependencies,
and the deployment **doctrine** they encode (single-writer owner thread, ADR-001).
The access primitives are not invented here — ``store.snapshot()`` (ADR-002 read
views), ``store.submit()`` / the ``aopen()`` owner-loop (ADR-001), and
``snapshot_context()`` (the per-request DataLoader, #100) already shipped. This
module is only where a route or resolver reaches them **without ever learning the
threading rules**.

The deployment doctrine, in one breath (GUIDE "FastAPI/Strawberry deployment")
------------------------------------------------------------------------------

* **One store per worker process — ``workers=1``.** A store is single-writer
  (the lease lock, invariant 10); ``uvicorn --workers 4`` is four processes and
  the second one to open the directory fails with ``StoreLockedError``. The
  lifespan opens exactly one store for the process and pins it on ``app.state``.
* **Reads scale through snapshots, not the live graph.** A read dependency
  (:func:`read_snapshot`) hands each request a frozen ``store.snapshot()`` — an
  any-thread/any-loop read view (ADR-002) — so a sync route dispatched to a
  threadpool worker, or an async route on the loop, reads committed state without
  ever touching a live entity or violating owner confinement (ADR-001). The
  snapshot is closed when the request ends (it holds a WAL read txn).
* **Writes serialize through the owner.** A foreign thread may not mutate the
  graph (``WrongThreadError``, unchanged); it **ships a closure** to the owner
  via :func:`submit_write`. The mutation + commit runs on the owner thread, and
  the dependency returns only once it has committed — back-pressure by
  construction, never a torn write.

``fastapi`` is imported **only at this submodule's top** — never from core and
never from :mod:`._reflect` — so plain ``import datacrystal`` stays inside the
``{msgspec, pyroaring}`` budget (fitness gate ``test_import_isolation_*``). A
bare ``import datacrystal`` never touches this package; importing
``datacrystal.web`` (hence this module) is what requires the ``web`` extra.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request

from datacrystal._snapshot import Snapshot
from datacrystal._store import Store
from datacrystal.web._strawberry import snapshot_context

__all__ = [
    "SNAPSHOT_CONTEXT_KEY",
    "create_app",
    "get_store",
    "graphql_context_getter",
    "read_snapshot",
    "store_lifespan",
    "submit_write",
]

#: The attribute on ``app.state`` under which the lifespan pins the one
#: process store. A module constant (not a bare string at the call sites) so the
#: lifespan and the dependencies can never disagree on the name — the same
#: discipline as :data:`._strawberry.LOADER_CONTEXT_KEY`.
STORE_STATE_KEY = "dc_store"


def store_lifespan(
    path: str | Path, **open_kwargs: Any
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build a FastAPI ``lifespan`` that opens ONE store per worker process.

    Pass the result as ``FastAPI(lifespan=store_lifespan("cabinet.store"))``:
    on startup it opens the store at ``path`` (forwarding ``**open_kwargs`` to
    :meth:`Store.open` — ``durability=``, ``cache_index=``, …) on the **server
    process's main thread**, which is therefore the store's owner thread
    (ADR-001 owner confinement); on shutdown it closes the store (draining the
    IO worker, persisting the index sidecar). The open store is pinned on
    ``app.state`` under :data:`STORE_STATE_KEY`, where :func:`get_store` reaches
    it.

    **One store per worker, single-writer (invariant 10).** This is why a
    datacrystal app runs ``workers=1``: a second worker process opening the same
    directory fails the lease lock with ``StoreLockedError``. Scale reads across
    snapshots within the one process (see the module doctrine), not across
    writer processes.

    The store is opened **synchronously** in the async startup — boot is
    O(checkpoint), a one-time blocking scan (the same cost ``aopen()`` pays at
    startup), not O(history). The owner thread is whichever thread runs the
    lifespan startup; FastAPI runs it on the main event loop's thread, so the
    owner is that thread for the process's life.
    """
    return lambda app: _StoreLifespan(app, Path(path), open_kwargs)


class _StoreLifespan:
    """The async-context-manager lifespan returned by :func:`store_lifespan`.

    A small class (rather than a bare ``@asynccontextmanager`` generator) so the
    opened :class:`Store` is reachable as ``.store`` for tests and so the
    ``path``/``open_kwargs`` capture reads cleanly. ``__aenter__`` opens + pins
    the store on ``app.state`` (returning ``None``, the stateless-lifespan shape),
    ``__aexit__`` closes it — exactly the startup/shutdown FastAPI expects.
    """

    __slots__ = ("_app", "_path", "_open_kwargs", "store")

    def __init__(self, app: FastAPI, path: Path, open_kwargs: dict[str, Any]) -> None:
        self._app = app
        self._path = path
        self._open_kwargs = open_kwargs
        self.store: Store | None = None

    async def __aenter__(self) -> None:
        # Open on the lifespan thread: it becomes the store's owner thread for
        # the process's life (ADR-001). Boot is O(checkpoint), one blocking scan.
        # Returns None (the Starlette stateless-lifespan shape); the store is
        # pinned on app.state, not yielded as merged lifespan state.
        store = Store.open(self._path, **self._open_kwargs)
        self.store = store
        setattr(self._app.state, STORE_STATE_KEY, store)

    async def __aexit__(self, *exc: object) -> bool | None:
        store = self.store
        if store is not None:
            store.close()  # drains the IO worker, persists the index sidecar
            self.store = None
        return None


def get_store(request: Request) -> Store:
    """The one process store, off ``app.state`` (a FastAPI dependency).

    Use it directly (``store: Store = Depends(get_store)``) when a route needs
    the live store — e.g. to call :func:`submit_write`. Raises ``RuntimeError``
    if the app was not wired with :func:`store_lifespan` (the store never landed
    on ``app.state``), pointing at the fix rather than ``AttributeError``-ing.
    """
    store = getattr(request.app.state, STORE_STATE_KEY, None)
    if not isinstance(store, Store):
        raise RuntimeError(
            "no datacrystal store on app.state — build the app with "
            "FastAPI(lifespan=store_lifespan(path)) so the store is opened on "
            "startup (#92)"
        )
    return store


def read_snapshot(request: Request) -> Iterator[Snapshot]:
    """Yield a per-request ``store.snapshot()``, closed when the request ends.

    The **read** dependency: ``snap: Snapshot = Depends(read_snapshot)``. A
    snapshot is a frozen read view at the durable watermark, callable from **any
    thread or loop** (ADR-002 read views), so the route reads
    :class:`~datacrystal.EntityView`/:class:`~datacrystal.Ref` — never a live
    entity — and is correct whether FastAPI runs it on the loop (async ``def``)
    or in a threadpool worker (sync ``def``). Owner confinement is never at risk
    because nothing here touches the live graph (ADR-001).

    A generator dependency (``yield``) so FastAPI closes the snapshot in the
    request's teardown even if the handler raises — important on the sqlite
    backend, where an open snapshot holds a WAL read txn that blocks checkpoint
    truncation (close promptly, the GUIDE rule).
    """
    store = get_store(request)
    with store.snapshot() as snap:
        yield snap


async def submit_write(request: Request) -> "_OwnerWriter":
    """Yield a callable that fans a mutation into the owner and returns committed.

    The **write** dependency: ``write: ... = Depends(submit_write)``. The route
    calls ``await write(fn)`` with a closure ``fn(store) -> result``; the closure
    is shipped to the store owner via ``store.submit()`` (ADR-001's sanctioned
    cross-thread write path), runs the mutation **+ commit** on the owner thread,
    and the ``await`` resolves only once it has committed — back-pressure by
    construction. A foreign thread mutating the graph **directly** still raises
    ``WrongThreadError`` (unchanged); the whole point of going through the owner
    is that the route never has to.

    Bridging the ``concurrent.futures.Future`` from ``submit()`` to the loop is
    :func:`asyncio.wrap_future`, so awaiting it never blocks the event loop while
    the owner runs the write. The closure must return **plain data** — a live
    entity in the result (even nested, or behind a ``Lazy``) fails with
    ``EntityEscapeError`` (the ``submit()`` contract); return an OID or a DTO.
    """
    return _OwnerWriter(get_store(request))


class _OwnerWriter:
    """The awaitable write callable :func:`submit_write` yields (#92).

    Holds the store and exposes ``await writer(fn)``: ship ``fn`` to the owner
    via ``store.submit`` and await its result on the loop. A thin class (not a
    closure) so the store binding is inspectable and the call signature is a
    typed method rather than an untyped lambda.
    """

    __slots__ = ("_store",)

    def __init__(self, store: Store) -> None:
        self._store = store

    async def __call__(self, fn: Callable[[Store], Any]) -> Any:
        store = self._store
        # submit() ships the closure to the owner; from the owner thread it runs
        # inline (same rules). wrap_future lets the loop await the owner's commit
        # without blocking (ADR-001 cross-thread write path).
        future = store.submit(lambda: fn(store))
        return await asyncio.wrap_future(future)


#: The key under which :func:`graphql_context_getter` stashes the per-request
#: snapshot on the GraphQL context, alongside the DataLoader. The snapshot is
#: closed by FastAPI's dependency teardown (the snapshot rides in on
#: :func:`read_snapshot`, a generator dependency), not from inside the context.
SNAPSHOT_CONTEXT_KEY = "dc_snapshot"


def graphql_context_getter(
    snapshot: Snapshot = Depends(read_snapshot),  # noqa: B008 — FastAPI dep marker
) -> Mapping[str, Any]:
    """Build a per-request GraphQL ``context`` of ``{snapshot, loader}`` (#92/#100).

    Pass as the Strawberry ``GraphQLRouter(context_getter=...)``. The snapshot is
    injected from :func:`read_snapshot` (a FastAPI **generator** dependency), so
    its WAL read txn is closed in the request teardown — Strawberry's
    ``context_getter`` has no teardown hook of its own, and reusing the read
    dependency is what gives the GraphQL request the same promptly-closed snapshot
    a REST route gets.

    From that one snapshot the context carries a **fresh**
    :class:`~datacrystal.web.SnapshotLoader` (``cache=False``) via
    :func:`~datacrystal.web.snapshot_context`. Per-request, per-snapshot
    construction is the load-bearing property (#100): a process-lifetime loader
    caches by default and would leak resolved entities across requests **and**
    across snapshot watermarks (a stale read after a commit). Request scoping is
    built here, never inherited. Every field on the request reads from this one
    watermark (ADR-002 read views), so a graph traversal is internally consistent
    even while the owner keeps committing.
    """
    context = dict(snapshot_context(snapshot))
    context[SNAPSHOT_CONTEXT_KEY] = snapshot
    return context


def create_app(
    path: str | Path,
    *,
    routers: "list[Any] | None" = None,
    **open_kwargs: Any,
) -> FastAPI:
    """A FastAPI app with the store lifespan wired — the one-call assembly (#92).

    Equivalent to ``FastAPI(lifespan=store_lifespan(path, **open_kwargs))`` plus
    ``app.include_router(r)`` for each router in ``routers``. The store opens on
    startup and closes on shutdown (one per worker — run ``workers=1``); routes
    reach it through :func:`get_store` / :func:`read_snapshot` / :func:`submit_write`.

    A convenience over hand-wiring the lifespan; an app that needs custom FastAPI
    construction (middleware, sub-apps) should call ``FastAPI(lifespan=...)``
    itself with :func:`store_lifespan`.
    """
    app = FastAPI(lifespan=store_lifespan(path, **open_kwargs))
    for router in routers or []:
        app.include_router(router)
    return app
