"""Shared ``@entity`` reflection for the web tier (#23 build plan, #49 spike S1).

One reflection engine, two targets: the Pydantic boundary (:mod:`._pydantic`)
and the Strawberry-over-snapshots boundary (:mod:`._strawberry`) both build
their generated types from the **same** field descriptors produced here, so the
REST and GraphQL surfaces can never drift in which fields they expose or what
core type each carries. The core stays untouched: this module reads the engine's
already-resolved :class:`~datacrystal._entity.TypeInfo`/:class:`FieldSpec` plus a
plain ``get_type_hints`` walk — it never imports a framework (those imports live
only at the top of :mod:`._pydantic` / :mod:`._strawberry`, never in core or here),
so it carries no ``pydantic``/``fastapi``/``strawberry`` dependency itself.

Why mirror the engine instead of re-deriving from raw annotations: the engine
already strips ``Annotated`` markers, resolves forward refs and validates the
field shapes at first persistence (``_resolve_specs``). Reflecting *through*
``TypeInfo`` keeps the web view honest by construction — a field the engine
persists is the field the web layer reflects, with the same name and the same
marker-stripped core type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from datacrystal._entity import FieldSpec, TypeInfo, type_info


@dataclass(frozen=True, slots=True)
class FieldDescriptor:
    """One reflected field, target-agnostic — the unit both web targets consume.

    ``core_type`` is the field's annotation with every ``datacrystal`` marker
    (``Index``/``Unique``/``FullText``/…) stripped off — i.e. the type a caller
    actually sees on the live object, the shape a Pydantic/Strawberry field
    should carry. ``has_default`` records whether the entity declares a default
    for this field (so a generated model can mark it optional); the engine's
    :class:`FieldSpec` is carried verbatim for targets that need the marker
    flags (lazy refs, blob, indexed, …) without re-walking the annotation.
    """

    name: str
    core_type: Any
    has_default: bool
    spec: FieldSpec


def _strip_annotated(hint: Any) -> Any:
    """Peel ``Annotated[...]`` wrappers down to the bare core type.

    The web twin of :func:`datacrystal._entity._strip_annotated` (which also
    *collects* the markers into a list); here we only need the core type, since
    the marker flags already live on the :class:`FieldSpec` the engine resolved.
    """
    while get_origin(hint) is Annotated:
        hint = get_args(hint)[0]
    return hint


def reflect(cls: type) -> tuple[TypeInfo, tuple[FieldDescriptor, ...]]:
    """Reflect an ``@entity`` class into its :class:`TypeInfo` + field descriptors.

    Raises :class:`~datacrystal._errors.NotAnEntityError` (via
    :func:`~datacrystal._entity.type_info`) for a non-``@entity`` class, so the
    web targets reject a bad class loudly at model-build time rather than later.
    Field order follows ``TypeInfo.field_names`` — the persisted schema order,
    the order the engine itself uses — so generated models are deterministic.
    """
    info = type_info(cls)
    hints = get_type_hints(cls, include_extras=True)
    descriptors: list[FieldDescriptor] = []
    for spec in info.specs:
        hint = hints.get(spec.name, Any)
        descriptors.append(
            FieldDescriptor(
                name=spec.name,
                core_type=_strip_annotated(hint),
                has_default=spec.name in info.defaults,
                spec=spec,
            )
        )
    return info, tuple(descriptors)
