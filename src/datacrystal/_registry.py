"""The live-object registry: one live instance per OID.

A ``WeakValueDictionary`` maps OID → live entity (DESIGN.md: this replaces
the JVM-heap mechanics — CPython refcounting collects unreferenced clean
entities, and the registry forgets them automatically thanks to the
``weakref_slot`` every ``@entity`` class carries). Identity is the contract:
while an entity is alive, every path to it yields the *same* object.
"""

from __future__ import annotations

import weakref
from typing import Any


class ObjectRegistry:
    __slots__ = ("_by_oid",)

    def __init__(self) -> None:
        self._by_oid: weakref.WeakValueDictionary[int, Any] = weakref.WeakValueDictionary()

    def get(self, oid: int) -> Any | None:
        return self._by_oid.get(oid)

    def add(self, oid: int, obj: Any) -> None:
        self._by_oid[oid] = obj

    def __len__(self) -> int:
        return len(self._by_oid)
