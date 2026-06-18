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
  reflected Strawberry type. Since #100 (S6b) they carry an **async relation
  resolver** (:func:`_relation_resolver`): it reads the raw
  :class:`~datacrystal._snapshot.Ref` token the default resolver would have
  returned and hands the OID to a **per-request DataLoader**, so a query
  following N sibling refs in one resolver tick batches into **one**
  ``Snapshot.get_many`` instead of N+1-ing the store.

The DataLoader (the N+1 killer)
-------------------------------

The relation resolver pulls a :class:`SnapshotLoader` off ``info.context`` (the
key :data:`LOADER_CONTEXT_KEY`) and awaits ``loader.load(oid)``. The loader
is **built per request, not defaulted** (:func:`snapshot_context` /
:meth:`SnapshotLoader.__init__`):

* its ``load_fn`` is :meth:`Snapshot.get_many` over the **one** snapshot pinned
  for that request/operation — the same watermark every field on the request
  reads from (ADR-002 read views);
* it is constructed with ``cache=False``. Strawberry's vendored DataLoader
  defaults to ``cache=True`` (``dataloader.py:139``), a **lifetime** cache that
  would leak resolved entities across requests *and* across snapshot watermarks
  (a stale read after a commit). Request scoping is a property we *build* by
  turning that off and constructing the loader fresh per request — never a
  default we inherit (:func:`test_loader_is_request_scoped_not_lifetime_cached`);
* a tick's ``.load()`` calls coalesce into one batch by Strawberry's
  ``call_soon`` scheduling (``dataloader.py:248``), so the load_fn receives the
  whole sibling-set of OIDs at once;
* a deleted/dangling ``Ref`` rides ``get_many``'s None-on-miss (ADR-003,
  unchecked deletes) — the loader returns ``None`` in that slot and the field
  resolves to GraphQL ``null``, never a 500.

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

import asyncio
from collections.abc import Mapping
from typing import Any, cast

from strawberry.annotation import StrawberryAnnotation
from strawberry.dataloader import DataLoader
from strawberry.tools import create_type
from strawberry.types import Info
from strawberry.types.field import StrawberryField
from strawberry.types.fields.resolver import StrawberryResolver

from datacrystal._entity import (
    TypeInfo,
    _is_list_of_scalar,  # pyright: ignore[reportPrivateUsage]  # engine leaf-set predicate, no second "scalar" definition
)
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

#: The key under which a per-request :class:`SnapshotLoader` lives on the
#: GraphQL ``info.context``. The relation resolver looks it up here; the request
#: wiring (#92) and :func:`snapshot_context` put it there. A module-level
#: constant (not a bare string at the call sites) so the resolver and the
#: context builder can never disagree on the name.
LOADER_CONTEXT_KEY = "dc_snapshot_loader"


class SnapshotLoader:
    """A per-request DataLoader over one pinned snapshot — the N+1 killer (#100).

    Holds a :class:`strawberry.dataloader.DataLoader` whose ``load_fn`` is this
    snapshot's :meth:`~datacrystal._snapshot.Snapshot.get_many` (#94): a batch of
    OIDs collected in one resolver tick resolves in a single
    :meth:`~datacrystal._snapshot.Snapshot.get_many` round-trip, aligned 1:1 and
    ``None``-tolerant on a dangling reference (ADR-003 unchecked deletes).

    **Request-scoped by construction, not by default.** Strawberry's vendored
    DataLoader defaults to ``cache=True`` (``dataloader.py:139``) — a *lifetime*
    cache keyed by OID that would survive across requests and across snapshot
    watermarks, serving a pre-commit entity after a later commit changed it. We
    build the loader with ``cache=False`` and construct one fresh per request
    (:func:`snapshot_context`), so the only thing scoping a batch is the request
    itself. The tick-coalescing that turns N sibling ``.load()`` calls into one
    ``get_many`` is Strawberry's ``call_soon`` batch dispatch (``dataloader.py:248``),
    which ``cache=False`` does not disturb.

    One loader binds one snapshot: every field on the request reads from the same
    committed watermark (ADR-002 read views), so a graph traversal is internally
    consistent even while the owner thread keeps committing.
    """

    __slots__ = ("snapshot", "loader")

    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot

        async def load_fn(oids: list[int]) -> list[EntityView | None]:
            # get_many is sync (a snapshot read view); the DataLoader contract is
            # an async load_fn returning a list aligned 1:1 with the keys —
            # exactly get_many's shape (#94), None where an OID is gone.
            return snapshot.get_many(oids)

        # cache=False is the load-bearing argument (see the class docstring): the
        # default cache=True is a lifetime cache that leaks across watermarks.
        self.loader: DataLoader[int, EntityView | None] = DataLoader(
            load_fn=load_fn, cache=False
        )

    async def load(self, oid: int) -> EntityView | None:
        """Resolve one OID through the batched loader (``None`` if gone)."""
        return await self.loader.load(oid)


def snapshot_context(snapshot: Snapshot) -> dict[str, Any]:
    """Build a fresh GraphQL ``context`` carrying a per-request
    :class:`SnapshotLoader` over ``snapshot`` (#100).

    Call this **once per request/operation** with the snapshot pinned for that
    request, then pass the result as ``context_value`` to ``schema.execute`` /
    ``execute_sync`` (or return it from the FastAPI GraphQL ``context_getter``,
    #92). Building it per request — rather than sharing one loader on the schema —
    is what scopes batching and caching to the request and pins every field to
    one watermark (ADR-002). The dict is the caller's to extend with other
    per-request values; the relation resolver only reads
    :data:`LOADER_CONTEXT_KEY`.
    """
    return {LOADER_CONTEXT_KEY: SnapshotLoader(snapshot)}


def _make_relation_resolver(name: str, *, list_edge: bool) -> Any:
    """Build the async relation resolver for the reference field ``name`` (#100).

    Returned as a closure (the field name captured, never a GraphQL argument) so
    Strawberry sees a resolver of exactly ``(root, info)`` — ``root`` untyped is
    the source :class:`~datacrystal._snapshot.EntityView`, ``info`` annotated
    :class:`strawberry.types.Info` is the context injection. The field's GraphQL
    type comes from the patched ``type_annotation`` (``_resolve_pending``), not
    from this resolver, so the resolver carries no return annotation — which also
    lets a cycle's target type be unbuilt when the closure is made.

    The default resolver would return the view's raw
    :class:`~datacrystal._snapshot.Ref` token (#99); this one takes that token's
    OID to the per-request DataLoader, so a tick's sibling references batch into
    one :meth:`~datacrystal._snapshot.Snapshot.get_many` (the N+1 killer). An
    absent edge (``None``) short-circuits without touching the loader; a
    dangling/deleted reference rides ``get_many``'s None-on-miss (ADR-003) and
    resolves to GraphQL ``null`` — never a 500.

    ``list_edge`` switches on the #30 multi-valued adjacency shape (story #103):
    the view holds a **tuple** of :class:`~datacrystal._snapshot.Ref` tokens, so
    every element's OID is dispatched to the loader and the per-element awaits are
    gathered in the SAME tick — Strawberry's ``call_soon`` coalescing then folds
    them (and every sibling list/scalar edge in the tick) into ONE
    :meth:`~datacrystal._snapshot.Snapshot.get_many`, never one batch per list.
    An empty list resolves to ``[]`` (no loader round-trip); a dangling element
    becomes ``null`` in its slot, the list itself non-null.
    """
    if list_edge:

        async def resolve_list(root: Any, info: Info[Any, Any]) -> Any:
            edges = getattr(root, name)
            if edges is None:
                return []
            loader = _loader_from(info)
            # The view freezes a list edge to a tuple of Ref tokens; tolerate a
            # bare OID defensively. Gather in one tick so the loads coalesce into
            # a single get_many — across this list AND every sibling edge.
            return await asyncio.gather(
                *(
                    loader.load(edge.oid if isinstance(edge, Ref) else edge)
                    for edge in edges
                )
            )

        resolve_list.__name__ = f"resolve_{name}"
        resolve_list.__qualname__ = f"_make_relation_resolver.resolve_{name}"
        return resolve_list

    async def resolve(root: Any, info: Info[Any, Any]) -> Any:
        ref = getattr(root, name)
        if ref is None:
            return None
        loader = _loader_from(info)
        # The view stores edges as Ref tokens; tolerate a bare OID defensively.
        oid = ref.oid if isinstance(ref, Ref) else ref
        return await loader.load(oid)

    resolve.__name__ = f"resolve_{name}"
    resolve.__qualname__ = f"_make_relation_resolver.resolve_{name}"
    return resolve


def _relation_field(name: str, *, list_edge: bool) -> StrawberryField:
    """A reference field named ``name`` carrying the async relation resolver.

    The ``type_annotation`` is a placeholder (``object``) here; it is patched to
    the (nullable, or ``list[...]``) target Strawberry type in
    :meth:`StrawberryReflector._resolve_pending` once every endpoint type is
    cached (the cycle break). ``list_edge`` selects the multi-valued resolver +
    the ``list[Target | None]`` annotation (story #103).
    """
    return StrawberryField(
        python_name=name,
        type_annotation=StrawberryAnnotation(object),
        base_resolver=StrawberryResolver(
            _make_relation_resolver(name, list_edge=list_edge)
        ),
    )


def _loader_from(info: Info[Any, Any]) -> SnapshotLoader:
    """Pull the per-request :class:`SnapshotLoader` off the GraphQL context, or
    fail loudly if the request was not wired with :func:`snapshot_context`.
    """
    context: object = info.context
    if isinstance(context, Mapping):
        mapping = cast("Mapping[object, object]", context)
        loader: object = mapping.get(LOADER_CONTEXT_KEY)
    else:
        loader = getattr(context, LOADER_CONTEXT_KEY, None)
    if not isinstance(loader, SnapshotLoader):
        raise RuntimeError(
            "GraphQL relation resolver found no per-request SnapshotLoader on "
            f"info.context[{LOADER_CONTEXT_KEY!r}] — execute the query with "
            "context_value=dc.web.snapshot_context(snapshot) (#100)"
        )
    return loader


def _is_scalar_field(desc: FieldDescriptor) -> bool:
    """A field GraphQL can serve directly off the frozen view: a scalar, an
    optional scalar, or a ``list`` of scalars (`#13`-style multi-valued field).

    Mirrors the engine's own indexable/list-of-scalar split
    (:func:`datacrystal._entity._is_indexable` / ``_is_list_of_scalar``) so the
    GraphQL scalar set is exactly the leaf set the engine treats as plain data —
    no second definition of "scalar" to drift.
    """
    from datacrystal._entity import (
        _is_indexable,  # pyright: ignore[reportPrivateUsage]  # engine leaf-set predicate, no second "scalar" definition
    )

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
        # (the cycle break): (field, referenced typename, is_list_edge) deferred
        # until resolve(). The list flag (#103) selects ``list[Target | None]``
        # over a plain nullable ``Target`` for a multi-valued adjacency field.
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

        Scalar fields are resolver-less: the default ``getattr`` resolver reads
        them straight off the :class:`~datacrystal._snapshot.EntityView`. A
        reference field instead carries the **async relation resolver** (#100),
        which takes the view's :class:`~datacrystal._snapshot.Ref` token through
        the per-request DataLoader (the N+1 killer) rather than returning it raw.
        A **list-valued** reference (the #30 multi-valued edge) reflects as a
        ``list[Target | None]`` of edges resolved through the same loader (#103) —
        detected via the container origin (``list_ref_target``), since
        ``referenced_entities`` flattens a list edge and a scalar ref to the same
        referent tuple.
        """
        if _is_scalar_field(desc):
            return _plain_field(desc.name, desc.core_type)
        list_target = list_ref_target(desc.core_type)
        if list_target is not None:
            # A list-of-ref adjacency edge (#103): reflect ``list[Target | None]``
            # and resolve every element OID through the per-request DataLoader.
            target_info, _ = reflect(list_target)
            field = _relation_field(desc.name, list_edge=True)
            self._pending_refs.append((field, target_info.typename, True))
            referents.append(list_target)
            return field
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
        # The type annotation is a placeholder here (the target's Strawberry type
        # may not exist yet on a cycle) — patched in ``_resolve_pending`` once
        # every endpoint is cached. The relation resolver (#100) is bound now.
        field = _relation_field(desc.name, list_edge=False)
        self._pending_refs.append((field, target_info.typename, False))
        referents.append(target)
        return field

    def _resolve_pending(self) -> None:
        """Patch each deferred reference field's annotation to its target
        Strawberry type, now that every endpoint type is in the cache.

        Strawberry resolves a field's type lazily at schema-conversion, so the
        annotation set here — after ``create_type`` — is the one the schema
        sees. A scalar edge becomes ``Target | None`` (nullable, so an absent
        reference validates without a non-null violation); a list edge (#103)
        becomes ``list[Target | None]`` — a non-null list (empty → ``[]``) of
        nullable elements (a dangling element resolves to ``null``, ADR-003).
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
    :class:`~datacrystal._snapshot.EntityView` (``_snapshot.py:114``).
    """
    return StrawberryField(
        python_name=name,
        type_annotation=StrawberryAnnotation(core_type),
    )
