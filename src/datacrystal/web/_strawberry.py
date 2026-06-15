"""``datacrystal[web]`` GraphQL boundary — reflect ``@entity`` into Strawberry (#23 / #49 S6).

The GraphQL half of the web tier: the shared reflection in :mod:`._reflect` is
turned into a Strawberry type here, resolving fields over a pinned
``store.snapshot()`` — never the live engine object. The reflection rules
(which fields, what core type) come straight from the engine's
:class:`~datacrystal._entity.TypeInfo`, so the REST (:mod:`._pydantic`) and
GraphQL surfaces can never disagree on what an entity exposes.

What this story (#99) builds, and where it stops:

* **Scalar fields** (``str``/``int``/``float``/``bool``, and ``list``-of-scalar
  via :func:`datacrystal._entity._is_list_of_scalar`) become GraphQL scalar
  fields with **no resolver** — Strawberry's *default* resolver
  (``StrawberryField.get_result`` → ``default_resolver(source, python_name)``,
  ``types/field.py:234``) does a plain ``getattr`` against the source, which is
  a frozen :class:`~datacrystal._snapshot.EntityView`
  (``EntityView.__getattr__``, ``_snapshot.py:114``). No Pydantic, no copy, no
  live entity crosses the request edge.
* **Entity-reference fields** (``FieldSpec.entity_ref`` / ``lazy_refs``) become
  GraphQL *object* fields whose declared type is the referenced ``@entity``'s
  reflected Strawberry type. They too carry **no resolver** here — the default
  resolver returns the raw :class:`~datacrystal._snapshot.Ref` token sitting in
  the view. Turning that ``Ref`` into the referenced view is the **relation
  resolver wired in #100 (S6b)**, batched per-request by a DataLoader (#101) —
  not hand-hydrated here.

Mutually- and self-referential entities (``Mineral`` ↔ ``Locality``) are built
with the type registry in :class:`StrawberryReflector`: each entity gets one
Strawberry type, cached by typename; a reference field's annotation is patched
in **after** both endpoint types exist (Strawberry resolves field types lazily
at schema-conversion, so the post-build patch is honoured). No hand-written
per-entity schema class — :func:`strawberry.tools.create_type` builds each type
from the reflected field list at runtime.

``strawberry`` is imported **only at this submodule's top** — never from core and
never from :mod:`._reflect` — so plain ``import datacrystal`` stays inside the
``{msgspec, pyroaring}`` budget (fitness gate ``test_import_isolation_*``).
"""

from __future__ import annotations

from typing import Any

from strawberry.annotation import StrawberryAnnotation
from strawberry.tools import create_type
from strawberry.types.field import StrawberryField

from datacrystal._entity import TypeInfo, _is_list_of_scalar
from datacrystal.web._reflect import FieldDescriptor, referenced_entities, reflect

__all__ = ["StrawberryReflector", "reflect_strawberry_type"]


def _is_scalar_field(desc: FieldDescriptor) -> bool:
    """A field GraphQL can serve directly off the frozen view: a scalar, an
    optional scalar, or a ``list`` of scalars (`#13`-style multi-valued field).

    Mirrors the engine's own indexable/list-of-scalar split
    (:func:`datacrystal._entity._is_indexable` / ``_is_list_of_scalar``) so the
    GraphQL scalar set is exactly the leaf set the engine treats as plain data —
    no second definition of "scalar" to drift."""
    from datacrystal._entity import _is_indexable

    return _is_indexable(desc.core_type) or _is_list_of_scalar(desc.core_type)


class StrawberryReflector:
    """Builds one Strawberry type per ``@entity`` class, cached by typename.

    The registry exists so a graph of mutually-referential entities reflects
    into a graph of mutually-referential Strawberry types without re-creating a
    type (Strawberry rejects two distinct types sharing a GraphQL name) and
    without infinite recursion on a cycle. #100 reuses the same cache to attach
    the relation resolver to the reference fields this story leaves resolver-less.
    """

    def __init__(self) -> None:
        self._types: dict[str, type] = {}
        # Reference fields whose target type is patched in once both ends exist
        # (the cycle break): (field, referenced typename) deferred until resolve().
        self._pending_refs: list[tuple[StrawberryField, str]] = []

    def reflect(self, cls: type) -> type:
        """Reflect ``cls`` (and every entity it references) into a Strawberry type.

        Idempotent per typename: a class already in the cache returns its built
        type, so a cycle terminates and a shared referent is one GraphQL type.
        """
        info, descriptors = reflect(cls)
        cached = self._types.get(info.typename)
        if cached is not None:
            return cached
        gql_type = self._build(info, descriptors)
        self._resolve_pending()
        return gql_type

    def _build(self, info: TypeInfo, descriptors: tuple[FieldDescriptor, ...]) -> type:
        # The engine's persisted typename is ``module:QualName`` (lineage-stable);
        # the GraphQL name is the bare class name — the identifier a schema reader
        # sees. (A future story may add a name-override hook for cross-module
        # name collisions; v1 takes the class name.)
        gql_name = info.cls.__name__
        fields: list[StrawberryField] = []
        referents: list[type] = []  # entity classes to reflect AFTER this is cached
        for desc in descriptors:
            field = self._field_for(desc, referents)
            if field is not None:
                fields.append(field)
        if not fields:
            raise ValueError(
                f"@entity {gql_name!r} reflects to no GraphQL-mappable field "
                "— a GraphQL type needs at least one scalar or reference field"
            )
        gql_type = create_type(gql_name, fields)
        # Cache this type BEFORE recursing into the referents so a cycle back to
        # it (referent → … → this type) terminates on the cache hit instead of
        # rebuilding — the cycle break. The reference fields' target annotations
        # are still placeholders here; they are patched once every referent is
        # built (``_resolve_pending``, called by the outer ``reflect``).
        self._types[info.typename] = gql_type
        for target in referents:
            self.reflect(target)
        return gql_type

    def _field_for(
        self, desc: FieldDescriptor, referents: list[type]
    ) -> StrawberryField | None:
        """One reflected field → a Strawberry field, or ``None`` to skip it.

        A reference field's target class is appended to ``referents`` to be
        reflected *after* the current type is cached (never recursed into here —
        that would loop on a cycle before the cache entry exists).

        Resolver-less by design: the default ``getattr`` resolver reads scalars
        straight off the :class:`~datacrystal._snapshot.EntityView` and returns a
        reference field's :class:`~datacrystal._snapshot.Ref` token verbatim
        (#100 swaps in the batched relation resolver)."""
        if _is_scalar_field(desc):
            return _plain_field(desc.name, desc.core_type)
        targets = referenced_entities(desc.core_type)
        if not targets:
            # Neither a scalar nor an entity reference (bare ``list``, ``dict``,
            # a ``dc.Blob`` bytes field, …) — GraphQL has no native shape for it,
            # so it is not part of the reflected type. A later story may map blobs
            # to a URL field; nothing here pretends to expose them.
            return None
        # An entity reference: a single-target edge keeps the referent's GraphQL
        # type; a multi-target (heterogeneous container/union of entities) is
        # out of scope for v1's single-type reflection.
        if len(targets) != 1:
            return None
        target = targets[0]
        target_info, _ = reflect(target)
        field = _plain_field(desc.name, object)  # placeholder, patched in resolve
        self._pending_refs.append((field, target_info.typename))
        referents.append(target)
        return field

    def _resolve_pending(self) -> None:
        """Patch each deferred reference field's annotation to its (nullable)
        target Strawberry type, now that every endpoint type is in the cache.

        Strawberry resolves a field's type lazily at schema-conversion, so the
        annotation set here — after ``create_type`` — is the one the schema
        sees. Nullable so an absent reference (a ``None`` / missing edge)
        validates without a non-null GraphQL violation."""
        still_pending: list[tuple[StrawberryField, str]] = []
        for field, typename in self._pending_refs:
            target = self._types.get(typename)
            if target is None:
                still_pending.append((field, typename))
                continue
            field.type_annotation = StrawberryAnnotation(target | None)
        self._pending_refs = still_pending


def reflect_strawberry_type(cls: type) -> type:
    """Reflect an ``@entity`` class into a Strawberry GraphQL type (#99).

    Convenience over a fresh :class:`StrawberryReflector` for the common case of
    one reflected root; callers that reflect several entities into one schema
    should share a single reflector so referents map to one GraphQL type each.
    """
    return StrawberryReflector().reflect(cls)


def _plain_field(name: str, core_type: Any) -> StrawberryField:
    """A resolver-less Strawberry field of ``core_type`` named ``name``.

    With no ``base_resolver``, Strawberry uses the default resolver
    (``types/field.py:234``) — a ``getattr(source, name)`` against whatever the
    parent field returns, which on this surface is a frozen
    :class:`~datacrystal._snapshot.EntityView` (``_snapshot.py:114``)."""
    return StrawberryField(
        python_name=name,
        type_annotation=StrawberryAnnotation(core_type),
    )
