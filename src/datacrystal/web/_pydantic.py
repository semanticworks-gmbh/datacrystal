"""``datacrystal[web]`` REST boundary — reflect ``@entity`` into Pydantic (#23 / #49 S2-S4).

The REST half of the web tier: the shared reflection in :mod:`._reflect` is
turned into a Pydantic model here (``entity_model`` / ``to_pydantic`` /
``from_pydantic``, landing in #96-#98), giving FastAPI a typed boundary DTO
without leaking the live engine object across the request edge.

``pydantic`` is imported **only at this submodule's top** — never from core and
never from :mod:`._reflect` — so plain ``import datacrystal`` stays inside the
``{msgspec, pyroaring}`` budget (fitness gate ``test_dep_isolation``).

``entity_model`` (#96) is the reflection-only step: it maps each reflected field
to a Pydantic ``(annotation, FieldInfo)`` pair and builds a model via
``pydantic.create_model``. The boundary type a request carries deliberately
diverges from the live engine type where the engine type is not transport-shaped:
a reference field (a ``Lazy`` handle or a directly-typed ``@entity``) crosses the
edge as its **OID** (an ``int``), never the live object — the request edge must
not carry an engine instance (and the DTO has no store to hydrate against). The
``to_pydantic`` / ``from_pydantic`` conversion direction lands in #97-#98.
"""

from __future__ import annotations

import types
from typing import Annotated, Any, Union, get_args, get_origin

import pydantic
from pydantic import ConfigDict, Field, create_model
from pydantic.fields import FieldInfo

from datacrystal._entity import EntityMeta
from datacrystal.web._reflect import FieldDescriptor, reflect

__all__ = ["entity_model"]

# entity_model is pure reflection over a class object, so its result is a
# function of the class alone — cache it (keyed by the class) and hand the same
# generated model back on every call. Keyed on the @entity class itself, which
# is a stable, hashable singleton per entity type.
_MODEL_CACHE: dict[type, type[pydantic.BaseModel]] = {}


def entity_model(cls: type) -> type[pydantic.BaseModel]:
    """Reflect an ``@entity`` class into a Pydantic model mirroring its fields.

    The model's fields follow the persisted schema order (``TypeInfo.field_names``,
    via :func:`~datacrystal.web._reflect.reflect`) and carry the entity's
    marker-stripped core types, so a FastAPI route can declare a typed request /
    response body that can never drift from the entity (#23 / #49 spike S2).

    The result is **cached per class** — entity_model is a pure function of the
    class, so repeated calls (every request handler import, every router build)
    return the *same* model object rather than rebuilding it. Reflection goes
    through :func:`reflect` → ``type_info(cls)``, never ``getattr(cls, field)``
    (class-attribute access returns a query ``FieldExpr`` via the metaclass).

    Field mapping (#96 acceptance criteria):

    * annotation = the field's marker-stripped core type; a ``Lazy`` ref or a
      directly-typed ``@entity`` field becomes an ``int`` OID (``int | None`` if
      the entity declared the field optional) — the request edge carries the id,
      not the live object;
    * a default → an *optional* Pydantic field (the engine stores defaults as a
      zero-arg factory; we call it for the concrete default value); no default →
      a **required** field;
    * an ``@entity(frozen=True)`` class → ``ConfigDict(frozen=True)`` so the DTO
      is immutable like its record-shaped source;
    * the engine's marker flags ride along as ``json_schema_extra`` (``unique`` →
      ``candidate_key``, ``indexed`` → ``queryable``, ``fulltext`` →
      ``searchable``) so generated OpenAPI advertises them.
    """
    cached = _MODEL_CACHE.get(cls)
    if cached is not None:
        return cached

    info, descriptors = reflect(cls)
    field_defs: dict[str, Any] = {}
    for d in descriptors:
        annotation = _boundary_annotation(d)
        field_info = _field_info(info.defaults.get(d.name), d)
        field_defs[d.name] = (annotation, field_info)

    config = ConfigDict(frozen=True) if info.frozen else None
    model = create_model(
        cls.__name__,
        __config__=config,
        **field_defs,
    )
    _MODEL_CACHE[cls] = model
    return model


def _boundary_annotation(d: FieldDescriptor) -> Any:
    """The Pydantic annotation a reflected field carries across the request edge.

    A reference field — the engine hydrates it as a ``Lazy`` handle (``lazy_refs``)
    or it is directly an ``@entity`` type — becomes its **OID** (``int``), so the
    DTO transports an identifier, not a live engine object (the request edge must
    not carry an engine instance, and a detached DTO has no store to hydrate
    against). Optionality is preserved: an optional ref becomes ``int | None``.
    A ``list`` / ``dict`` field keeps its core type as-is (``list[scalar]`` / a
    mapping is already transport-shaped). Everything else keeps its scalar core
    type verbatim.
    """
    core = d.core_type
    if d.spec.lazy_refs or _contains_entity(core):
        return int | None if _is_optional(core) else int
    return core


def _field_info(default_factory: Any, d: FieldDescriptor) -> FieldInfo:
    """Build the Pydantic ``FieldInfo`` (default + marker metadata) for a field.

    ``default_factory`` is the engine's zero-arg default factory (or ``None`` if
    the field has no default). The engine stores *every* default as a factory —
    even literal scalars (see ``TypeInfo.defaults``) — so we **call it** to get
    the concrete default value; absence makes the field required.
    """
    extra = _marker_extra(d)
    if default_factory is None:
        return Field(json_schema_extra=extra or None)
    return Field(default=default_factory(), json_schema_extra=extra or None)


def _marker_extra(d: FieldDescriptor) -> dict[str, Any]:
    """The engine's marker flags as OpenAPI ``json_schema_extra`` keys.

    Surfaces the index/uniqueness/full-text declarations the engine already
    resolved onto the ``FieldSpec`` so generated OpenAPI advertises them to a
    client (``unique`` → candidate key, ``indexed`` → server-side queryable,
    ``fulltext`` → server-side searchable). Only the set-true flags are emitted,
    to keep the schema noise-free.
    """
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
            inner = args[0] if len(args) == 1 else Union[tuple(args)]  # type: ignore[valid-type]
            return inner, True
    return hint, False


def _is_optional(hint: Any) -> bool:
    return _strip_optional(hint)[1]


def _contains_entity(hint: Any) -> bool:
    """True if the marker-stripped core is (or optionally wraps) an ``@entity``.

    Mirrors the engine's reference detection but at boundary-mapping granularity:
    a directly-typed ``@entity`` field (``Locality`` / ``Locality | None``) is a
    reference even though the engine does not hydrate it lazily, so it must cross
    the request edge as an OID just like a ``Lazy`` ref. An ``@entity`` class is
    an instance of :class:`~datacrystal._entity.EntityMeta`.
    """
    if get_origin(hint) is Annotated:
        return _contains_entity(get_args(hint)[0])
    if isinstance(hint, EntityMeta):
        return True
    if get_origin(hint) in (Union, types.UnionType):
        return any(_contains_entity(a) for a in get_args(hint))
    return False
