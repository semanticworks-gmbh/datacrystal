"""``datacrystal[web]`` GraphQL boundary — reflect ``@entity`` into Strawberry (#23 / #49 S6).

The shared reflection in :mod:`._reflect` becomes Strawberry types resolving over a pinned snapshot.
Scalar fields use the default ``getattr`` on the frozen ``EntityView`` (#99).
Reference fields batch through a per-request :class:`SnapshotLoader` (#100, ``web-deployment.md``).
``strawberry`` is imported only here, never from core or :mod:`._reflect`.

"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, cast

from strawberry.annotation import StrawberryAnnotation
from strawberry.dataloader import DataLoader
from strawberry.tools import create_type
from strawberry.types import Info
from strawberry.types.field import StrawberryField
from strawberry.types.fields.resolver import StrawberryResolver

from datacrystal._entity import (
    TypeInfo,
    _is_list_of_scalar,  # engine leaf-set predicate, no second "scalar" definition
)
from datacrystal._redacted import Redacted
from datacrystal._snapshot import EntityView, Ref, Snapshot
from datacrystal.web._reflect import (
    FieldDescriptor,
    list_ref_target,
    referenced_entities,
    reflect,
)

__all__ = [
    "LOADER_CONTEXT_KEY",
    "SnapshotLoader",
    "StrawberryReflector",
    "reflect_strawberry_type",
    "snapshot_context",
]

#: The ``info.context`` key under which a per-request :class:`SnapshotLoader` lives.
LOADER_CONTEXT_KEY = "dc_snapshot_loader"


class SnapshotLoader:
    """A per-request DataLoader over one pinned snapshot — the N+1 killer (#100).

    The OIDs collected in one resolver tick resolve in a single ``Snapshot.get_many``, ``None``
    where a reference dangles (ADR-003).
    Built with ``cache=False`` and fresh per request, because Strawberry's default lifetime cache
    would serve pre-commit entities across requests and watermarks.
    One loader binds one snapshot, so a traversal reads one watermark (ADR-002).
    """

    __slots__ = ("loader", "snapshot")

    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot

        async def load_fn(oids: list[int]) -> list[EntityView | None]:
            # Read fence (ADR-008 W4-4): a denied row comes back as a redacted twin whose
            # field access raises. Map it to None before Strawberry resolves subfields, so
            # denied, dangling and deleted all resolve to null (no existence leak, no 500).
            views = snapshot.get_many(oids)
            return [None if isinstance(v, Redacted) else v for v in views]

        # cache=False is load-bearing (see the class docstring).
        self.loader: DataLoader[int, EntityView | None] = DataLoader(
            load_fn=load_fn, cache=False
        )

    async def load(self, oid: int) -> EntityView | None:
        """Resolve one OID through the batched loader (``None`` if gone)."""
        return await self.loader.load(oid)


def snapshot_context(snapshot: Snapshot) -> dict[str, Any]:
    """Build a GraphQL ``context`` carrying a per-request :class:`SnapshotLoader` (#100).

    Call it once per request and pass the result as ``context_value`` (or return it from the
    ``context_getter``, #92).
    The dict is the caller's to extend; the resolver reads only :data:`LOADER_CONTEXT_KEY`.
    """
    return {LOADER_CONTEXT_KEY: SnapshotLoader(snapshot)}


def _make_relation_resolver(name: str, *, list_edge: bool) -> Callable[..., object]:
    """Build the async relation resolver for the reference field ``name`` (#100).

    The closure takes exactly ``(root, info)`` and has no return annotation: the field type is
    patched later by ``_resolve_pending``, since the target may not exist yet on a cycle.
    It sends the view's ``Ref`` OID through the per-request loader; a dangling edge is ``null``.
    With ``list_edge`` (#103) the loads are gathered in one tick, coalescing into one ``get_many``.
    """
    if list_edge:

        async def resolve_list(root: object, info: Info[Any, Any]) -> object:
            edges = getattr(root, name)
            if edges is None:
                return []
            loader = _loader_from(info)
            # Gather in one tick so the loads coalesce into one get_many; a bare OID is tolerated.
            return await asyncio.gather(
                *(
                    loader.load(edge.oid if isinstance(edge, Ref) else edge)
                    for edge in edges
                )
            )

        resolve_list.__name__ = f"resolve_{name}"
        resolve_list.__qualname__ = f"_make_relation_resolver.resolve_{name}"
        return resolve_list

    async def resolve(root: object, info: Info[Any, Any]) -> object:
        ref = getattr(root, name)
        if ref is None:
            return None
        loader = _loader_from(info)
        # A bare OID is tolerated next to a Ref token.
        oid = ref.oid if isinstance(ref, Ref) else ref
        return await loader.load(oid)

    resolve.__name__ = f"resolve_{name}"
    resolve.__qualname__ = f"_make_relation_resolver.resolve_{name}"
    return resolve


def _relation_field(name: str, *, list_edge: bool) -> StrawberryField:
    """Build a reference field named ``name`` carrying the async relation resolver.

    Its ``type_annotation`` is a placeholder that :meth:`StrawberryReflector._resolve_pending`
    patches (the cycle break).
    """
    return StrawberryField(
        python_name=name,
        type_annotation=StrawberryAnnotation(object),
        base_resolver=StrawberryResolver(
            _make_relation_resolver(name, list_edge=list_edge)
        ),
    )


def _loader_from(info: Info[Any, Any]) -> SnapshotLoader:
    """Return the per-request :class:`SnapshotLoader`, raising if it was not set up."""
    context: object = info.context
    if isinstance(context, Mapping):
        mapping = cast("Mapping[object, object]", context)
        loader: object = mapping.get(LOADER_CONTEXT_KEY)
    else:
        loader = getattr(context, LOADER_CONTEXT_KEY, None)
    if not isinstance(loader, SnapshotLoader):
        raise RuntimeError(  # noqa: TRY004 - RuntimeError is the documented contract
            "GraphQL relation resolver found no per-request SnapshotLoader on "
            f"info.context[{LOADER_CONTEXT_KEY!r}] — execute the query with "
            "context_value=dc.web.snapshot_context(snapshot) (#100)"
        )
    return loader


def _is_scalar_field(desc: FieldDescriptor) -> bool:
    """Report whether a field is a scalar, optional scalar or list of scalars served off the view.

    It reuses the engine's leaf-set predicates so "scalar" has one definition.
    """
    from datacrystal._entity import (
        _is_indexable,  # engine leaf-set predicate, no second "scalar" definition
    )

    return _is_indexable(desc.core_type) or _is_list_of_scalar(desc.core_type)


class StrawberryReflector:
    """Builds one Strawberry type per ``@entity`` class, cached by typename.

    Strawberry rejects two types with one GraphQL name, and the cache also stops recursion on a
    cycle.
    """

    def __init__(self) -> None:
        self._types: dict[str, type] = {}
        # (field, referenced typename, is_list_edge), patched once both ends exist.
        self._pending_refs: list[tuple[StrawberryField, str, bool]] = []

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
        # The GraphQL name is the bare class name; the persisted typename is ``module:QualName``.
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
        # Cache before recursing so a cycle back to this type ends on the cache hit.
        self._types[info.typename] = gql_type
        for target in referents:
            self.reflect(target)
        return gql_type

    def _field_for(
        self, desc: FieldDescriptor, referents: list[type]
    ) -> StrawberryField | None:
        """Return a Strawberry field for one reflected field, or ``None`` to skip it.

        A reference target is appended to ``referents``, reflected after the current type is cached
        (a cycle would loop otherwise).
        A list edge reflects as ``list[Target | None]`` (#103); ``list_ref_target`` detects it
        because ``referenced_entities`` flattens it like a scalar ref.
        """
        if _is_scalar_field(desc):
            return _plain_field(desc.name, desc.core_type)
        list_target = list_ref_target(desc.core_type)
        if list_target is not None:
            target_info, _ = reflect(list_target)
            field = _relation_field(desc.name, list_edge=True)
            self._pending_refs.append((field, target_info.typename, True))
            referents.append(list_target)
            return field
        targets = referenced_entities(desc.core_type)
        if not targets:
            # No GraphQL shape (bare ``list``, ``dict``, a ``dc.Blob`` field): not reflected.
            return None
        # A union of several entities is out of scope for v1's single-type reflection.
        if len(targets) != 1:
            return None
        target = targets[0]
        target_info, _ = reflect(target)
        field = _relation_field(desc.name, list_edge=False)
        self._pending_refs.append((field, target_info.typename, False))
        referents.append(target)
        return field

    def _resolve_pending(self) -> None:
        """Patch each deferred reference field's annotation to its target type once all are cached.

        Strawberry resolves types lazily, so a patch after ``create_type`` is honoured.
        A scalar edge is ``Target | None``; a list edge (#103) is a non-null ``list[Target |
        None]``.
        """
        still_pending: list[tuple[StrawberryField, str, bool]] = []
        for field, typename, is_list in self._pending_refs:
            target = self._types.get(typename)
            if target is None:
                still_pending.append((field, typename, is_list))
                continue
            annotation = list[target | None] if is_list else target | None
            field.type_annotation = StrawberryAnnotation(annotation)
        self._pending_refs = still_pending


def reflect_strawberry_type(cls: type) -> type:
    """Reflect an ``@entity`` class into a Strawberry GraphQL type (#99).

    Callers reflecting several entities into one schema should share a :class:`StrawberryReflector`,
    so each referent maps to one GraphQL type.
    """
    return StrawberryReflector().reflect(cls)


def _plain_field(name: str, core_type: object) -> StrawberryField:
    """Build a resolver-less Strawberry field of ``core_type`` named ``name``.

    Strawberry's default resolver does ``getattr`` on the parent, a frozen ``EntityView`` here.
    """
    return StrawberryField(
        python_name=name,
        type_annotation=StrawberryAnnotation(core_type),
    )
