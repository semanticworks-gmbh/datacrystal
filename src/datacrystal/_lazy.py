"""Explicit lazy references (ROADMAP item 1: explicit ``Lazy[T]``).

A ``Lazy[T]`` is a typed handle to an entity that may not be loaded yet.
It is the only deferred-loading mechanism in v0.x — class-swap ghosts remain
a deferred optimization. Wrap a live entity with ``Lazy.of(obj)`` when
building a graph; on hydration the engine creates unloaded handles that fetch
their target from the store on first ``.get()``.
"""

from __future__ import annotations

import weakref
from typing import Any

from datacrystal._errors import StoreClosedError


class Lazy[T]:
    """A typed, explicitly-lazy reference to an entity.

    * ``Lazy.of(entity)`` — wrap a live entity (loaded handle).
    * ``ref.get()`` — return the target, loading it from the store if needed.
      Loading goes through the store and therefore enforces the ADR-001
      owner-thread contract.
    * ``ref.peek()`` — return the target only if already loaded, else ``None``.
    """

    __slots__ = ("_obj", "_oid", "_storeref")

    def __init__(self) -> None:
        raise TypeError("use Lazy.of(entity) to create a lazy reference")

    @classmethod
    def of(cls, obj: T) -> "Lazy[T]":
        self = object.__new__(cls)
        self._obj = obj
        self._oid = None
        self._storeref = None
        return self

    @classmethod
    def _unloaded(cls, oid: int, store: Any) -> "Lazy[T]":
        self = object.__new__(cls)
        self._obj = None
        self._oid = oid
        self._storeref = weakref.ref(store)
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
            obj = store._load_oid(self._oid)
            self._obj = obj
        return obj

    def peek(self) -> T | None:
        return self._obj

    def __repr__(self) -> str:
        if self._obj is not None:
            return f"Lazy({self._obj!r})"
        return f"Lazy(<unloaded oid={self._oid}>)"
