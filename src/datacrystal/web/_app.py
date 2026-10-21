"""``datacrystal[web]`` app wiring — a FastAPI app over a store (#23 / #49 S; #92).

A lifespan owns the store, and per-request dependencies give a route snapshots and owner writes
without the threading rules.
The deployment doctrine is in ``docs/how-to/web-deployment.md``.
``fastapi`` is imported only here, never from core or :mod:`._reflect`.

"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request

from datacrystal._actors import Principal
from datacrystal._errors import WriteDeniedError
from datacrystal._snapshot import Snapshot
from datacrystal._store import Store
from datacrystal.web._strawberry import snapshot_context

__all__ = [
    "SNAPSHOT_CONTEXT_KEY",
    "create_app",
    "get_principal",
    "get_store",
    "graphql_context_getter",
    "read_snapshot",
    "store_lifespan",
    "submit_write",
]

#: The ``app.state`` attribute under which the lifespan pins the one process store.
STORE_STATE_KEY = "dc_store"

#: The ``app.state`` attribute for the per-store :class:`_SnapshotPool` (#104), pinned by the
#: lifespan or the first read.
SNAPSHOT_POOL_STATE_KEY = "dc_snapshot_pool"


class _PooledSnapshot:
    """One shared snapshot at a single watermark, refcounted (#104).

    A superseded snapshot is closed only once its last in-flight reader releases it.
    ``refs`` and ``retired`` are mutated only under the owning :class:`_SnapshotPool`'s lock.
    """

    __slots__ = ("refs", "retired", "snapshot", "tid")

    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.tid = snapshot.tid  # the watermark this snapshot pins (the cache key)
        self.refs = 0
        self.retired = False


class _SnapshotPool:
    """Watermark-keyed snapshot cache for the web read path (#104).

    A per-request snapshot rebuilds its query index over the whole store (~52 ms at 38k records).
    The pool builds that index once per commit and shares it between readers at the watermark.
    A commit retires the old snapshot, which closes once drained.
    Thread-safe: ``acquire`` and ``release`` take the lock; the index build happens outside it.
    """

    __slots__ = ("_current", "_lock", "_store")

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._current: _PooledSnapshot | None = None

    def acquire(self) -> _PooledSnapshot:
        """Return the shared snapshot for the current watermark, refcount += 1."""
        with self._lock:
            wm = self._store.last_tid  # cheap int read; building under the lock is
            cur = self._current        # fine — snapshot() is O(1), index build is lazy
            if cur is None or cur.tid != wm:
                if cur is not None:
                    cur.retired = True
                    self._maybe_close(cur)
                # Pin the base to anonymous (ADR-008 W4-4): a bare ``snapshot()`` binds the
                # operator's ``store.principal``, fail-open if that is root.
                # Requests ride their own identity via ``Snapshot.for_principal``.
                cur = _PooledSnapshot(self._store.snapshot(principal=Principal(uid=0)))
                self._current = cur
            cur.refs += 1
            return cur

    def release(self, pooled: _PooledSnapshot) -> None:
        """Drop one reader; close a retired snapshot once its last reader is gone."""
        with self._lock:
            pooled.refs -= 1
            self._maybe_close(pooled)

    def _maybe_close(self, pooled: _PooledSnapshot) -> None:
        # caller holds the lock. Snapshot.close() is idempotent (guards _closed).
        if pooled.retired and pooled.refs <= 0:
            pooled.snapshot.close()

    def close(self) -> None:
        """Close the live snapshot on shutdown (lifespan exit drains requests first)."""
        with self._lock:
            cur, self._current = self._current, None
            if cur is not None:
                cur.snapshot.close()


_POOL_CREATE_LOCK = threading.Lock()


def _get_pool(request: Request) -> _SnapshotPool:
    """Return the per-store snapshot pool off ``app.state`` (#104), creating it on first use.

    A hand-wired app that pins only the store gets its pool here, under a lock so two first
    requests never build two.
    """
    app = request.app
    pool = getattr(app.state, SNAPSHOT_POOL_STATE_KEY, None)
    if not isinstance(pool, _SnapshotPool):
        with _POOL_CREATE_LOCK:
            pool = getattr(app.state, SNAPSHOT_POOL_STATE_KEY, None)
            if not isinstance(pool, _SnapshotPool):
                pool = _SnapshotPool(get_store(request))
                setattr(app.state, SNAPSHOT_POOL_STATE_KEY, pool)
    return pool


def store_lifespan(
    path: str | Path, **open_kwargs: Any
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build a FastAPI ``lifespan`` that opens one store per worker process.

    Use ``FastAPI(lifespan=store_lifespan(path))``; ``**open_kwargs`` go to :meth:`Store.open`.
    The thread running startup becomes the owner thread (ADR-001); the open is synchronous.
    A second worker on the same directory fails with ``StoreLockedError``, hence ``workers=1``.
    """
    return lambda app: _StoreLifespan(app, Path(path), open_kwargs)


class _StoreLifespan:
    """The async-context-manager lifespan returned by :func:`store_lifespan`.

    A class rather than a generator so the opened :class:`Store` is reachable as ``.store`` for
    tests.
    """

    __slots__ = ("_app", "_open_kwargs", "_path", "store")

    def __init__(self, app: FastAPI, path: Path, open_kwargs: dict[str, Any]) -> None:
        self._app = app
        self._path = path
        self._open_kwargs = open_kwargs
        self.store: Store | None = None

    async def __aenter__(self) -> None:
        """Open the store on the lifespan thread and pin it and its snapshot pool."""
        # Returns None (the stateless-lifespan shape); the store is pinned, not yielded as state.
        store = Store.open(self._path, **self._open_kwargs)
        self.store = store
        setattr(self._app.state, STORE_STATE_KEY, store)
        setattr(self._app.state, SNAPSHOT_POOL_STATE_KEY, _SnapshotPool(store))

    async def __aexit__(self, *exc: object) -> bool | None:
        """Close the pool's snapshot, then the store (the snapshot holds a read view of it)."""
        pool = getattr(self._app.state, SNAPSHOT_POOL_STATE_KEY, None)
        if isinstance(pool, _SnapshotPool):
            pool.close()
            setattr(self._app.state, SNAPSHOT_POOL_STATE_KEY, None)
        store = self.store
        if store is not None:
            store.close()  # drains the IO worker, persists the index sidecar
            self.store = None
        return None


def get_store(request: Request) -> Store:
    """Return the one process store off ``app.state`` (a FastAPI dependency).

    Raises ``RuntimeError`` if the app was not wired with :func:`store_lifespan`.
    """
    store = getattr(request.app.state, STORE_STATE_KEY, None)
    if not isinstance(store, Store):
        raise RuntimeError(  # noqa: TRY004 - RuntimeError is the documented contract
            "no datacrystal store on app.state — build the app with "
            "FastAPI(lifespan=store_lifespan(path)) so the store is opened on "
            "startup (#92)"
        )
    return store


def get_principal() -> Principal | None:
    """Return the per-request principal (epic #168 W2-8, ADR-008); ``None`` means anonymous.

    A request is a third party's write, so it never stamps the operator's store-opening identity.
    On the read path ``None`` reads exactly the anonymous readable set (fail-closed).
    Override it through ``app.dependency_overrides`` (see ``docs/how-to/web-deployment.md``),
    returning a ``Principal``, not a bare uid: ``acting_as(uid)`` applies the sponsor gate.
    """
    return None


def read_snapshot(
    request: Request,
    principal: Principal | None = Depends(get_principal),
) -> Iterator[Snapshot]:
    """Yield the pooled snapshot for the current watermark, bound to the request principal.

    Use it as ``snap: Snapshot = Depends(read_snapshot)``; it is readable from any thread or loop
    (ADR-002).
    A generator dependency, so the pool refcount is released in teardown even if the handler raises.
    The principal rides on the shared core through ``Snapshot.for_principal`` in O(1) (ADR-008 R15);
    ``None`` reads as the anonymous base.
    """
    pool = _get_pool(request)
    pooled = pool.acquire()
    try:
        yield (
            pooled.snapshot
            if principal is None
            else pooled.snapshot.for_principal(principal)
        )
    finally:
        pool.release(pooled)


async def submit_write(
    request: Request,
    principal: Principal | None = Depends(get_principal),
) -> _OwnerWriter:
    """Return a callable that ships a mutation to the owner and resolves once it has committed.

    Use it as ``write = Depends(submit_write)`` and ``await write(fn)`` with ``fn(store) ->
    result``.
    The closure runs on the owner thread via ``store.submit()`` (ADR-001), so the route never
    mutates directly.
    It must return plain data: a live entity in the result raises ``EntityEscapeError``.
    """
    return _OwnerWriter(get_store(request), principal)


class _OwnerWriter:
    """The awaitable write callable :func:`submit_write` returns (#92)."""

    __slots__ = ("_principal", "_store")

    def __init__(self, store: Store, principal: Principal | None = None) -> None:
        self._store = store
        self._principal = principal

    async def __call__(self, fn: Callable[[Store], Any]) -> Any:
        """Run ``fn(store)`` on the owner thread as the request principal and await the commit."""
        store = self._store
        # acting_as must wrap inside the closure: the store resets the acting stack around
        # every submitted closure (W1). None is an explicit anonymous scope.
        principal = self._principal if self._principal is not None else Principal(uid=0)

        def run() -> Any:
            with store.acting_as(principal):
                return fn(store)

        future = store.submit(run)
        try:
            return await asyncio.wrap_future(future)
        except WriteDeniedError as exc:
            # Mapped here so hand-wired apps get it too; denial happens in P1, nothing committed
            # (R10).
            raise HTTPException(403, detail={
                "error": "write-denied", "message": str(exc),
            }) from exc


#: The GraphQL context key under which :func:`graphql_context_getter` stashes the per-request
#: snapshot.
SNAPSHOT_CONTEXT_KEY = "dc_snapshot"


def graphql_context_getter(
    snapshot: Snapshot = Depends(read_snapshot),
) -> Mapping[str, Any]:
    """Build a per-request GraphQL ``context`` of ``{snapshot, loader}`` (#92/#100).

    Pass it as ``GraphQLRouter(context_getter=...)``.
    The snapshot comes from :func:`read_snapshot`, shared with REST and released in teardown.
    The loader is fresh per request: a longer-lived caching loader would leak entities across
    requests and watermarks (#100).
    """
    context = dict(snapshot_context(snapshot))
    context[SNAPSHOT_CONTEXT_KEY] = snapshot
    return context


def create_app(
    path: str | Path,
    *,
    routers: list[Any] | None = None,
    **open_kwargs: Any,
) -> FastAPI:
    """Build a FastAPI app with the store lifespan wired and each of ``routers`` included (#92).

    An app that needs custom FastAPI construction should call
    ``FastAPI(lifespan=store_lifespan(...))`` itself.
    """
    app = FastAPI(lifespan=store_lifespan(path, **open_kwargs))
    for router in routers or []:
        app.include_router(router)
    return app
