"""Shared ``@entity`` reflection for the web tier (#23 build plan, #49 spike S1).

The Pydantic and Strawberry targets build their types from the same field descriptors,
so REST and GraphQL cannot drift in which fields they expose.
Reflecting through the engine's ``TypeInfo`` (not raw annotations) keeps the web view equal to what
the
engine persists; the module imports no framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from datacrystal._entity import EntityMeta, FieldSpec, TypeInfo, type_info
from datacrystal._lazy import Lazy


@dataclass(frozen=True, slots=True)
class FieldDescriptor:
    """One reflected field, target-agnostic — the unit both web targets consume.

    ``core_type`` is the annotation with every ``datacrystal`` marker stripped.
    ``has_default`` lets a generated model mark the field optional.
    ``spec`` carries the engine's marker flags verbatim.
    """

    name: str
    core_type: Any
    has_default: bool
    spec: FieldSpec


def _strip_annotated(hint: Any) -> Any:
    """Peel ``Annotated[...]`` wrappers down to the bare core type."""
    while get_origin(hint) is Annotated:
        hint = get_args(hint)[0]
    return hint


def referenced_entities(core_type: Any) -> tuple[type, ...]:
    """Return the ``@entity`` classes a field's core type can point at (deduped, in order).

    Walks through ``Lazy[...]``, unions and containers: ``Lazy[Locality] | None`` yields
    ``(Locality,)``, ``list[Mineral]`` yields ``(Mineral,)``, a scalar field yields ``()``.
    """
    seen: dict[int, type] = {}
    _collect_entities(core_type, seen)
    return tuple(seen.values())


def list_ref_target(core_type: Any) -> type | None:
    """Return the single ``@entity`` referent of a list-valued reference field, or ``None``.

    Covers ``list[Lazy[T]]`` and ``list[T]`` / ``tuple[T, ...]`` edges (story #103).
    :func:`referenced_entities` flattens containers and cannot tell a list edge from a scalar ref,
    so the container origin is tested here.
    Returns ``None`` for a list of scalars or of a union of several entities (single-target
    reflection only).
    """
    inner = _strip_annotated(core_type)
    if get_origin(inner) not in (list, tuple):
        return None
    targets = referenced_entities(inner)
    return targets[0] if len(targets) == 1 else None


def _collect_entities(hint: Any, seen: dict[int, type]) -> None:
    while get_origin(hint) is Annotated:
        hint = get_args(hint)[0]
    if isinstance(hint, EntityMeta):
        seen.setdefault(id(hint), hint)
        return
    origin = get_origin(hint)
    if origin is Lazy or hint is Lazy:
        for arg in get_args(hint):
            _collect_entities(arg, seen)
        return
    for arg in get_args(hint):
        if arg is type(None) or arg is Ellipsis:
            continue
        _collect_entities(arg, seen)


def reflect(cls: type) -> tuple[TypeInfo, tuple[FieldDescriptor, ...]]:
    """Reflect an ``@entity`` class into its :class:`TypeInfo` and field descriptors.

    Raises :class:`~datacrystal._errors.NotAnEntityError` for a non-``@entity`` class.
    Field order is the persisted schema order, so generated models are deterministic.
    """
    info = type_info(cls)
    hints = get_type_hints(cls, include_extras=True)
    descriptors: list[FieldDescriptor] = []
    for spec in info.specs:
        if spec.name.startswith("_dc_"):
            # Lib-managed permission columns (ADR-008): security labels must
            # never ride REST/GraphQL DTOs — not readable in responses, not
            # client-settable on any face. Every web surface builds on this
            # reflection, so one skip covers them all.
            continue
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
