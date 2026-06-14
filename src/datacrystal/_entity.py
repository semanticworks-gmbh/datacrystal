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
import types
import weakref
from typing import (
    Annotated,
    Any,
    Callable,
    Mapping,
    Union,
    cast,
    dataclass_transform,
    get_args,
    get_origin,
    get_type_hints,
)

from datacrystal._conditions import FieldExpr
from datacrystal._containers import wrap_value
from datacrystal._errors import FrozenEntityError, NotAnEntityError
from datacrystal._lazy import Lazy
from datacrystal._state import STATE_NEW, touch


class _Marker:
    """Field marker singleton for use inside ``typing.Annotated``."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"datacrystal.{self.name}"


Index = _Marker("Index")      # secondary bitmap index (pyroaring)
Unique = _Marker("Unique")    # unique secondary key (SDA delta 1)


class _FullText(_Marker):
    """The ``dc.FullText`` marker — bare, or parameterized by calling it:
    ``Annotated[str, dc.FullText]`` / ``Annotated[str, dc.FullText(language="de")]``.

    Deliberately **inert in the core engine** (ROADMAP item 10: indexing
    and stemming are ``datacrystal[fts]``'s job). The engine only records
    the declaration in the FieldSpec so consumers can read field + language
    straight from the model — the M3 FTS5 contract spike does today, the
    extra will. The parameterized form exists in core because the API
    freezes at the v0.1.0 tag, before the extra ships (decided 2026-06-12).

    ``language`` is a lowercase short code ("de", "en", …); which codes are
    supported (and the default for ``None``) is the extra's contract.
    """

    __slots__ = ("language",)

    def __init__(self, language: str | None = None) -> None:
        super().__init__("FullText")
        self.language = language

    def __call__(self, *, language: str) -> "_FullText":
        if not language:
            raise TypeError(
                "FullText(language=...) takes a non-empty language code, e.g. "
                'FullText(language="de")'
            )
        return _FullText(language=language)

    def __repr__(self) -> str:
        if self.language is None:
            return "datacrystal.FullText"
        return f"datacrystal.FullText(language={self.language!r})"


FullText = _FullText()  # the bare marker; call it to declare a language


class RenamedFrom(_Marker):
    """Field marker: this field was persisted under a different name (#26 (a)).

    ``mohs: Annotated[float | None, dc.RenamedFrom("hardness")]`` — on decode, a
    record that lacks ``mohs`` but has ``hardness`` binds the old column, so the
    rename follows the code without rewriting old records (additive, invariant
    8; the rename heuristic stays OFF — you name the old field explicitly).

    Scoped to **non-indexed fields read through live hydration** in v0.2;
    combining it with ``Index``/``Unique`` raises (the index/snapshot/arrow
    decode paths don't honor renames yet — that is a follow-on). Rewriting old
    records to the new name is the ``migrate`` story, not this marker.
    """

    __slots__ = ("old_name",)

    def __init__(self, old_name: str) -> None:
        super().__init__("RenamedFrom")
        if not old_name:
            raise TypeError("RenamedFrom(old_name) takes a non-empty field name")
        self.old_name = old_name

    def __repr__(self) -> str:
        return f"datacrystal.RenamedFrom({self.old_name!r})"


class Glue(_Marker):
    """Field marker: derive this field from an OLD record when it is absent from
    a persisted record (#26 (b)) — the declarative reshape hook for schema
    evolution that needs data *moved*, not just renamed.

    ``Glue(fn)`` calls ``fn(old)`` where ``old`` is the persisted record as a
    read-only ``{field_name: value}`` mapping, and uses the result as this
    field's value. It fires **only when the field is absent** from the record's
    own persisted shape — exactly like a default that can read its siblings — so
    once data is written in the new shape the glue is a no-op (it never rewrites
    a record, invariant 8). Split / merge / derive across fields::

        @dc.entity
        class Locality:
            lat: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[0]))]
            lon: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[1]))]
            # old records persisted `coords="48.1,11.5"`; lat/lon follow the code

    Scoped (v0.2, like :class:`RenamedFrom`) to **non-indexed fields read through
    live hydration / decode** (``get``/``query``/``pluck``); the index, snapshot
    and arrow decode paths are a follow-on. Combining it with ``Index``/``Unique``
    or with ``RenamedFrom`` raises. Rewriting old records to the new shape on disk
    is the ``migrate`` story (#26 (c)), not this marker.
    """

    __slots__ = ("fn",)

    def __init__(self, fn: Callable[[Mapping[str, Any]], Any]) -> None:
        super().__init__("Glue")
        if not callable(fn):
            raise TypeError("Glue(fn) takes a callable: old-record mapping -> value")
        self.fn = fn

    def __repr__(self) -> str:
        return "datacrystal.Glue(...)"


_INDEXABLE_TYPES = (str, int, float, bool)


@dataclasses.dataclass(frozen=True, slots=True)
class FieldSpec:
    """Resolved per-field metadata (computed lazily from type hints)."""

    name: str
    lazy_refs: bool       # refs inside this field hydrate as Lazy handles
    indexed: bool
    unique: bool
    fulltext: bool
    fulltext_language: str | None = None  # from FullText(language=...), None if bare
    multivalued: bool = False  # indexed list field — inverted (element) postings (#13)
    renamed_from: str | None = None  # old persisted field name (RenamedFrom, #26 (a))
    glue: Callable[[Mapping[str, Any]], Any] | None = None  # derive-when-absent (Glue, #26 (b))


class TypeInfo:
    """Engine-side metadata for one entity class."""

    __slots__ = ("cls", "typename", "field_names", "frozen", "_specs", "_defaults",
                 "_spec_by_name")

    def __init__(self, cls: type, typename: str, field_names: tuple[str, ...],
                 frozen: bool) -> None:
        self.cls = cls
        self.typename = typename
        self.field_names = field_names
        self.frozen = frozen
        self._specs: tuple[FieldSpec, ...] | None = None
        self._defaults: dict[str, Any] | None = None
        self._spec_by_name: dict[str, FieldSpec] | None = None

    @property
    def specs(self) -> tuple[FieldSpec, ...]:
        if self._specs is None:
            self._specs = _resolve_specs(self.cls, self.field_names)
        return self._specs

    def spec(self, name: str) -> FieldSpec | None:
        """O(1) FieldSpec lookup — ``get()``/``get_many()`` hit this on
        every natural-key call (perf gate ``unique_key_lookup``)."""
        by_name = self._spec_by_name
        if by_name is None:
            by_name = self._spec_by_name = {s.name: s for s in self.specs}
        return by_name.get(name)

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


def _entity_new(cls: type[Any], *args: Any, **kwargs: Any) -> Any:
    # cls: type[Any] (not bare `type`) makes object.__new__ return Any, so no
    # per-instance cast() is needed on this hot (every-entity) path.
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
    base = cast(
        "Any",
        dataclasses.dataclass(  # type: ignore[call-overload]
            slots=True, weakref_slot=True, eq=False, frozen=frozen
        )(cls),
    )
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
    # Resolve the field specs eagerly so a bad Index/Unique type (e.g.
    # Annotated[datetime, Index]) raises its TypeError at the @entity definition
    # site, not lazily on first commit() — far from the mistake (#19).
    # Mutually- or self-referencing Lazy[T] entities can't resolve their hints
    # here: under `from __future__ import annotations` the referent name isn't
    # bound yet, so get_type_hints() raises NameError. Fall back to the lazy
    # path, which re-resolves (and re-validates) once every name exists — the
    # same TypeError, moved earlier when it can be, never removed.
    try:
        _ = info.specs
    except NameError:
        pass
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
    specs: list[FieldSpec] = []
    for name in field_names:
        hint = hints.get(name, Any)
        markers: list[_Marker] = []
        core = _strip_annotated(hint, markers)
        indexed = any(m is Index for m in markers)
        unique = any(m is Unique for m in markers)
        fulltext = next((m for m in markers if isinstance(m, _FullText)), None)
        renamed = next((m for m in markers if isinstance(m, RenamedFrom)), None)
        glued = next((m for m in markers if isinstance(m, Glue)), None)
        lazy_refs = _contains_lazy(core)
        is_list = _is_list_of_scalar(core)
        if (indexed or unique) and not (_is_indexable(core) or is_list):
            raise TypeError(
                f"{cls.__name__}.{name}: Index/Unique fields must be scalar "
                f"(str, int, float or bool, optionally | None) or a list of "
                f"scalars, got {hint!r}"
            )
        if unique and is_list:
            raise TypeError(
                f"{cls.__name__}.{name}: a Unique field cannot be a list "
                f"(a multi-valued field has no single key), got {hint!r}"
            )
        if renamed is not None and (indexed or unique):
            raise TypeError(
                f"{cls.__name__}.{name}: RenamedFrom on an Index/Unique field is "
                "not supported yet — v0.2 scopes renames to non-indexed fields "
                "read through live hydration; rename an indexed field via a "
                "migration instead"
            )
        if glued is not None and (indexed or unique):
            raise TypeError(
                f"{cls.__name__}.{name}: Glue on an Index/Unique field is not "
                "supported yet — v0.2 scopes glue to non-indexed fields read "
                "through live hydration / decode"
            )
        if glued is not None and renamed is not None:
            raise TypeError(
                f"{cls.__name__}.{name}: a field cannot declare both RenamedFrom "
                "and Glue — RenamedFrom binds an old column by name, Glue computes "
                "from the old record; pick one"
            )
        specs.append(FieldSpec(
            name, lazy_refs, indexed, unique,
            fulltext is not None,
            fulltext.language if fulltext is not None else None,
            multivalued=indexed and is_list,
            renamed_from=renamed.old_name if renamed is not None else None,
            glue=glued.fn if glued is not None else None,
        ))
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


def _is_union(hint: Any) -> bool:
    return get_origin(hint) in (Union, types.UnionType)


def _is_indexable(hint: Any) -> bool:
    """A scalar (str/int/float/bool) or optional scalar (``| None``).

    Deliberately rejects ``list[scalar]`` — that is a *multi-valued* index
    (:func:`_is_list_of_scalar`), maintained with element-wise postings, not a
    single scalar key. This must key off the Union origin, not args alone:
    ``get_args(list[str])`` is also ``(str,)``, so an args-only check would
    wrongly accept a list as a scalar (and then crash on the unhashable list
    key at insert)."""
    if hint in _INDEXABLE_TYPES:
        return True
    if _is_union(hint):
        args = [a for a in get_args(hint) if a is not type(None)]
        return bool(args) and all(a in _INDEXABLE_TYPES for a in args)
    return False


def _is_list_of_scalar(hint: Any) -> bool:
    """``list[scalar]`` or ``list[scalar] | None`` — an inverted (multi-valued)
    index over the list's elements (#13). Rejects bare ``list`` (no element
    type), ``list[Ref]``, nested ``list[list[...]]``, and ``dict``."""
    if _is_union(hint):
        args = [a for a in get_args(hint) if a is not type(None)]
        if len(args) != 1:
            return False
        hint = args[0]
    if get_origin(hint) is not list:
        return False
    elems = get_args(hint)
    return len(elems) == 1 and elems[0] in _INDEXABLE_TYPES
