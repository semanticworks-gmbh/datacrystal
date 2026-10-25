"""``store.snapshot()``: frozen entity views at a commit watermark.

A snapshot (ADR-001 rider 2) is how any thread reads committed state while the owner writes.
It stands on a pinned storage read view of one durable commit boundary (ADR-002) and exposes
immutable :class:`EntityView` DTOs, never live entities, so owner confinement holds by design.
:meth:`Snapshot.query` and :meth:`Snapshot.count` plan over snapshot-local indexes rebuilt from
that view (invariant 11), never shared with the owner's; ``index_bitmaps`` exposes them frozen.
"""

from __future__ import annotations

import threading
import warnings
from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, BinaryIO, Self, cast

from pyroaring import BitMap64, FrozenBitMap64

from datacrystal._conditions import (
    And,
    Condition,
    Not,
    Or,
    Pred,
    apply_window,
    parse_order_by,
    query_target,
    validate_window,
    window_iter,
)
from datacrystal._entity import TYPES_BY_NAME, TypeInfo, is_entity, oid_of, type_info
from datacrystal._errors import (
    DanglingRefError,
    DataCrystalError,
    QueryError,
    ReadDeniedError,
    SchemaMismatchError,
    StoreClosedError,
    UnseenTypeWarning,
)
from datacrystal._indexes import (
    ClassIndexes,
    QueryPlan,
    build_class_indexes,
    explain_plan,
    harvest_ref_oids,
    plan,
    readable_bitmap,
    windowed_index_order,
)
from datacrystal._lazy import Lazy
from datacrystal._permissions import PERM_LEGACY_FILLS, can_read_row, is_root
from datacrystal._records import BlobToken, RefToken, decode_payload
from datacrystal._redacted import Redacted
from datacrystal._storage.protocol import StorageReadView

if TYPE_CHECKING:
    from datacrystal._actors import Principal

_VIEW_CHUNK = 8192  # records per load_many in snapshot scans (peak-RAM bound)


class Ref:
    """An entity reference inside a snapshot — resolve it via
    :meth:`Snapshot.get`. Snapshots never hand out live entities (ADR-001),
    so references stay explicit OID tokens.
    """

    __slots__ = ("oid",)

    def __init__(self, oid: int) -> None:
        self.oid = oid

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Ref) and other.oid == self.oid

    def __hash__(self) -> int:
        return hash((Ref, self.oid))

    def __repr__(self) -> str:
        return f"dc.Ref({self.oid})"


class EntityView:
    """One entity's committed state as immutable plain data.

    Field access mirrors the live class (``view.name``); entity references
    are :class:`Ref` tokens, lists are tuples, dicts are read-only mappings.
    ``oid``/``typename``/``fields()`` are reserved names — an entity field
    with one of those names is reachable via ``fields()`` only.
    """

    __slots__ = ("_oid", "_typename", "_values")

    _oid: int
    _typename: str
    _values: dict[str, Any]

    def __init__(self, oid: int, typename: str, values: dict[str, Any]) -> None:
        object.__setattr__(self, "_oid", oid)
        object.__setattr__(self, "_typename", typename)
        object.__setattr__(self, "_values", values)

    @property
    def oid(self) -> int:
        return self._oid

    @property
    def typename(self) -> str:
        return self._typename

    def fields(self) -> Mapping[str, Any]:
        return MappingProxyType(self._values)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - dynamic field access, the type is the entity's
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(
                f"{self._typename} snapshot view has no field {name!r}"
            ) from None

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(
            "snapshot views are read-only — mutate live entities on the owner "
            "thread (or ship a closure via store.submit())"
        )

    def __delattr__(self, name: str) -> None:
        raise AttributeError("snapshot views are read-only")

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, EntityView)
            and other._oid == self._oid
            and other._typename == self._typename
            and other._values == self._values
        )

    def __hash__(self) -> int:
        return hash((self._typename, self._oid))

    def __repr__(self) -> str:
        return f"<EntityView {self._typename} oid={self._oid}>"


class RedactedView(Redacted, EntityView):
    """The snapshot-tier redacted twin (ADR-008 R14) of a found-but-denied protected record.

    :meth:`Snapshot.get_many` returns it instead of a leak. It is both an :class:`EntityView` and
    a ``dc.Redacted``, the live twin's marker, so one denial identity spans live and snapshot.
    ``oid`` and ``typename`` stay readable; any data-field access, ``fields()`` or
    ``dc_permissions`` raises :class:`ReadDeniedError`.
    It is field-empty, never committable, and per-principal: two derefs build two twins.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - overrides EntityView.__getattr__
        raise ReadDeniedError(
            f"this {self._typename} snapshot view is redacted for the current "
            "principal (ADR-008 R14): traversal is graceful, reading redacted "
            "data is loud — check isinstance(x, dc.Redacted) before using fields"
        )

    def fields(self) -> Mapping[str, Any]:
        raise ReadDeniedError(
            f"this {self._typename} snapshot view is redacted (ADR-008 R14) — "
            "its fields are withheld from the current principal"
        )

    def __repr__(self) -> str:
        return f"<RedactedView {self._typename} oid={self._oid}>"


def _typename_of(cls_or_typename: type | str) -> str:
    """Resolve the ``all()`` argument to a persisted typename."""
    if isinstance(cls_or_typename, str):
        return cls_or_typename
    # runtime guard: callers may pass non-type/str (test all(42)); annotation advisory
    if isinstance(cls_or_typename, type):
        return type_info(cls_or_typename).typename  # loud if not @entity
    raise TypeError(
        f"all() takes an @entity class or a typename string, "
        f"got {cls_or_typename!r}"
    )


def _redacted_view(oid: int, typename: str) -> RedactedView:
    """Build a field-empty redacted twin for ``oid``, so every data access raises."""
    return RedactedView(oid, typename, {})


def _freeze(value: object) -> object:
    """Turn a decoded payload value into an immutable snapshot value."""
    if isinstance(value, RefToken):
        return Ref(value.oid)
    if isinstance(value, list):
        return tuple(_freeze(item) for item in cast("list[object]", value))
    if isinstance(value, dict):
        return MappingProxyType(
            {k: _freeze(v) for k, v in cast("dict[Any, object]", value).items()}
        )
    return value


def _view_value(value: object) -> object:
    """Map a predicate value onto the snapshot representation: entities and
    Lazy handles become :class:`Ref` tokens, lists the tuples ``_freeze``
    makes of them — so conditions written against live objects evaluate
    against frozen views (the snapshot twin of the store's raw-read
    transform).
    """
    if is_entity(value):
        oid = oid_of(value)
        if oid is None:
            raise QueryError(
                "cannot match an entity that was never stored — it has no OID"
            )
        return Ref(oid)
    if isinstance(value, Lazy):
        handle = cast("Lazy[Any]", value)
        target = handle._peek_unchecked()  # OID only (predicate→Ref)
        if target is not None:
            return _view_value(target)
        if handle.oid is None:
            raise QueryError("cannot match an unloaded Lazy without an OID")
        return Ref(handle.oid)
    if isinstance(value, list):
        return tuple(_view_value(item) for item in cast("list[object]", value))
    if isinstance(value, dict):
        return {
            k: _view_value(v) for k, v in cast("dict[Any, object]", value).items()
        }
    return value


def _order_views(views: list[EntityView], field: str,
                 descending: bool) -> list[EntityView]:
    """Order views by ``field`` for the un-indexed or residual order_by path (#25).

    NULLs go last; ``views`` arrive ascending-OID, which the stable sort keeps as the tiebreak.
    """
    present = [v for v in views if getattr(v, field) is not None]
    absent = [v for v in views if getattr(v, field) is None]
    present.sort(key=lambda v: getattr(v, field), reverse=descending)
    return present + absent


def _view_condition(cond: Condition) -> Condition:
    if isinstance(cond, Pred):
        if cond.op == "in":
            return Pred(cond.cls, cond.field, "in",
                        tuple(_view_value(v) for v in cond.value))
        return Pred(cond.cls, cond.field, cond.op, _view_value(cond.value))
    if isinstance(cond, And):
        return And(tuple(_view_condition(p) for p in cond.parts))
    if isinstance(cond, Or):
        return Or(tuple(_view_condition(p) for p in cond.parts))
    if isinstance(cond, Not):
        return Not(_view_condition(cond.part))
    return cond


class SnapshotIndexes:
    """One class's index bitmaps, frozen at a snapshot's watermark (ADR-001 bound decision 4).

    ``extent`` holds every committed OID of the class across its type lineage,
    ``eq[field][value]`` the OIDs whose indexed ``field`` equals ``value``, and
    ``unique[field][value]`` the OID owning a unique key.
    All of it is immutable and snapshot-local, so any thread may use it while the owner commits.
    """

    __slots__ = ("eq", "extent", "unique")

    def __init__(self, ci: ClassIndexes) -> None:
        self.extent: FrozenBitMap64 = FrozenBitMap64(ci.extent)
        self.eq: Mapping[str, Mapping[Any, FrozenBitMap64]] = MappingProxyType({
            field: MappingProxyType(
                {value: FrozenBitMap64(bm) for value, bm in postings.items()}
            )
            for field, postings in ci.eq.items()
        })
        self.unique: Mapping[str, Mapping[Any, int]] = MappingProxyType({
            field: MappingProxyType(dict(holders))
            for field, holders in ci.unique.items()
        })

    def __repr__(self) -> str:
        return (
            f"<SnapshotIndexes extent={len(self.extent)} "
            f"eq={sorted(self.eq)} unique={sorted(self.unique)}>"
        )


class UnseenTypeLedger:
    """The types a Store and its snapshots already warned about, shared and thread-safe (#201).

    Snapshots may be read from other threads, so the check-and-add runs under one lock.
    """

    def __init__(self) -> None:
        self._warned: set[str] = set()
        self._lock = threading.Lock()

    def first_sight(self, typename: str) -> bool:
        """Record ``typename`` and return True only the first time it is seen."""
        with self._lock:
            if typename in self._warned:
                return False
            self._warned.add(typename)
            return True


class _SnapshotCore:
    """The principal-free shared state of one snapshot watermark (ADR-008 R15, W4 amendment).

    It holds the pinned read view, the type lineage and every expensive derived cache.
    Any number of per-principal :class:`Snapshot` handles share one core, so index builds stay
    O(n) per commit, never O(n) per principal.
    """

    def __init__(self, view: StorageReadView, unseen: UnseenTypeLedger | None = None) -> None:
        self.view = view
        self.unseen = unseen if unseen is not None else UnseenTypeLedger()
        boot = view.boot()
        # tid semantics: the watermark this view pins. If a commit's P2 has
        # landed but its P3 has not yet run on the owner, this may be one
        # commit AHEAD of store.last_tid — that commit is durable, the
        # snapshot is honest (ADR-002 consequences).
        self.tid = int(boot.meta.get("next_tid", "1")) - 1
        root_meta = boot.meta.get("root_oid", "")
        self.root_oid: int | None = int(root_meta) if root_meta else None
        self.types = tuple(
            (cid, typename, tuple(fields)) for cid, typename, fields in boot.types
        )
        self.fields_by_cid: dict[int, tuple[str, ...]] = {}
        self.typename_by_cid: dict[int, str] = {}
        self.cids_by_typename: dict[str, list[int]] = {}
        for cid, typename, fields in self.types:
            self.fields_by_cid[cid] = tuple(fields)
            self.typename_by_cid[cid] = typename
            self.cids_by_typename.setdefault(typename, []).append(cid)
        self.lock = threading.Lock()
        self.cache: dict[int, EntityView] = {}
        self.indexes: dict[type, ClassIndexes] = {}
        self.frozen: dict[type, SnapshotIndexes] = {}
        # Snapshot-local reverse-reference postings (target OID → referrer OIDs),
        # built once from this pinned view on first incoming() — never shared
        # with the owner's live reverse index (invariant 11). None = not built.
        self.reverse: dict[int, BitMap64] | None = None
        self.closed = False

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.view.close()

    def guard(self) -> None:
        if self.closed:
            raise StoreClosedError("this snapshot has been closed")

    # -- shared derived state (caller holds the lock) ----------------------

    def class_indexes(self, ti: TypeInfo) -> ClassIndexes:
        """Return the snapshot-local mutable indexes for one class, the planner's working form."""
        ci = self.indexes.get(ti.cls)
        if ci is None:
            lineage = [
                (cid, list(self.fields_by_cid[cid]))
                for cid in self.cids_by_typename.get(ti.typename, [])
            ]
            ci = build_class_indexes(ti, lineage, self.view.scan_type)
            ci.seal()  # frozen consumer: drop the incremental-update memory
            self.indexes[ti.cls] = ci
        return ci

    def ensure_reverse(self) -> dict[int, BitMap64]:
        """Build the snapshot-local reverse postings once (caller holds the
        lock): scan every committed record in this pinned view, harvest its
        outgoing refs, invert to target OID → referrer OIDs. The frozen-view
        analogue of ``IndexManager.ensure_reverse`` — global (every cid, every
        field), rebuildable, never persisted (invariant 11).
        """
        if self.reverse is not None:
            return self.reverse
        rev: dict[int, BitMap64] = {}
        for cid in self.fields_by_cid:
            for rec in self.view.scan_type(cid):
                for target in harvest_ref_oids(decode_payload(rec.payload)):
                    rev.setdefault(target, BitMap64()).add(rec.oid)
        self.reverse = rev
        return rev

    def no_live_class_fenced(self, typename: str) -> bool:
        """Return True if no live ``@entity`` class exists but the lineage has ``_dc_*`` columns.

        This is the ADR-008 W4-5 fail-closed case: the raw shape would leak protected data and
        label columns, and no live class can post-filter it. Callers fail closed except for root.
        """
        if TYPES_BY_NAME.get(typename) is not None:
            return False
        for cid in self.cids_by_typename.get(typename, ()):
            if "_dc_owner" in self.fields_by_cid.get(cid, ()):
                return True
        return False

    def load_missing(self, oids: list[int], *, tolerant: bool) -> None:
        """Materialize uncached OIDs into ``self.cache`` (caller holds the
        lock), one ``load_many`` per ``_VIEW_CHUNK`` of *misses* — never per
        OID. Intolerant (``tolerant=False``, the index-driven path): every OID
        is known-present (it came from a snapshot-local bitmap), so a missing
        record is internal corruption and raises. Tolerant: a missing OID is
        simply left absent from the cache (deleted/never-committed, ADR-003).
        """
        missing = [oid for oid in oids if oid not in self.cache]
        for start in range(0, len(missing), _VIEW_CHUNK):
            chunk = missing[start:start + _VIEW_CHUNK]
            records = self.view.load_many(chunk)
            for oid in chunk:
                rec = records.get(oid)
                if rec is None:
                    if tolerant:
                        continue
                    raise DataCrystalError(
                        f"internal error: indexed oid {oid} has no record "
                        f"at watermark {self.tid}"
                    )
                self.materialize(rec.oid, rec.cid, rec.payload)

    def views_for(self, oids: list[int]) -> list[EntityView]:
        """Batch-materialize EntityViews for known-present OIDs (caller holds
        the lock); raises on any miss — the internal, index-driven path.
        """
        self.load_missing(oids, tolerant=False)
        return [self.cache[oid] for oid in oids]

    def views_for_tolerant(self, oids: list[int]) -> list[EntityView | None]:
        """Return the views for ``oids``, None for an absent or deleted OID (caller holds the lock).

        The miss-tolerant sibling of :meth:`_views_for`, behind :meth:`Snapshot.get_many` (#94).
        """
        self.load_missing(oids, tolerant=True)
        return [self.cache.get(oid) for oid in oids]

    def warn_unseen(self, ti: TypeInfo) -> None:
        if not self.unseen.first_sight(ti.typename):
            return
        warnings.warn(
            UnseenTypeWarning(
                f"this snapshot has no committed records of {ti.cls.__name__} "
                f"at watermark {self.tid} — the result is empty (first run? "
                "forgot to commit()? opened a different store file?)"
            ),
            stacklevel=4,
        )

    def decode_values(self, cid: int, payload: bytes) -> tuple[str, dict[str, Any]]:
        """Decode one record into ``(typename, frozen-values)`` — by NAME
        through its own persisted shape, missing live fields filled from
        dataclass defaults (the same additive-evolution rules as live
        hydration). Principal-free: the read fence runs at the handle surface
        (per-row on deref, bitmap-intersect on discovery), never here.
        """
        typename = self.typename_by_cid.get(cid)
        persisted = self.fields_by_cid.get(cid)
        if typename is None or persisted is None:
            raise DataCrystalError(f"unknown type id {cid} in store")
        raw = decode_payload(payload)
        if len(raw) != len(persisted):
            raise SchemaMismatchError(
                f"{typename}: record has {len(raw)} fields, its type "
                f"dictionary row has {len(persisted)} — the store is damaged"
            )
        by_name = dict(zip(persisted, raw))
        ti = TYPES_BY_NAME.get(typename)
        values: dict[str, Any] = {}
        if ti is None:
            # No live class in this process: present the persisted shape. A
            # label-bearing shape is fenced at the surface (fail closed for
            # non-root, W4-5) BEFORE this decode is ever returned.
            for name, value in by_name.items():
                values[name] = _freeze(value)
        else:
            for name in ti.field_names:
                if name in by_name:
                    values[name] = _freeze(by_name[name])
                    continue
                if ti.protected and name in PERM_LEGACY_FILLS:
                    # R7 legacy fill (ADR-008) — snapshots do NOT ride the
                    # store's hydration plan, so the special case repeats
                    # here from the same shared constant; a miss would give
                    # web/GraphQL readers different labels than the live path.
                    values[name] = _freeze(PERM_LEGACY_FILLS[name]())
                    continue
                factory = ti.defaults.get(name)
                if factory is None:
                    raise SchemaMismatchError(
                        f"{typename}.{name} does not exist in records persisted "
                        f"with fields {list(persisted)} and has no default — give "
                        "the new field a default value to enable additive "
                        "schema evolution"
                    )
                values[name] = _freeze(factory())
        return typename, values

    def materialize(self, oid: int, cid: int, payload: bytes) -> EntityView:
        typename, values = self.decode_values(cid, payload)
        view = EntityView(oid, typename, values)
        self.cache[oid] = view
        return view


class Snapshot:
    """A frozen, thread-safe view of the store at one commit watermark for ONE principal.

    Create it with ``store.snapshot(principal=...)`` from any thread (ADR-002, ADR-008 R15).
    Discovery surfaces (``query``, ``count``, ``all``, ``explain``, ``incoming``) intersect the
    principal's readable OIDs first; deref surfaces (``get``, ``get_many``, ``open_blob``) check
    ``can_read_row`` per row. Close it promptly: on sqlite it holds a WAL read transaction.
    Un-slotted on purpose (the web DataLoader patches ``get_many``): ADR-008, "Read surfaces".
    """

    def __init__(self, core: _SnapshotCore, principal: Principal) -> None:
        self._core = core
        self._principal = principal
        # Per-principal readable-bitmap cache, keyed by CLASS (never by the
        # unhashable Principal — TRAP 1). One entry per protected class this
        # handle has queried; None means root (skip the intersect).
        self._readable: dict[Any, BitMap64 | None] = {}

    def for_principal(self, principal: Principal) -> Snapshot:
        """Return a sibling handle over the SAME shared core, bound to ``principal`` (ADR-008 R15).

        It costs O(1) and shares every view, index and reverse posting with this handle;
        each handle enforces its own principal's readable set.
        """
        return Snapshot(self._core, principal)

    # -- surface ---------------------------------------------------------

    @property
    def principal(self) -> Principal:
        """The acting principal this handle is bound to (immutable)."""
        return self._principal

    @property
    def tid(self) -> int:
        """The commit watermark this snapshot pins (0 = empty store)."""
        return self._core.tid

    @property
    def types(self) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
        """The type lineage at this watermark as ``(cid, typename, field names)`` rows.

        A COMMIT-DELTA consumer bootstraps from it before applying deltas from ``tid`` onward.
        """
        return self._core.types

    @property
    def root(self) -> Any:  # noqa: ANN401 - public return, the root value is dynamic
        """The committed root value (refs as :class:`Ref`, containers frozen), or ``None``.

        Raises :class:`ReadDeniedError` if the root record is unreadable by this principal.
        """
        if self._core.root_oid is None:
            return None
        return self.get(self._core.root_oid).value

    def get(self, ref: Ref | int) -> EntityView:
        """Resolve an OID or :class:`Ref` to its :class:`EntityView`, the STRICT deref (R14).

        A record this principal cannot read raises :class:`ReadDeniedError`, revealing existence;
        :meth:`get_many` gives the redacted-twin form. Root reads everything.
        Raises ``DanglingRefError`` for a deleted or never-committed OID (ADR-003) and
        ``StoreClosedError`` on a closed snapshot.
        """
        oid = ref.oid if isinstance(ref, Ref) else ref
        core = self._core
        with core.lock:
            core.guard()
            view = core.cache.get(oid)
            if view is None:
                rec = core.view.load_many([oid]).get(oid)
                if rec is None:
                    raise DanglingRefError(
                        f"no record for oid {oid} at watermark {core.tid} — "
                        "deleted (v0.x deletes are unchecked, ADR-003) or "
                        "never committed"
                    )
                view = core.materialize(rec.oid, rec.cid, rec.payload)
        if self._classify(view) == "ok":
            return view
        raise ReadDeniedError(
            f"oid {oid} is not readable by the current principal (ADR-008 R14): "
            "the strict snapshot deref reveals existence — use get_many() for "
            "the redacted-twin / None form"
        )

    def get_many(self, refs: Iterable[EntityView | Ref | int]) -> list[EntityView | None]:
        """Batch-resolve OIDs to views in one round-trip per chunk, aligned with the input (#94).

        It is the deref seam for the ``datacrystal[web]`` DataLoader.
        A missing OID yields ``None`` (ADR-003); a denied protected OID yields a redacted twin
        (ADR-008 R14), as ``lazy_ref.get()`` does; a ``_dc_*`` record without a live class yields
        ``None`` (W4-5). It raises only ``StoreClosedError``.
        """
        oids = [r.oid if isinstance(r, (EntityView, Ref)) else r for r in refs]
        core = self._core
        with core.lock:
            core.guard()
            views = core.views_for_tolerant(oids)
        out: list[EntityView | None] = []
        for oid, view in zip(oids, views):
            if view is None:
                out.append(None)  # truly absent (deleted / never committed)
                continue
            kind = self._classify(view)
            if kind == "ok":
                out.append(view)
            elif kind == "twin":
                out.append(_redacted_view(oid, view.typename))
            else:  # "deny": no live class for a _dc_* record — fail closed
                out.append(None)
        return out

    def open_blob(self, view: EntityView | Ref | int, field: str) -> BinaryIO:
        """Open a committed ``dc.Blob`` field as a binary stream over this pinned view (ADR-007 §3).

        The blob is fenced with its owning record (ADR-008 W4-3): resolving the owner is the
        strict deref, so a denied owner raises :class:`ReadDeniedError` before any byte is read.
        Closing the stream does not close the snapshot.
        Raises ``QueryError`` for an unknown field, ``TypeError`` for a non-blob field and
        ``ValueError`` for a None blob, besides the errors of :meth:`get`.
        """
        core = self._core
        ev = view if isinstance(view, EntityView) else self.get(view)
        # A caller-supplied raw EntityView bypasses get()'s fence — re-check it
        # (zero-cost for an unprotected view: _classify returns "ok" at once).
        if self._classify(ev) != "ok":
            raise ReadDeniedError(
                f"{ev.typename} oid {ev.oid} is not readable by the current "
                "principal — its blobs are fenced with the record (ADR-008 W4-3)"
            )
        fields = ev.fields()
        if field not in fields:
            raise QueryError(
                f"{ev.typename} snapshot view has no field {field!r}"
            )
        value = fields[field]
        if isinstance(value, BlobToken):
            with core.lock:
                core.guard()
                # on_close=None: the stream rides the snapshot's shared view, so
                # closing it must not tear down the snapshot's read transaction.
                return core.view.open_blob_stream(value.blob_oid)
        if value is None:
            raise ValueError(f"{ev.typename}.{field} is None — no blob to open")
        raise TypeError(
            f"{ev.typename}.{field} is not a dc.Blob field — open_blob() streams "
            "out-of-line blob values only"
        )

    def all(self, cls_or_typename: type | str, *, limit: int | None = None,
            offset: int = 0, order_by: object = None) -> list[EntityView]:
        """Return every committed entity of one type across its lineage; old shapes decode by name.

        ``limit``/``offset`` window the result (#14); ``order_by=(field, 'asc'|'desc')`` sorts the
        whole extent first (#25) and needs the live class.
        A protected class is intersected with the readable OIDs before the window (ADR-008 R12).
        A ``_dc_*`` typename without a live class raises ``ReadDeniedError`` for non-root (W4-5).
        """
        validate_window(limit, offset)
        typename = _typename_of(cls_or_typename)
        core = self._core
        ti = TYPES_BY_NAME.get(typename)
        if ti is None and not is_root(self._principal) \
                and core.no_live_class_fenced(typename):
            raise ReadDeniedError(
                f"all({typename!r}) is refused: the persisted lineage carries "
                "_dc_* permission columns but no live @entity class exists to "
                "enforce them — fail closed for a non-root principal (ADR-008 W4-5)"
            )
        if order_by is not None:
            return self._all_ordered(typename, ti, order_by, limit, offset)
        if ti is not None and ti.protected:
            return self._all_protected(ti, limit, offset)
        return self._all_streamed(typename, limit, offset)

    def _all_ordered(self, typename: str, ti: TypeInfo | None, order_by: object,
                     limit: int | None, offset: int) -> list[EntityView]:
        if ti is None:
            raise QueryError(
                f"all(order_by=...) needs the live @entity class for "
                f"{typename!r} to name the sort field"
            )
        core = self._core
        ofield, descending = parse_order_by(order_by, ti)
        with core.lock:
            core.guard()
            ci = core.class_indexes(ti)
            extent = ci.extent
            if ti.protected:
                rb = self._readable_for(ti, ci)
                if rb is not None:
                    extent = ci.extent & rb
            if ofield in ci.eq:
                window = windowed_index_order(ci, extent, ofield,
                                              descending, limit, offset)
                return core.views_for(window)
            views = core.views_for(list(extent))
        return apply_window(_order_views(views, ofield, descending), limit, offset)

    def _all_protected(self, ti: TypeInfo, limit: int | None, offset: int) -> list[EntityView]:
        # protected fast path: route the extent through readable & window,
        # never the raw scan_type stream (a denied row must not pin a slot).
        core = self._core
        with core.lock:
            core.guard()
            ci = core.class_indexes(ti)
            rb = self._readable_for(ti, ci)
            candidate = ci.extent if rb is None else (ci.extent & rb)
            return core.views_for(window_iter(candidate, limit, offset))

    def _all_streamed(self, typename: str, limit: int | None, offset: int) -> list[EntityView]:
        # unprotected / no-live-class-unlabelled: the original streaming fast
        # path — zero cost, no index build, no readable compile.
        core = self._core
        stop = None if limit is None else offset + limit
        out: list[EntityView] = []
        with core.lock:
            core.guard()
            for cid in core.cids_by_typename.get(typename, []):
                for rec in core.view.scan_type(cid):
                    view = core.cache.get(rec.oid)
                    if view is None:
                        view = core.materialize(rec.oid, rec.cid, rec.payload)
                    out.append(view)
                    if stop is not None and len(out) >= stop:
                        return out[offset:]
        return apply_window(out, limit, offset)

    def index_bitmaps(self, cls: type) -> SnapshotIndexes:
        """Return frozen index-bitmap views for ``cls`` at this watermark.

        A ``protected=True`` class is refused for every principal, root included (ADR-008 R12):
        raw postings leak row existence and label structure, and no post-filter of them exists.
        Built on first use by one O(extent) scan of the read view, then cached for the snapshot.
        """
        ti = type_info(cls)  # loud for non-entity classes
        if ti.protected:
            raise ReadDeniedError(
                f"index_bitmaps({cls.__name__}) is refused on a protected class "
                "(ADR-008 R12): raw value-keyed postings leak row existence and "
                "label structure, and no honest post-filter of them exists"
            )
        core = self._core
        if ti.typename not in core.cids_by_typename:
            core.warn_unseen(ti)
        with core.lock:
            core.guard()
            frozen = core.frozen.get(cls)
            if frozen is None:
                frozen = SnapshotIndexes(core.class_indexes(ti))
                core.frozen[cls] = frozen
            return frozen

    def count(self, target: type | Condition) -> int:
        """Count the entities matching ``target`` at this watermark, from snapshot-local bitmaps.

        On a ``protected=True`` class the count covers the readable subset only (ADR-008 R12).
        """
        cls, cond = query_target(target, "count")
        ti = type_info(cls)
        core = self._core
        if ti.typename not in core.cids_by_typename:
            core.warn_unseen(ti)
            return 0
        with core.lock:
            core.guard()
            ci = core.class_indexes(ti)
            if cond is None:
                if ti.protected:
                    rb = self._readable_for(ti, ci)
                    return len(ci.extent) if rb is None else len(rb)
                return len(ci.extent)
            bitmap, residual = plan(cond, ci)
            candidate = bitmap if bitmap is not None else ci.extent
            if ti.protected:
                rb = self._readable_for(ti, ci)
                if rb is not None:
                    candidate = candidate & rb  # a NEW bitmap — never mutate ci.extent
            if residual is None:
                return len(candidate)
            oids = list(candidate)
            view_cond = _view_condition(residual)
            return sum(
                1 for view in core.views_for(oids) if view_cond.evaluate(view)
            )

    def query(self, target: type | Condition, *, limit: int | None = None,
              offset: int = 0, order_by: object = None) -> list[EntityView]:
        """Return the :class:`EntityView` DTOs matching a Condition, or the full extent of a class.

        On a ``protected=True`` class the candidates are intersected with the readable OIDs
        before any window, order or hydration, so a denied row is never hydrated (ADR-008 R12).
        Raises ``NotAnEntityError`` or ``TypeError`` for a bad ``target``, ``ValueError`` for a
        negative window and ``QueryError`` for a bad ``order_by``.
        """
        validate_window(limit, offset)
        cls, cond = query_target(target, "query")
        ti = type_info(cls)
        core = self._core
        if ti.typename not in core.cids_by_typename:
            core.warn_unseen(ti)
            return []
        order = parse_order_by(order_by, ti) if order_by is not None else None
        with core.lock:
            core.guard()
            ci = core.class_indexes(ti)
            if cond is None:
                bitmap, residual = None, None
            else:
                bitmap, residual = plan(cond, ci)
            candidate = bitmap if bitmap is not None else ci.extent
            if ti.protected:
                rb = self._readable_for(ti, ci)
                if rb is not None:
                    candidate = candidate & rb  # a NEW bitmap — never mutate ci.extent
            if order is not None:
                ofield, descending = order
                if residual is None and ofield in ci.eq:
                    window = windowed_index_order(ci, candidate, ofield,
                                                  descending, limit, offset)
                    return core.views_for(window)
                views = core.views_for(list(candidate))
            else:
                # #51: no residual → window lazily; a residual needs all candidates
                oids = (window_iter(candidate, limit, offset) if residual is None
                        else list(candidate))
                views = core.views_for(oids)
        if order is not None:
            ofield, descending = order
            if residual is not None:
                view_cond = _view_condition(residual)
                views = [view for view in views if view_cond.evaluate(view)]
            return apply_window(_order_views(views, ofield, descending), limit, offset)
        if residual is None:
            return views
        view_cond = _view_condition(residual)
        matched = [view for view in views if view_cond.evaluate(view)]
        return apply_window(matched, limit, offset)

    def explain(self, target: type | Condition) -> QueryPlan:
        """Return the deterministic plan for ``target`` over this snapshot's indexes.

        On a ``protected=True`` class the reported ``candidates`` and ``extent`` counts are
        filtered to the readable subset (ADR-008 R12); the plan itself is untouched.
        """
        cls, cond = query_target(target, "explain")
        ti = type_info(cls)
        core = self._core
        if ti.typename not in core.cids_by_typename:
            core.warn_unseen(ti)
            return QueryPlan(
                ti.typename, None if cond is None else repr(cond),
                False, None, 0, 0,
            )
        with core.lock:
            core.guard()
            ci = core.class_indexes(ti)
            readable = self._readable_for(ti, ci) if ti.protected else None
            return explain_plan(ti.typename, ci, cond, readable=readable)

    def incoming(self, target: EntityView | Ref | int) -> list[EntityView]:
        """Return every committed entity that **references** ``target``, as :meth:`Store.incoming`.

        The reverse index is class-blind, so this is a discovery surface (ADR-008 R12): a referrer
        the principal cannot read, or a ``_dc_*`` one without a live class (W4-5), is dropped.
        A ``target`` with no record yields an empty list.
        """
        oid = target.oid if isinstance(target, (EntityView, Ref)) else target
        core = self._core
        with core.lock:
            core.guard()
            referrers = core.ensure_reverse().get(oid)
            if referrers is None:
                return []
            views = core.views_for(list(referrers))
        # discovery filters: keep only readable referrers (drop twin/deny).
        return [view for view in views if self._classify(view) == "ok"]

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the shared core (idempotent). Every sibling handle over the
        same core observes the close.
        """
        self._core.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        core = self._core
        state = "closed" if core.closed else f"tid={core.tid}"
        return f"<datacrystal.Snapshot {state} uid={self._principal.uid}>"

    # -- internals ---------------------------------------------------------

    def _readable_for(self, ti: TypeInfo, ci: ClassIndexes) -> BitMap64 | None:
        """Return this handle's readable-OID bitmap for a PROTECTED class, None for root.

        Callers guard on ``ti.protected`` (zero-cost invariant). It is compiled once per class and
        cached by class, never by the unhashable Principal (TRAP 1).
        """
        key = ti.cls
        cache = self._readable
        if key in cache:
            return cache[key]
        rb = readable_bitmap(self._principal, ci)
        cache[key] = rb
        return rb

    def _classify(self, view: EntityView) -> str:
        """Return this principal's deref verdict for a view: ``"ok"``, ``"twin"`` or ``"deny"``.

        It runs on every deref, cache hit or miss (TRAP 2), so a view materialized for one
        principal never leaks to a sibling handle.
        ``"twin"`` is a denied protected record with a live class; ``"deny"`` one without a live
        class, which fails closed (W4-5).
        """
        ti = TYPES_BY_NAME.get(view.typename)
        if ti is None:
            if "_dc_owner" not in view.fields():
                return "ok"  # unprotected legacy shape, no label columns
            return "ok" if is_root(self._principal) else "deny"
        if not ti.protected:
            return "ok"  # zero cost — no readable/can_read_row call
        if is_root(self._principal):
            return "ok"
        v = view.fields()
        if can_read_row(self._principal, v["_dc_owner"], v["_dc_groups"],
                        v["_dc_read_floor"]):
            return "ok"
        return "twin"

    def _stream(self, typename: str) -> Iterator[tuple[int, dict[str, Any]]]:
        """Yield ``(oid, field-values)`` of a type without filling the cache (#16).

        It is the bounded-memory bootstrap scan for the fts, arrow and deltalog mirrors.
        It fails closed (ADR-008 W4-5): a protected or label-bearing typename under a non-root
        principal raises ``ReadDeniedError``, because a silently partial mirror is worse.
        """
        core = self._core
        ti = TYPES_BY_NAME.get(typename)
        protected = (ti is not None and ti.protected) \
            or core.no_live_class_fenced(typename)
        if protected and not is_root(self._principal):
            raise ReadDeniedError(
                f"_stream({typename!r}) is refused for a non-root principal "
                "(ADR-008 W4-5): a mirror bootstrap over a protected type would "
                "be a silently partial mirror — fail closed instead"
            )
        with core.lock:
            core.guard()
            for cid in core.cids_by_typename.get(typename, []):
                for rec in core.view.scan_type(cid):
                    _, values = core.decode_values(rec.cid, rec.payload)
                    yield rec.oid, values
