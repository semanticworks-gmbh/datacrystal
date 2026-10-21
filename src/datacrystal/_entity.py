"""The ``@entity`` decorator, datacrystal's canonical entity form (ROADMAP item 1, DESIGN.md).

An entity is a slots dataclass with a weakref slot plus three engine slots (``__dc_*__``).
Dirty tracking is a one-shot ``__setattr__`` hook enforcing the ADR-001 owner-thread check.
Class-level field access returns a :class:`~datacrystal._conditions.FieldExpr` (fitness #15).
Markers (``Annotated[str, dc.Unique]``) are harvested at first use, after forward refs resolve.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import decimal as _decimal
import types
import weakref
from collections.abc import Callable, Mapping
from typing import (
    Annotated,
    Any,
    Union,
    cast,
    dataclass_transform,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from datacrystal._conditions import FieldExpr
from datacrystal._containers import wrap_value
from datacrystal._errors import FrozenEntityError, NotAnEntityError
from datacrystal._lazy import Lazy
from datacrystal._permissions import PERM_FIELDS, VIEWER, Permissions
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
SortedIndex = _Marker("SortedIndex")  # sorted index → range queries (>=/</between), ADR-004
Blob = _Marker("Blob")        # out-of-line raw bytes, lazy handle on read (ADR-007 / #81)


class _FullText(_Marker):
    """The ``dc.FullText`` marker, bare or called with a language: ``dc.FullText(language="de")``.

    Inert in the core engine (ROADMAP item 10): it only records the declaration in the FieldSpec for
    ``datacrystal[fts]`` to read.
    The parameterized form is in core because the API freezes at the v0.1.0 tag, before the extra
    ships (decided 2026-06-12).
    """

    __slots__ = ("language",)

    def __init__(self, language: str | None = None) -> None:
        super().__init__("FullText")
        self.language = language

    def __call__(self, *, language: str) -> _FullText:
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

    On decode, a record that lacks ``mohs`` but has ``hardness`` binds the old column, so the rename
    follows the code without rewriting records (invariant 8).
    Scoped in v0.2 to non-indexed fields read through live hydration; combining it with
    ``Index``/``Unique`` raises.
    Rewriting old records is the ``migrate`` story.
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
    """Field marker: derive this field from the old record when it is absent from a persisted one
    (#26 (b)).

    ``Glue(fn)`` calls ``fn(old)`` with the persisted record as a read-only mapping.
    It fires only when the field is absent, so it never rewrites a record (invariant 8).
    Scoped like :class:`RenamedFrom`; combining it with ``Index``, ``Unique`` or ``RenamedFrom``
    raises.
    """

    __slots__ = ("fn",)

    def __init__(self, fn: Callable[[Mapping[str, Any]], Any]) -> None:
        super().__init__("Glue")
        if not callable(fn):
            raise TypeError("Glue(fn) takes a callable: old-record mapping -> value")
        self.fn = fn

    def __repr__(self) -> str:
        return "datacrystal.Glue(...)"


# Orderable scalar leaf types admitted as Index/Unique/SortedIndex keys (#106 amends ADR-004 section
# 1; Decimal joined in #184).
# The aware-vs-naive datetime comparability fork is handled in the sorted run (_indexes.py), not at
# this type gate.
_INDEXABLE_TYPES = (str, int, float, bool, _dt.datetime, _dt.date, _decimal.Decimal)


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
    sorted: bool = False  # sorted index → range queries (SortedIndex, ADR-004 / #18)
    blob: bool = False  # out-of-line raw bytes, hydrates as a Blob handle (ADR-007 / #81)
    # May this field hold an entity reference (direct, Lazy, or in a container)?
    # Conservatively True unless the resolved type is provably ref-free — drives
    # the flat-entity ingest fast-path (#52). Default True = always safe.
    entity_ref: bool = True
    decimal: bool = False  # Decimal (or Decimal | None): float literals are refused (#184)


class TypeInfo:
    """Engine-side metadata for one entity class."""

    __slots__ = (
        "_data_field_names",
        "_defaults",
        "_has_entity_refs",
        "_spec_by_name",
        "_specs",
        "cls",
        "field_names",
        "frozen",
        "protected",
        "typename",
    )

    def __init__(self, cls: type, typename: str, field_names: tuple[str, ...],
                 frozen: bool, protected: bool = False) -> None:
        self.cls = cls
        self.typename = typename
        self.field_names = field_names
        self.frozen = frozen
        # THE flag every permission path branches on (ADR-008): one bool per
        # class — owner stamping (W2-2), the upsert label shield (W2-3), the
        # commit gate (W2-5), the readable compiler (W3) all key off it;
        # nothing ever sniffs field names.
        self.protected = protected
        self._specs: tuple[FieldSpec, ...] | None = None
        self._defaults: dict[str, Any] | None = None
        self._spec_by_name: dict[str, FieldSpec] | None = None
        self._has_entity_refs: bool | None = None
        self._data_field_names: tuple[str, ...] | None = None

    @property
    def data_field_names(self) -> tuple[str, ...]:
        """``field_names`` minus the lib-managed ``_dc_`` label columns — the
        fields merge-style writes (``upsert()``) may copy from a fresh
        instance. IS ``field_names`` (the same tuple object) for unprotected
        classes, so the unprotected path pays exactly nothing (W2-3: a fresh
        instance carries R6 birth defaults; copying them over a survivor
        would reset curated labels — and ``/v1/submit`` rides ``upsert``).
        """
        if not self.protected:
            return self.field_names
        cached = self._data_field_names
        if cached is None:
            cached = self._data_field_names = tuple(
                n for n in self.field_names if n not in _PERM_FIELD_SET)
        return cached

    @property
    def specs(self) -> tuple[FieldSpec, ...]:
        if self._specs is None:
            self._specs = _resolve_specs(self.cls, self.field_names)
        return self._specs

    def spec(self, name: str) -> FieldSpec | None:
        """Return the FieldSpec for ``name`` in O(1); ``get()`` and ``get_many()`` hit this on every
        natural-key call.
        """
        by_name = self._spec_by_name
        if by_name is None:
            by_name = self._spec_by_name = {s.name: s for s in self.specs}
        return by_name.get(name)

    @property
    def defaults(self) -> dict[str, Any]:
        """Map field name to a zero-arg factory for the fields that have a default.

        Additive schema evolution fills missing fields from here; a field absent from it cannot be
        added to a class with persisted records.
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
        return tuple(s for s in self.specs if s.indexed or s.unique or s.sorted)

    @property
    def has_entity_refs(self) -> bool:
        """True if any field may hold an entity reference (direct, Lazy, or in a
        container). A type with none skips P1 graph discovery on commit (#52):
        :meth:`Store._register_graph` walks no fields for such an entity, since
        :meth:`Store._walk_value` would only ever hit ref-free leaves.
        """
        cached = self._has_entity_refs
        if cached is None:
            cached = self._has_entity_refs = any(s.entity_ref for s in self.specs)
        return cached

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


_PERM_FIELD_SET = frozenset(PERM_FIELDS)


def _check_reserved(cls: type, name: str) -> None:
    """Reject the ``_dc_`` prefix and ``dc_permissions`` as field names on every entity class
    (ADR-008 Context).

    Unprotected classes are checked too, so ``protected=True`` can be retrofitted later (R7).
    """
    if name.startswith("_dc_") or name == "dc_permissions":
        raise TypeError(
            f"{cls.__name__}.{name}: the '_dc_' prefix and 'dc_permissions' are "
            "reserved for datacrystal's lib-managed permission columns "
            "(ADR-008); rename the field"
        )


def _inject_perm_columns(cls: type, annotations: dict[str, Any]) -> None:
    """Add the four lib-managed label columns to the raw class before ``dataclasses.dataclass()``
    runs (ADR-008 Context).

    All are ``init=False`` with the R6 birth defaults; the R7 legacy fill lives in the decode sites.
    ``_dc_read_floor`` carries SortedIndex (ADR-004 rule 3), ``_dc_owner`` and ``_dc_groups`` carry
    Index (W3-1 / D3), and ``_dc_write_floor`` is unindexed because the gate decodes it from the
    prior record.
    """
    annotations["_dc_owner"] = Annotated[int, Index]
    cls._dc_owner = dataclasses.field(default=0, init=False)
    annotations["_dc_groups"] = Annotated[list[int], Index]
    cls._dc_groups = dataclasses.field(default_factory=list[int], init=False)
    annotations["_dc_read_floor"] = Annotated[int, SortedIndex]
    cls._dc_read_floor = dataclasses.field(default=VIEWER, init=False)
    annotations["_dc_write_floor"] = int
    cls._dc_write_floor = dataclasses.field(default=VIEWER, init=False)


def _permissions_view(self: Any) -> Permissions:
    """Return the injected ``dc_permissions`` getter value: the four columns as one frozen
    :class:`Permissions`.
    """
    return Permissions(
        owner=self._dc_owner,
        groups=tuple(self._dc_groups),
        read_floor=self._dc_read_floor,
        write_floor=self._dc_write_floor,
    )


def _permissions_assign(self: Any, value: Any) -> None:
    """Set the four label columns from a :class:`Permissions`, as the injected setter.

    ``child.dc_permissions = parent.dc_permissions`` copies all four columns verbatim, owner
    included, with groups in a fresh list.
    Legality is judged by the commit gate (W2-5), not here; validation runs before the first column
    write, so a raise stages nothing.
    """
    if not isinstance(value, Permissions):
        raise TypeError(
            f"dc_permissions must be assigned a dc.Permissions, got "
            f"{type(value).__name__}"
        )
    self._dc_owner = value.owner
    self._dc_groups = list(value.groups)  # wraps owner-bound (copy)
    self._dc_read_floor = value.read_floor
    self._dc_write_floor = value.write_floor


@overload
def entity[TEntity](cls: type[TEntity], /) -> type[TEntity]: ...
@overload
def entity[TEntity](*, frozen: bool = False,
           protected: bool = False) -> Callable[[type[TEntity]], type[TEntity]]: ...


@dataclass_transform(eq_default=False, field_specifiers=(dataclasses.field, dataclasses.Field))
def entity(cls: type | None = None, /, *, frozen: bool = False,
           protected: bool = False) -> Any:
    """Declare a datacrystal entity (class decorator).

    Applies ``@dataclass(slots=True, weakref_slot=True, eq=False)`` (equality is identity, one live
    instance per OID), adds the engine slots and dirty-tracking hook, and registers the type.
    ``protected=True`` (ADR-008, epic #168) also injects the four permission columns and the
    read-only ``dc_permissions`` view; unprotected classes pay nothing.
    """

    def wrap(c: type) -> type:
        return _make_entity(c, frozen, protected)

    if cls is None:
        return wrap
    return _make_entity(cls, frozen, protected)


def _make_entity(cls: type, frozen: bool, protected: bool = False) -> type:
    if isinstance(cls, EntityMeta):
        raise TypeError(f"{cls.__name__} is already an @entity class")
    # Reserved-name guard, then injection — both on the raw class's own
    # materialized annotations dict, mutated IN PLACE (the only PEP-649-safe
    # form; assigning a fresh dict breaks lazy-annotation classes).
    annotations = cls.__annotations__
    for name in annotations:
        _check_reserved(cls, name)
    if "dc_permissions" in vars(cls):
        raise TypeError(
            f"{cls.__name__}.dc_permissions: reserved for the lib-managed "
            "permissions view (ADR-008); rename the attribute"
        )
    if protected:
        _inject_perm_columns(cls, annotations)
    base = cast(
        "Any",
        dataclasses.dataclass(
            slots=True, weakref_slot=True, eq=False, frozen=frozen
        )(cls),
    )
    field_names = tuple(f.name for f in dataclasses.fields(base))
    # Belt and braces: a plain-dataclass parent can smuggle fields past the
    # own-annotations check above; the injected four are exempt by name.
    injected = _PERM_FIELD_SET if protected else frozenset[str]()
    for name in field_names:
        if name not in injected:
            _check_reserved(cls, name)
    typename = f"{cls.__module__}:{cls.__qualname__}"

    namespace: dict[str, Any] = {
        "__slots__": ("__dc_oid__", "__dc_state__", "__dc_store__"),
        "__module__": cls.__module__,
        "__qualname__": cls.__qualname__,
        "__dc_fieldset__": frozenset(field_names),
        "__new__": _entity_new,
        "__setattr__": _frozen_setattr if frozen else _tracked_setattr,
    }
    if protected:
        namespace["dc_permissions"] = property(_permissions_view, _permissions_assign)
    final = EntityMeta(cls.__name__, (base,), namespace)

    info = TypeInfo(final, typename, field_names, frozen, protected)
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
    """Return the entity's OID, or ``None`` if it was never registered with a store."""
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
        _check_r11_lazy_only(cls, name, core)
        indexed = any(m is Index for m in markers)
        unique = any(m is Unique for m in markers)
        srt = any(m is SortedIndex for m in markers)
        is_blob = any(m is Blob for m in markers)
        fulltext = next((m for m in markers if isinstance(m, _FullText)), None)
        renamed = next((m for m in markers if isinstance(m, RenamedFrom)), None)
        glued = next((m for m in markers if isinstance(m, Glue)), None)
        lazy_refs = _contains_lazy(core)
        is_list = _is_list_of_scalar(core)
        if is_blob:
            # A blob is a raw-bytes leaf stored out-of-line (ADR-007): it must be
            # `bytes` (or `bytes | None`), and cannot also be indexed/renamed/
            # multivalued — a blob has no key to index, no column to rename, and
            # the bytes never live in the record the index/rename paths read.
            if not _is_bytes_or_optional(core):
                raise TypeError(
                    f"{cls.__name__}.{name}: a dc.Blob field must be bytes "
                    f"(optionally | None) — out-of-line raw bytes have no other "
                    f"shape (ADR-007), got {hint!r}"
                )
            if indexed or unique or srt:
                raise TypeError(
                    f"{cls.__name__}.{name}: a dc.Blob field cannot also be "
                    "Index/Unique/SortedIndex — opaque out-of-line bytes are not "
                    "indexable (ADR-007); index a separate hash/metadata field"
                )
            if fulltext is not None or renamed is not None or glued is not None:
                raise TypeError(
                    f"{cls.__name__}.{name}: a dc.Blob field cannot also be "
                    "FullText/RenamedFrom/Glue — the bytes live out-of-line, not "
                    "in the record those paths read (ADR-007)"
                )
        if (indexed or unique) and not (_is_indexable(core) or is_list):
            raise TypeError(
                f"{cls.__name__}.{name}: Index/Unique fields must be scalar "
                f"(str, int, float, bool, datetime, date or Decimal, optionally | None) "
                f"or a list of scalars, got {hint!r}"
            )
        if srt and not _is_indexable(core):
            raise TypeError(
                f"{cls.__name__}.{name}: SortedIndex fields must be a scalar "
                f"(str, int, float, bool, datetime, date or Decimal, optionally | None) — "
                f"a range index needs an orderable single value, got {hint!r}"
            )
        if unique and is_list:
            raise TypeError(
                f"{cls.__name__}.{name}: a Unique field cannot be a list "
                f"(a multi-valued field has no single key), got {hint!r}"
            )
        if renamed is not None and (indexed or unique or srt):
            raise TypeError(
                f"{cls.__name__}.{name}: RenamedFrom on an indexed field is "
                "not supported yet — v0.2 scopes renames to non-indexed fields "
                "read through live hydration; rename an indexed field via a "
                "migration instead"
            )
        if glued is not None and (indexed or unique or srt):
            raise TypeError(
                f"{cls.__name__}.{name}: Glue on an indexed field is not "
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
            sorted=srt,
            blob=is_blob,
            entity_ref=_may_hold_entity(core),
            decimal=_is_decimal(core),
        ))
    return tuple(specs)


def _is_decimal(hint: Any) -> bool:
    """``Decimal`` or ``Decimal | None``."""
    if hint is _decimal.Decimal:
        return True
    if _is_union(hint):
        args = [a for a in get_args(hint) if a is not type(None)]
        return args == [_decimal.Decimal]
    return False


def _is_bytes_or_optional(hint: Any) -> bool:
    """``bytes`` or ``bytes | None`` — the only shape a ``dc.Blob`` field may
    take (ADR-007: out-of-line raw bytes have no other representation).
    """
    if hint is bytes:
        return True
    if _is_union(hint):
        args = [a for a in get_args(hint) if a is not type(None)]
        return args == [bytes]
    return False


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


def _check_r11_lazy_only(cls: type, field_name: str, hint: Any) -> None:
    """Reject a direct reference to a protected class outside ``Lazy[...]``.

    ADR-008 R11.
    Legal forms are ``Lazy[X]``, ``Lazy[X] | None`` and ``list[Lazy[X]]``; this keeps ``Lazy.get()``
    the one read checkpoint.
    The walk mirrors :func:`_may_hold_entity` but stops at ``Lazy``.
    It runs from :func:`_resolve_specs`, deferred on ``NameError`` like the other per-field checks.
    """
    origin = get_origin(hint)
    if origin is Annotated:
        _check_r11_lazy_only(cls, field_name, get_args(hint)[0])
        return
    if origin is Lazy or hint is Lazy:
        return  # the exemption: a protected class behind Lazy is legal
    if hint in _REF_FREE_LEAVES:
        return
    if origin in (list, tuple, set, frozenset, dict):
        for arg in get_args(hint):
            if arg is not Ellipsis:
                _check_r11_lazy_only(cls, field_name, arg)
        return
    if _is_union(hint):
        for arg in get_args(hint):
            _check_r11_lazy_only(cls, field_name, arg)
        return
    if isinstance(hint, EntityMeta) and type_info(hint).protected:
        raise TypeError(
            f"{cls.__name__}.{field_name}: {hint.__name__} is protected — "
            "protected classes are Lazy-referable only (ADR-008 R11); the "
            f"legal forms are dc.Lazy[{hint.__name__}], "
            f"dc.Lazy[{hint.__name__}] | None, or list[dc.Lazy[{hint.__name__}]]"
        )


def _is_union(hint: Any) -> bool:
    return get_origin(hint) in (Union, types.UnionType)


def _is_indexable(hint: Any) -> bool:
    """Return whether ``hint`` is a scalar (str/int/float/bool/datetime/date/Decimal) or optional
    scalar.

    ``list[scalar]`` is a multi-valued index (:func:`_is_list_of_scalar`), so this checks the Union
    origin, not ``get_args`` alone.
    """
    if hint in _INDEXABLE_TYPES:
        return True
    if _is_union(hint):
        args = [a for a in get_args(hint) if a is not type(None)]
        return bool(args) and all(a in _INDEXABLE_TYPES for a in args)
    return False


# Leaf types _walk_value treats as referencing nothing; the flat-entity fast path (#52) is its
# static twin.
# Anything else (an @entity class, Lazy, Any, a bare container, an unknown type) counts as possibly
# ref-bearing.
_REF_FREE_LEAVES = (str, int, float, bool, bytes, _dt.datetime, _dt.date, _dt.time,
                    _decimal.Decimal, type(None))


def _may_hold_entity(hint: Any) -> bool:
    """Return True unless ``hint`` is provably ref-free (#52).

    Only used to skip work, so it must never return False for a type that could carry an entity ref.
    """
    if get_origin(hint) is Annotated:
        return _may_hold_entity(get_args(hint)[0])
    if hint in _REF_FREE_LEAVES:
        return False
    origin = get_origin(hint)
    if origin in (list, tuple, set, frozenset, dict):
        args = tuple(a for a in get_args(hint) if a is not Ellipsis)
        # A bare/unparameterised container (no args) has unknown elements → walk.
        return (not args) or any(_may_hold_entity(a) for a in args)
    if _is_union(hint):
        return any(_may_hold_entity(a) for a in get_args(hint))
    # entity class, Lazy[...], Any, object, unparameterised generic, unknown.
    return True


def _is_list_of_scalar(hint: Any) -> bool:
    """Return whether ``hint`` is ``list[scalar]`` or ``list[scalar] | None``, an inverted
    multi-valued index (#13).
    """
    if _is_union(hint):
        args = [a for a in get_args(hint) if a is not type(None)]
        if len(args) != 1:
            return False
        hint = args[0]
    if get_origin(hint) is not list:
        return False
    elems = get_args(hint)
    return len(elems) == 1 and elems[0] in _INDEXABLE_TYPES
