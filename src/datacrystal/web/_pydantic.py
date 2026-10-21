"""``datacrystal[web]`` REST boundary — reflect ``@entity`` into Pydantic (#23 / #49 S2-S4).

``entity_model`` (#96) builds a DTO from the shared reflection; ``to_pydantic`` (#97) projects
an entity or ``EntityView`` into it; ``from_pydantic`` (#98) rebuilds a live entity.
A reference crosses the edge as its OID, never a live object.
``pydantic`` is imported only here, never from core or :mod:`._reflect`.
See ``docs/design/SPIKE-pydantic-interop.md`` and ``docs/reference.md``.

"""

from __future__ import annotations

import types
from typing import Annotated, Any, Literal, Union, cast, get_args, get_origin

import pydantic
from pydantic import ConfigDict, Field, create_model
from pydantic.fields import FieldInfo

from datacrystal._entity import EntityMeta, is_entity, oid_of, type_info
from datacrystal._lazy import BlobHandle, Lazy
from datacrystal._records import BlobToken, RefToken
from datacrystal._redacted import Redacted
from datacrystal._snapshot import EntityView, Ref
from datacrystal.web._reflect import FieldDescriptor, list_ref_target, reflect

__all__ = ["entity_model", "from_pydantic", "to_pydantic"]

# entity_model is a pure function of the class and the face, so its result is cached.
Face = Literal["plain", "create", "public"]
_MODEL_CACHE: dict[tuple[type, Face], type[pydantic.BaseModel]] = {}

# Class-name suffix per face; "plain" keeps the bare class name.
_FACE_SUFFIX: dict[Face, str] = {"plain": "", "create": "Create", "public": "Public"}


def entity_model(cls: type, *, face: Face = "plain") -> type[pydantic.BaseModel]:
    """Reflect an ``@entity`` class into a cached Pydantic model mirroring its fields (#96, #98).

    Faces: ``"plain"`` (the declared fields), ``"create"`` (a request body, ``{Name}Create``)
    and ``"public"`` (a ``response_model``, ``{Name}Public``, plus a required ``oid``).
    The mapping rules are in ``docs/reference.md`` (the ``datacrystal.web`` reflection API).
    Reflection uses ``type_info``, because ``getattr(cls, field)`` returns a query ``FieldExpr``.
    """
    cached = _MODEL_CACHE.get((cls, face))
    if cached is not None:
        return cached

    info, descriptors = reflect(cls)
    field_defs: dict[str, Any] = {}
    if face == "public":
        # Required: a response is only built from a committed (OID-bearing) read.
        field_defs["oid"] = (int, Field(json_schema_extra={"primary_key": True}))
    for d in descriptors:
        annotation = _boundary_annotation(d)
        field_info = _field_info(info.defaults.get(d.name), d)
        field_defs[d.name] = (annotation, field_info)

    # base64 for bytes in both directions: the default utf-8 mode raises on non-utf-8 content
    # and silently re-encodes utf-8 bytes (#153).
    config = ConfigDict(
        from_attributes=True,
        frozen=info.frozen,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )
    model = create_model(
        cls.__name__ + _FACE_SUFFIX[face],
        __config__=config,
        **field_defs,
    )
    _MODEL_CACHE[(cls, face)] = model
    return model


def to_pydantic(
    source: Any, *, nested: int = 0, face: Face = "plain"
) -> pydantic.BaseModel:
    """Project a live entity or ``EntityView`` into a detached :func:`entity_model` DTO (#97, #98).

    Prefer an ``EntityView`` (store-free); a live entity is read on the owner thread (ADR-001).
    A reference crosses as its OID and a load is never forced; ``nested=N`` recurses into
    already-resident referents only.
    A ``Redacted`` source returns ``None`` despite the annotation (ADR-008 W4-4).
    """
    if isinstance(source, Redacted):
        # Checked before EntityView: a RedactedView is also an EntityView.
        return None  # pyrefly: ignore[bad-return]  # documented wire-null return
    cls: type
    values: dict[str, Any]
    oid: int | None
    if isinstance(source, EntityView):
        cls = _type_info_for_view(source)
        values = dict(source.fields())
        oid = source.oid
    elif is_entity(source):
        ti = type_info(source)
        cls = ti.cls  # the canonical class (typed), not type(source) (Unknown)
        _guard_owner(source)  # ADR-001: read on the owner thread, pre-read
        values = {name: getattr(source, name) for name in ti.field_names}
        oid = oid_of(source)
    else:
        raise TypeError(
            f"to_pydantic() takes a live @entity or an EntityView, got "
            f"{type(source).__name__!r} — for a cross-thread read pass a "
            "store.snapshot() view"
        )
    model = entity_model(cls, face=face)
    data = {name: _dto_value(value, nested) for name, value in values.items()}
    if face == "public":
        if oid is None:
            raise ValueError(
                "to_pydantic(face='public') needs an OID-bearing source — the "
                "entity was never committed (store and commit it first)"
            )
        data["oid"] = oid
    # A nested DTO sits in an int-typed ref slot, which model_validate would reject.
    if nested > 0 and _has_nested_dto(data):
        return model.model_construct(**data)
    return model.model_validate(data)


def from_pydantic(model_instance: pydantic.BaseModel, cls: type, *, store: Any = None) -> Any:
    """Rebuild a live ``@entity`` of ``cls`` from a boundary DTO via its public constructor (#98).

    The result is a fresh ``STATE_NEW`` instance; the engine slots are never written, and a
    ``public`` face's ``oid`` is ignored.
    Reference OIDs stay raw OIDs unless ``store=`` is given, which resolves them with one
    owner-thread ``get_many``.
    """
    _, descriptors = reflect(cls)  # raises NotAnEntityError loudly for a non-@entity class
    # field_values takes only names from reflect(), which drops the ``_dc_*`` permission columns
    # (ADR-008), so a client cannot spoof a floor through the request edge (W4-4).
    field_values: dict[str, Any] = {}
    # (name, descriptor, is list edge (#103), OIDs) per ref field; all OIDs flatten into one
    # get_many.
    ref_plan: list[tuple[str, FieldDescriptor, bool, list[int]]] = []
    for d in descriptors:
        if not hasattr(model_instance, d.name):
            continue  # absent on this face/DTO → fall to the dataclass default
        value = getattr(model_instance, d.name)
        is_ref = d.spec.lazy_refs or _contains_entity(d.core_type)
        if is_ref and store is not None:
            if list_ref_target(d.core_type) is not None and isinstance(value, list):
                # A list edge crosses as list[int] (#103); an empty list stays empty.
                oids = [int(o) for o in cast("list[Any]", value)]
                ref_plan.append((d.name, d, True, oids))
                continue
            if isinstance(value, int):
                ref_plan.append((d.name, d, False, [value]))  # one round-trip below
                continue
        field_values[d.name] = value  # scalar / container / OID-as-is (no store)

    if ref_plan:
        assert store is not None  # ref_plan is filled only when a store was passed
        # One owner-thread round-trip for every ref OID (ADR-001, no N+1).
        flat_oids = [oid for _, _, _, oids in ref_plan for oid in oids]
        resolved: list[Any] = store.get_many(flat_oids) if flat_oids else []
        cursor = 0
        for name, d, is_list, oids in ref_plan:
            targets = resolved[cursor : cursor + len(oids)]
            cursor += len(oids)
            # A Lazy field carries a handle, a direct @entity field the entity itself.
            if is_list:
                field_values[name] = (
                    [Lazy[Any].of(t) for t in targets] if d.spec.lazy_refs else targets
                )
            else:
                target = targets[0]
                field_values[name] = (
                    Lazy[Any].of(target) if d.spec.lazy_refs else target
                )

    return cls(**field_values)  # public constructor → STATE_NEW; never pokes __dc_* slots


def _type_info_for_view(view: EntityView) -> type:
    """Return the live ``@entity`` class named by a store-free snapshot view's typename.

    The class must be imported in this process.
    """
    from datacrystal._entity import TYPES_BY_NAME

    ti = TYPES_BY_NAME.get(view.typename)
    if ti is None:
        raise TypeError(
            f"to_pydantic() needs the live @entity class for {view.typename!r} "
            "to build its model — import the class in this process first"
        )
    return ti.cls


def _dto_value(value: Any, nested: int) -> Any:
    """Project one field value to its detached DTO form (#97), like the engine's read transforms.

    References become OIDs, containers plain ``list`` / ``dict``; ``nested`` is as in
    :func:`to_pydantic`.
    No branch forces a load.
    """
    if isinstance(value, Ref):
        return value.oid
    if isinstance(value, RefToken):  # raw decoded token (defensive; views freeze)
        return value.oid
    if isinstance(value, (BlobToken, BlobHandle)):
        # Project the blob OID, never .bytes(), which would force a load (ADR-007 §3).
        return value.blob_oid
    if is_entity(value):
        oid = oid_of(value)
        if oid is None:
            raise ValueError(
                "cannot project an entity that was never stored — it has no OID "
                "(store and commit it first)"
            )
        if nested > 0:
            return to_pydantic(value, nested=nested - 1)
        return oid
    if isinstance(value, Lazy):
        handle = cast("Lazy[Any]", value)
        target = handle.peek()  # mirror swizzle()/_view_value(): a loaded handle
        if target is not None:  # is best — recurse only into what is resident
            return _dto_value(target, nested)
        if handle.oid is None:
            raise ValueError("cannot project an unloaded Lazy without an OID")
        return handle.oid
    if isinstance(value, (list, tuple)):
        # A live PersistentList is a list, a view's container a tuple.
        return [_dto_value(item, nested) for item in cast("list[Any]", value)]
    if isinstance(value, dict):
        # A live PersistentDict is a dict, a view's container a MappingProxyType.
        return {k: _dto_value(v, nested) for k, v in cast("dict[Any, Any]", value).items()}
    return value  # scalars, None, datetime/date/time pass native


def _has_nested_dto(data: dict[str, Any]) -> bool:
    """Report whether any projected value carries a nested DTO, which needs ``model_construct``."""
    return any(_contains_model(v) for v in data.values())


def _contains_model(value: Any) -> bool:
    """Report whether ``value`` is or recursively contains a Pydantic model."""
    if isinstance(value, pydantic.BaseModel):
        return True
    if isinstance(value, list):
        return any(_contains_model(v) for v in cast("list[Any]", value))
    if isinstance(value, dict):
        return any(_contains_model(v) for v in cast("dict[Any, Any]", value).values())
    return False


def _guard_owner(entity: Any) -> None:
    """Raise ``WrongThreadError`` if a foreign thread projects a live entity (ADR-001).

    A never-stored entity, or one whose store was collected, has no owner, so the guard does
    nothing.
    """
    try:
        storeref = object.__getattribute__(entity, "__dc_store__")
    except AttributeError:
        return  # never stored — no owner to confine to
    store = storeref() if storeref is not None else None
    if store is not None:
        store._guard()  # raises WrongThreadError off the owner thread (ADR-001)


def _boundary_annotation(d: FieldDescriptor) -> Any:
    """Return the Pydantic annotation a reflected field carries across the request edge.

    A reference becomes ``int`` (its OID), a list edge ``list[int]`` (#103), each ``| None`` if
    optional.
    The list edge is detected by ``list_ref_target`` because ``lazy_refs`` is true for both shapes.
    Every other field keeps its core type.
    """
    core = d.core_type
    if list_ref_target(core) is not None:
        return list[int] | None if _is_optional(core) else list[int]
    if d.spec.lazy_refs or _contains_entity(core):
        return int | None if _is_optional(core) else int
    return core


def _field_info(default_factory: Any, d: FieldDescriptor) -> FieldInfo:
    """Build the Pydantic ``FieldInfo`` (default and marker metadata) for a field.

    The engine stores every default as a zero-arg factory (``TypeInfo.defaults``), so it is called
    here;
    no factory makes the field required.
    """
    extra = _marker_extra(d)
    if default_factory is None:
        return Field(json_schema_extra=extra or None)
    return Field(default=default_factory(), json_schema_extra=extra or None)


def _marker_extra(d: FieldDescriptor) -> dict[str, Any]:
    """Return the engine's set marker flags as OpenAPI ``json_schema_extra`` keys."""
    spec = d.spec
    extra: dict[str, Any] = {}
    if spec.unique:
        extra["candidate_key"] = True
    if spec.indexed:
        extra["queryable"] = True
    if spec.fulltext:
        extra["searchable"] = True
    return extra


def _strip_optional(hint: Any) -> tuple[Any, bool]:
    """Split ``X | None`` into ``(X, True)``; a non-optional hint → ``(hint, False)``."""
    if get_origin(hint) in (Union, types.UnionType):
        args = [a for a in get_args(hint) if a is not type(None)]
        if len(args) < len(get_args(hint)):
            inner = args[0] if len(args) == 1 else Union[tuple(args)]  # noqa: UP007 - Union[...] over a runtime tuple has no | spelling  # pyrefly: ignore[invalid-annotation]
            return inner, True
    return hint, False


def _is_optional(hint: Any) -> bool:
    """Report whether ``hint`` is ``X | None``."""
    return _strip_optional(hint)[1]


def _contains_entity(hint: Any) -> bool:
    """Report whether the marker-stripped core is, or optionally wraps, an ``@entity``.

    A directly-typed ``@entity`` field is a reference too, so it crosses as an OID like a ``Lazy``.
    """
    if get_origin(hint) is Annotated:
        return _contains_entity(get_args(hint)[0])
    if isinstance(hint, EntityMeta):
        return True
    if get_origin(hint) in (Union, types.UnionType):
        return any(_contains_entity(a) for a in get_args(hint))
    return False
