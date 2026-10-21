"""Owner-bound persistent containers (M2 deliverable, KICKOFF risk 1).

An in-place mutation of a list or dict inside a CLEAN entity would bypass the one-shot
``__setattr__`` hook, so every list/dict entering an entity field is wrapped in a
:class:`PersistentList`/:class:`PersistentDict` bound to its owner.
Every mutator flips the owner to DIRTY *before* mutating, or raises ``FrozenEntityError``.
Wrapping COPIES: after ``e.tags = data``, mutate through ``e.tags``, not ``data``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Self, SupportsIndex, cast

from datacrystal._errors import FrozenEntityError
from datacrystal._state import touch

if TYPE_CHECKING:
    from _typeshed import SupportsKeysAndGetItem, SupportsRichComparison


def wrap_value(value: object, owner: object) -> object:
    """Wrap lists and dicts (recursively) as persistent containers bound to ``owner``.

    Containers already bound to the same owner pass through; everything else is copied.
    """
    if isinstance(value, (PersistentList, PersistentDict)):
        if value._dc_owner is owner:
            return value
        return type(value)(value, owner=owner)
    if isinstance(value, list):
        return PersistentList(cast("list[Any]", value), owner=owner)
    if isinstance(value, dict):
        return PersistentDict(cast("dict[Any, Any]", value), owner=owner)
    if type(value) is tuple:  # exactly tuple: NamedTuples pass through as-is
        return tuple(wrap_value(item, owner) for item in cast("tuple[Any, ...]", value))
    return value


def _is_frozen_owner(owner: object) -> bool:
    if owner is None:
        return False
    # type.__getattribute__ returns Any, so no cast() on this hot (per-wrap) path.
    return type.__getattribute__(type(owner), "__dc_typeinfo__").frozen


type _UpdateSource = SupportsKeysAndGetItem[Any, Any] | Iterable[tuple[Any, Any]]


class PersistentList(list[Any]):
    """A list that marks its owning entity dirty on every mutation."""

    __slots__ = ("_dc_frozen", "_dc_owner")

    def __init__(self, iterable: Iterable[object] = (), *, owner: object = None) -> None:
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

    def append(self, item: object) -> None:
        self._touch()
        super().append(wrap_value(item, self._dc_owner))

    def extend(self, iterable: Iterable[object]) -> None:
        self._touch()
        super().extend(wrap_value(item, self._dc_owner) for item in iterable)

    def insert(self, index: SupportsIndex, item: object) -> None:
        self._touch()
        super().insert(index, wrap_value(item, self._dc_owner))

    def remove(self, item: object) -> None:
        self._touch()
        super().remove(item)

    def pop(self, index: SupportsIndex = -1) -> Any:  # noqa: ANN401 - public element type is dynamic, as in list[Any]
        self._touch()
        return super().pop(index)

    def clear(self) -> None:
        self._touch()
        super().clear()

    def sort(
        self, *, key: Callable[[Any], SupportsRichComparison] | None = None, reverse: bool = False
    ) -> None:
        self._touch()
        super().sort(key=key, reverse=reverse)

    def reverse(self) -> None:
        self._touch()
        super().reverse()

    def __setitem__(self, index: SupportsIndex | slice, value: object) -> None:
        self._touch()
        if isinstance(index, slice):
            super().__setitem__(
                index,
                [wrap_value(item, self._dc_owner) for item in cast("Iterable[object]", value)],
            )
        else:
            super().__setitem__(index, wrap_value(value, self._dc_owner))

    def __delitem__(self, index: SupportsIndex | slice) -> None:
        self._touch()
        super().__delitem__(index)

    def __iadd__(self, other: Iterable[object]) -> Self:
        self.extend(other)
        return self

    def __imul__(self, count: SupportsIndex) -> Self:
        self._touch()
        super().__imul__(count)
        return self


class PersistentDict(dict[Any, Any]):
    """A dict that marks its owning entity dirty on every mutation."""

    __slots__ = ("_dc_frozen", "_dc_owner")

    def __init__(self, mapping: Mapping[Any, Any] | Iterable[Any] = (),
                 *, owner: object = None) -> None:
        self._dc_owner = owner
        self._dc_frozen = _is_frozen_owner(owner)
        super().__init__()
        items = (
            cast("dict[Any, Any]", mapping).items()
            if isinstance(mapping, dict)
            else mapping
        )
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

    def __setitem__(self, key: object, value: object) -> None:
        self._touch()
        super().__setitem__(key, wrap_value(value, self._dc_owner))

    def __delitem__(self, key: object) -> None:
        self._touch()
        super().__delitem__(key)

    def pop(self, key: object, *default: object) -> Any:  # noqa: ANN401 - public value type is dynamic, as in dict[Any, Any]
        self._touch()
        return super().pop(key, *default)

    def popitem(self) -> tuple[Any, Any]:
        self._touch()
        return super().popitem()

    def clear(self) -> None:
        self._touch()
        super().clear()

    def update(self, *args: _UpdateSource, **kwargs: object) -> None:
        self._touch()
        for key, value in dict(*args, **kwargs).items():
            super().__setitem__(key, wrap_value(value, self._dc_owner))

    def setdefault(self, key: object, default: object = None) -> Any:  # noqa: ANN401 - public value type is dynamic, as in dict[Any, Any]
        if key in self:
            return self[key]
        self._touch()
        wrapped = wrap_value(default, self._dc_owner)
        super().__setitem__(key, wrapped)
        return wrapped

    def __ior__(self, other: _UpdateSource) -> Self:
        self.update(other)
        return self
