"""The ``@entity`` decorator — datacrystal's canonical entity form.

Per ROADMAP item 1 / DESIGN.md, an entity is a **slots dataclass with a
weakref slot** (so the WeakValueDictionary registry can hold it without
keeping it alive), extended with three engine slots:

* ``__dc_oid__``   — the object id, stamped when the entity is registered
* ``__dc_state__`` — NEW / CLEAN / DIRTY (tri-state dirty tracking, item 1)
* ``__dc_store__`` — weakref to the owning store (lets the write hook report)

Dirty tracking is a **one-shot ``__setattr__`` hook**: the first write to a
CLEAN entity notifies the store (and enforces the ADR-001 owner-thread check
*before* mutating); subsequent writes take the fast path. ``frozen=True``
entities never arm the hook at all — mutation always raises (SDA delta 2).

Class-level field access (``Mineral.crystal_system``) is intercepted by the
metaclass and returns a :class:`~datacrystal._conditions.FieldExpr` for the
query AST. Instance attribute access does **not** go through the metaclass,
so reads keep plain-slots speed (fitness function #15 is structural).

Field markers are declared via ``typing.Annotated``::

    @dc.entity
    class Mineral:
        qid: Annotated[str, dc.Unique]
        crystal_system: Annotated[str | None, dc.Index] = None
        type_locality: dc.Lazy[Locality] | None = None

Marker harvesting is deferred to first use (PEP 649 lazy annotations make
forward references resolve once all classes exist).
"""

from __future__ import annotations

import dataclasses
import weakref
from typing import (
    Annotated,
    Any,
    dataclass_transform,
    get_args,
    get_origin,
    get_type_hints,
)

from datacrystal._conditions import FieldExpr
from datacrystal._containers import wrap_value
from datacrystal._errors import FrozenEntityError, NotAnEntityError
from datacrystal._lazy import Lazy
from datacrystal._state import (  # noqa: F401  (STATE_* re-exported)
    STATE_CLEAN,
    STATE_DIRTY,
    STATE_NEW,
    touch,
)


class _Marker:
    """Field marker singleton for use inside ``typing.Annotated``."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"datacrystal.{self.name}"


Index = _Marker("Index")      # secondary bitmap index (pyroaring)
Unique = _Marker("Unique")    # unique secondary key (SDA delta 1)
FullText = _Marker("FullText")  # reserved for datacrystal[fts] (late v0.x)

_INDEXABLE_TYPES = (str, int, float, bool)


@dataclasses.dataclass(frozen=True, slots=True)
class FieldSpec:
    """Resolved per-field metadata (computed lazily from type hints)."""

    name: str
    lazy_refs: bool       # refs inside this field hydrate as Lazy handles
    indexed: bool
    unique: bool
    fulltext: bool


class TypeInfo:
    """Engine-side metadata for one entity class."""

    __slots__ = ("cls", "typename", "field_names", "frozen", "_specs", "_defaults")

    def __init__(self, cls: type, typename: str, field_names: tuple[str, ...],
                 frozen: bool) -> None:
        self.cls = cls
        self.typename = typename
        self.field_names = field_names
        self.frozen = frozen
        self._specs: tuple[FieldSpec, ...] | None = None
        self._defaults: dict[str, Any] | None = None

    @property
    def specs(self) -> tuple[FieldSpec, ...]:
        if self._specs is None:
            self._specs = _resolve_specs(self.cls, self.field_names)
        return self._specs

    @property
    def defaults(self) -> dict[str, Any]:
        """name → zero-arg factory, for the fields that HAVE a default.

        Additive schema evolution fills fields missing from old records from
        here; a field absent from this map cannot be added to a class that
        has persisted records (SchemaMismatchError names it).
        """
        if self._defaults is None:
            out: dict[str, Any] = {}
            for f in dataclasses.fields(self.cls):
                if f.default is not dataclasses.MISSING:
                    out[f.name] = lambda v=f.default: v
                elif f.default_factory is not dataclasses.MISSING:
                    out[f.name] = f.default_factory
            self._defaults = out
        return self._defaults

    def indexed_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(s for s in self.specs if s.indexed or s.unique)

    def __repr__(self) -> str:
        return f"<TypeInfo {self.typename} fields={self.field_names}>"


# Global registry: typename -> TypeInfo, fed by @entity at decoration time.
TYPES_BY_NAME: dict[str, TypeInfo] = {}


class EntityMeta(type):
    """Metaclass that turns class-level field access into FieldExprs.

    Only ``EntityClass.field`` (class access) is intercepted; instance
    attribute lookup never consults the metaclass, so it stays at plain
    slot-descriptor speed.
    """

    def __getattribute__(cls, name: str) -> Any:
        try:
            fields = type.__getattribute__(cls, "__dc_fieldset__")
        except AttributeError:
            return type.__getattribute__(cls, name)
        if name in fields:
            return FieldExpr(cls, name)
        return type.__getattribute__(cls, name)


def _entity_new(cls: type, *args: Any, **kwargs: Any) -> Any:
    self = object.__new__(cls)
    object.__setattr__(self, "__dc_state__", STATE_NEW)
    return self


def _tracked_setattr(self: Any, name: str, value: Any) -> None:
    # Checks the owner thread (raising BEFORE the mutation lands), flips the
    # state to DIRTY and buffers the entity for commit.
    touch(self)
    if isinstance(value, (list, dict, tuple)):
        value = wrap_value(value, self)
    object.__setattr__(self, name, value)


def _frozen_setattr(self: Any, name: str, value: Any) -> None:
    raise FrozenEntityError(
        f"{type(self).__name__} is an @entity(frozen=True) append-only record; "
        "create a new record instead of mutating"
    )


@dataclass_transform(eq_default=False, field_specifiers=(dataclasses.field, dataclasses.Field))
def entity(cls: type | None = None, /, *, frozen: bool = False):
    """Class decorator declaring a datacrystal entity.

    Applies ``@dataclass(slots=True, weakref_slot=True, eq=False)`` (entity
    equality is identity — there is exactly one live instance per OID), adds
    the engine slots and the dirty-tracking hook, and registers the type.
    """

    def wrap(c: type) -> type:
        return _make_entity(c, frozen)

    if cls is None:
        return wrap
    return _make_entity(cls, frozen)


def _make_entity(cls: type, frozen: bool) -> type:
    if isinstance(cls, EntityMeta):
        raise TypeError(f"{cls.__name__} is already an @entity class")
    base = dataclasses.dataclass(  # type: ignore[call-overload]
        slots=True, weakref_slot=True, eq=False, frozen=frozen
    )(cls)
    field_names = tuple(f.name for f in dataclasses.fields(base))
    typename = f"{cls.__module__}:{cls.__qualname__}"

    namespace: dict[str, Any] = {
        "__slots__": ("__dc_oid__", "__dc_state__", "__dc_store__"),
        "__module__": cls.__module__,
        "__qualname__": cls.__qualname__,
        "__dc_fieldset__": frozenset(field_names),
        "__new__": _entity_new,
        "__setattr__": _frozen_setattr if frozen else _tracked_setattr,
    }
    final = EntityMeta(cls.__name__, (base,), namespace)

    info = TypeInfo(final, typename, field_names, frozen)
    type.__setattr__(final, "__dc_typeinfo__", info)
    TYPES_BY_NAME[typename] = info
    return final


def is_entity(obj: Any) -> bool:
    return isinstance(type(obj), EntityMeta)


def type_info(cls_or_obj: Any) -> TypeInfo:
    cls = cls_or_obj if isinstance(cls_or_obj, type) else type(cls_or_obj)
    try:
        return type.__getattribute__(cls, "__dc_typeinfo__")
    except AttributeError:
        raise NotAnEntityError(
            f"{cls.__name__} is not an @entity class"
        ) from None


def oid_of(obj: Any) -> int | None:
    """The entity's OID, or None if it was never registered with a store."""
    try:
        return object.__getattribute__(obj, "__dc_oid__")
    except AttributeError:
        return None


def state_of(obj: Any) -> int:
    return object.__getattribute__(obj, "__dc_state__")


def stamp(obj: Any, oid: int, store: Any, state: int) -> None:
    """Bind an entity to a store: set oid, store weakref and lifecycle state."""
    object.__setattr__(obj, "__dc_oid__", oid)
    object.__setattr__(obj, "__dc_store__", weakref.ref(store))
    object.__setattr__(obj, "__dc_state__", state)


def set_state(obj: Any, state: int) -> None:
    object.__setattr__(obj, "__dc_state__", state)


def set_field(obj: Any, name: str, value: Any) -> None:
    """Set a field bypassing the dirty-tracking hook (hydration only)."""
    object.__setattr__(obj, name, value)


# --- type-hint analysis (lazy: runs on first persistence of a class) -------


def _resolve_specs(cls: type, field_names: tuple[str, ...]) -> tuple[FieldSpec, ...]:
    hints = get_type_hints(cls, include_extras=True)
    specs = []
    for name in field_names:
        hint = hints.get(name, Any)
        markers: list[_Marker] = []
        core = _strip_annotated(hint, markers)
        indexed = any(m is Index for m in markers)
        unique = any(m is Unique for m in markers)
        fulltext = any(m is FullText for m in markers)
        lazy_refs = _contains_lazy(core)
        if (indexed or unique) and not _is_indexable(core):
            raise TypeError(
                f"{cls.__name__}.{name}: Index/Unique fields must be scalar "
                f"(str, int, float or bool, optionally | None), got {hint!r}"
            )
        specs.append(FieldSpec(name, lazy_refs, indexed, unique, fulltext))
    return tuple(specs)


def _strip_annotated(hint: Any, markers: list[_Marker]) -> Any:
    while get_origin(hint) is Annotated:
        args = get_args(hint)
        markers.extend(a for a in args[1:] if isinstance(a, _Marker))
        hint = args[0]
    return hint


def _contains_lazy(hint: Any) -> bool:
    origin = get_origin(hint)
    if origin is Lazy or hint is Lazy:
        return True
    if origin is Annotated:
        return _contains_lazy(get_args(hint)[0])
    return any(_contains_lazy(a) for a in get_args(hint))


def _is_indexable(hint: Any) -> bool:
    if hint in _INDEXABLE_TYPES:
        return True
    # Allow Optional[scalar] / scalar | None
    args = [a for a in get_args(hint) if a is not type(None)]
    if args and all(a in _INDEXABLE_TYPES for a in args):
        return True
    return False
