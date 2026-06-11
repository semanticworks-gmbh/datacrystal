"""Owner-bound persistent containers (M2 deliverable, KICKOFF risk 1).

In-place mutation of a list/dict inside a CLEAN entity used to be invisible
to the one-shot ``__setattr__`` hook — the #1 silent-lost-write footgun in
both ancestor systems (ZODB, EclipseStore). The fix: every list/dict that
enters an entity field (by assignment, construction or hydration) is wrapped
in a :class:`PersistentList`/:class:`PersistentDict` bound to its owning
entity. Every mutator flips the owner to DIRTY *before* mutating (so the
ADR-001 owner-thread check still raises pre-mutation); mutating a container
owned by an ``@entity(frozen=True)`` record raises ``FrozenEntityError``.

Semantics: wrapping COPIES the container — containers are by-value parts of
their owner (they round-trip by value, never by reference). After assigning
``e.tags = data``, mutate through ``e.tags``, not through ``data``.

Containers hold a strong reference to their owner: as long as you can reach
a container, its owner cannot be collected out from under it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, SupportsIndex

from datacrystal._errors import FrozenEntityError
from datacrystal._state import touch


def wrap_value(value: Any, owner: Any) -> Any:
    """Wrap lists/dicts (recursively) as persistent containers bound to
    ``owner``. Already-bound containers of the same owner pass through;
    everything else is copied into a fresh bound wrapper."""
    if isinstance(value, (PersistentList, PersistentDict)):
        if value._dc_owner is owner:
            return value
        return type(value)(value, owner=owner)
    if isinstance(value, list):
        return PersistentList(value, owner=owner)
    if isinstance(value, dict):
        return PersistentDict(value, owner=owner)
    if type(value) is tuple:  # exactly tuple: NamedTuples pass through as-is
        return tuple(wrap_value(item, owner) for item in value)
    return value


def _is_frozen_owner(owner: Any) -> bool:
    if owner is None:
        return False
    return type(owner).__dc_typeinfo__.frozen


class PersistentList(list):
    """A list that marks its owning entity dirty on every mutation."""

    __slots__ = ("_dc_owner", "_dc_frozen")

    def __init__(self, iterable: Iterable[Any] = (), *, owner: Any = None) -> None:
        self._dc_owner = owner
        self._dc_frozen = _is_frozen_owner(owner)
        super().__init__(wrap_value(item, owner) for item in iterable)

    def _touch(self) -> None:
        if self._dc_frozen:
            raise FrozenEntityError(
                f"this list belongs to a frozen @entity "
                f"({type(self._dc_owner).__name__}); append-only records "
                "cannot be mutated"
            )
        if self._dc_owner is not None:
            touch(self._dc_owner)

    # -- mutators: touch first (may raise), then mutate, wrapping new items --

    def append(self, item: Any) -> None:
        self._touch()
        super().append(wrap_value(item, self._dc_owner))

    def extend(self, iterable: Iterable[Any]) -> None:
        self._touch()
        super().extend(wrap_value(item, self._dc_owner) for item in iterable)

    def insert(self, index: SupportsIndex, item: Any) -> None:
        self._touch()
        super().insert(index, wrap_value(item, self._dc_owner))

    def remove(self, item: Any) -> None:
        self._touch()
        super().remove(item)

    def pop(self, index: SupportsIndex = -1) -> Any:
        self._touch()
        return super().pop(index)

    def clear(self) -> None:
        self._touch()
        super().clear()

    def sort(self, *, key: Any = None, reverse: bool = False) -> None:
        self._touch()
        super().sort(key=key, reverse=reverse)

    def reverse(self) -> None:
        self._touch()
        super().reverse()

    def __setitem__(self, index: Any, value: Any) -> None:
        self._touch()
        if isinstance(index, slice):
            super().__setitem__(
                index, [wrap_value(item, self._dc_owner) for item in value]
            )
        else:
            super().__setitem__(index, wrap_value(value, self._dc_owner))

    def __delitem__(self, index: Any) -> None:
        self._touch()
        super().__delitem__(index)

    def __iadd__(self, other: Iterable[Any]) -> "PersistentList":
        self.extend(other)
        return self

    def __imul__(self, count: SupportsIndex) -> "PersistentList":
        self._touch()
        super().__imul__(count)
        return self


class PersistentDict(dict):
    """A dict that marks its owning entity dirty on every mutation."""

    __slots__ = ("_dc_owner", "_dc_frozen")

    def __init__(self, mapping: Mapping[Any, Any] | Iterable[Any] = (),
                 *, owner: Any = None) -> None:
        self._dc_owner = owner
        self._dc_frozen = _is_frozen_owner(owner)
        super().__init__()
        items = mapping.items() if isinstance(mapping, dict) else mapping
        for key, value in items:
            super().__setitem__(key, wrap_value(value, owner))

    def _touch(self) -> None:
        if self._dc_frozen:
            raise FrozenEntityError(
                f"this dict belongs to a frozen @entity "
                f"({type(self._dc_owner).__name__}); append-only records "
                "cannot be mutated"
            )
        if self._dc_owner is not None:
            touch(self._dc_owner)

    # -- mutators ------------------------------------------------------------

    def __setitem__(self, key: Any, value: Any) -> None:
        self._touch()
        super().__setitem__(key, wrap_value(value, self._dc_owner))

    def __delitem__(self, key: Any) -> None:
        self._touch()
        super().__delitem__(key)

    def pop(self, key: Any, *default: Any) -> Any:
        self._touch()
        return super().pop(key, *default)

    def popitem(self) -> tuple[Any, Any]:
        self._touch()
        return super().popitem()

    def clear(self) -> None:
        self._touch()
        super().clear()

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._touch()
        for key, value in dict(*args, **kwargs).items():
            super().__setitem__(key, wrap_value(value, self._dc_owner))

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key in self:
            return self[key]
        self._touch()
        wrapped = wrap_value(default, self._dc_owner)
        super().__setitem__(key, wrapped)
        return wrapped

    def __ior__(self, other: Any) -> "PersistentDict":
        self.update(other)
        return self
