"""Explicit lazy references (ROADMAP item 1) and their timeout manager.

A ``Lazy[T]`` is a typed handle to an entity that may not be loaded yet.
It is the only deferred-loading mechanism in v0.x — class-swap ghosts remain
a deferred optimization. Wrap a live entity with ``Lazy.of(obj)`` when
building a graph; on hydration the engine creates unloaded handles that
fetch their target from the store on first ``.get()``.

The :class:`LazyReferenceManager` (KICKOFF M2) demotes loaded handles back
to unloaded after a configurable idle timeout, releasing the subgraph behind
the cut point (root reachability = RAM; ``Lazy`` is where both stop).
**Timeout-only in v0.1** — RSS-quota clearing is deferred because psutil
stays out of the core deps (KICKOFF open question 5, recorded decision).

Daemon principle (ADR-001 bound decision 3): the manager NEVER touches the
graph from a foreign thread. Sync stores piggyback ``maybe_sweep()`` on the
owner's API boundaries; ``aopen()`` runs an owner-loop task. Each sweep
records its acting thread so the conformance suite can assert owner-only.
"""

from __future__ import annotations

import threading
import time
import weakref
from typing import Any, Callable, cast

from datacrystal._errors import StoreClosedError


class Lazy[T]:
    """A typed, explicitly-lazy reference to an entity.

    * ``Lazy.of(entity)`` — wrap a live entity (loaded handle).
    * ``ref.get()`` — return the target, loading it from the store if needed.
      Loading goes through the store and therefore enforces the ADR-001
      owner-thread contract.
    * ``ref.peek()`` — return the target only if already loaded, else ``None``.

    A handle that knows its OID and store may be *demoted* (unloaded again)
    by the LazyReferenceManager after idling past the store's
    ``lazy_timeout``; the next ``.get()`` simply reloads — same identity if
    the target is still live anywhere.
    """

    __slots__ = ("_obj", "_oid", "_storeref", "_atime", "_clock", "__weakref__")

    # Slot attribute types declared at class level (annotation-only, no value —
    # compatible with __slots__). Pins ``_obj`` to ``T | None`` so an engine
    # assignment from a loosely-typed store cannot poison it to Unknown.
    _obj: T | None
    _oid: int | None
    _storeref: weakref.ref[Any] | None
    _atime: float
    _clock: Callable[[], float] | None

    def __init__(self) -> None:
        raise TypeError("use Lazy.of(entity) to create a lazy reference")

    @classmethod
    def of(cls, obj: T) -> "Lazy[T]":
        self = object.__new__(cls)
        self._obj = obj
        self._oid = None
        self._storeref = None
        self._atime = 0.0
        self._clock = None
        return self

    @classmethod
    def _loaded(cls, obj: T, oid: int, store: Any) -> "Lazy[T]":
        """Engine path: a hydrated handle whose target is already live
        (registry hit) — demotable, unlike a user-made ``Lazy.of``."""
        self = cls.of(obj)
        self._oid = oid
        self._storeref = weakref.ref(store)
        return self

    @classmethod
    def _unloaded(cls, oid: int, store: Any) -> "Lazy[Any]":
        self = object.__new__(cls)
        self._obj = None
        self._oid = oid
        self._storeref = weakref.ref(store)
        self._atime = 0.0
        self._clock = None
        return self

    @property
    def loaded(self) -> bool:
        return self._obj is not None

    @property
    def oid(self) -> int | None:
        return self._oid

    def get(self) -> T:
        obj = self._obj
        if obj is None:
            storeref = self._storeref
            store = storeref() if storeref is not None else None
            if store is None:
                raise StoreClosedError(
                    "lazy reference cannot load: its store is closed or gone"
                )
            obj = cast(T, store._load_oid(self._oid))
            self._obj = obj
            manager = store._lazyman
            if manager is not None:
                manager.track(self)
        elif self._clock is not None:
            self._atime = self._clock()  # refresh idle time for the manager
        return obj

    def peek(self) -> T | None:
        return self._obj

    def __repr__(self) -> str:
        if self._obj is not None:
            return f"Lazy({self._obj!r})"
        return f"Lazy(<unloaded oid={self._oid}>)"


class LazyReferenceManager:
    """Demotes idle loaded ``Lazy`` handles back to unloaded (timeout-only).

    Owns an injectable ``clock`` (tests never sleep) and a weak set of the
    handles it may demote — only handles that can reload themselves (those
    with an OID and a store) are ever tracked.
    """

    def __init__(self, timeout: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if timeout <= 0:
            raise ValueError(f"lazy_timeout must be positive, got {timeout!r}")
        self._timeout = timeout
        self._clock = clock
        self._handles: weakref.WeakSet[Any] = weakref.WeakSet()
        self._last_sweep = clock()
        # Conformance hooks (fitness #4, daemon principle): which thread
        # demoted last, and how many handles ever.
        self.last_demotion_thread: int | None = None
        self.demoted_total = 0

    @property
    def sweep_interval(self) -> float:
        return max(self._timeout / 4.0, 0.01)

    def track(self, handle: Any) -> None:
        """Register a (re)loaded handle and stamp its access time."""
        handle._clock = self._clock
        handle._atime = self._clock()
        self._handles.add(handle)

    def maybe_sweep(self) -> int:
        """Sweep if an interval has passed — the sync owner's piggyback."""
        if self._clock() - self._last_sweep < self.sweep_interval:
            return 0
        return self.sweep()

    def sweep(self) -> int:
        """Demote every tracked handle idle past the timeout; returns the
        count. Callers are the owner by construction (API piggyback or the
        owner-loop task) — recorded for the conformance suite."""
        now = self._clock()
        self._last_sweep = now
        demoted = 0
        for handle in list(self._handles):
            if handle._obj is not None and handle._oid is not None \
                    and now - handle._atime > self._timeout:
                handle._obj = None  # the next get() reloads through the store
                demoted += 1
        if demoted:
            self.last_demotion_thread = threading.get_ident()
            self.demoted_total += demoted
        return demoted
