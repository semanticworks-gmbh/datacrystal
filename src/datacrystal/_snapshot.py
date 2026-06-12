"""``store.snapshot()``: frozen entity views at a commit watermark.

ADR-001 rider 2, shipped at M3 (KICKOFF): a snapshot is the sanctioned way
for ANY thread to read committed state while the owner keeps writing. It
stands on the storage read view (ADR-002) — a pinned, isolated view of
exactly one durable commit boundary — and exposes records as immutable
:class:`EntityView` DTOs: plain decoded data, never live entities, so
nothing here can violate owner confinement or dirty tracking by design.

Scope honesty (KICKOFF M3): views are *DTO reads* — ``get``/``all``/``root``
at one watermark. The frozen index-bitmap views slot is reserved
(:meth:`Snapshot.index_bitmaps`) and lands with the bitmap indexes at M4.
"""

from __future__ import annotations

import threading
from types import MappingProxyType
from typing import Any, Mapping

from datacrystal._entity import TYPES_BY_NAME, type_info
from datacrystal._errors import DataCrystalError, SchemaMismatchError, StoreClosedError
from datacrystal._records import RefToken, decode_payload
from datacrystal._storage.protocol import StorageReadView


class Ref:
    """An entity reference inside a snapshot — resolve it via
    :meth:`Snapshot.get`. Snapshots never hand out live entities (ADR-001),
    so references stay explicit OID tokens."""

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

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(
                f"{self._typename} snapshot view has no field {name!r}"
            ) from None

    def __setattr__(self, name: str, value: Any) -> None:
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


def _freeze(value: Any) -> Any:
    """Decoded payload value → immutable snapshot value."""
    if isinstance(value, RefToken):
        return Ref(value.oid)
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    return value


class Snapshot:
    """A frozen, thread-safe view of the store at one commit watermark.

    Create via ``store.snapshot()`` — from any thread, even while the owner
    commits (the storage read view pins one durable commit boundary,
    ADR-002). Close promptly (it is a context manager): on the sqlite
    backend an open snapshot holds a WAL read transaction, which blocks
    checkpoint truncation.
    """

    def __init__(self, view: StorageReadView) -> None:
        self._view = view
        boot = view.boot()
        # tid semantics: the watermark this view pins. If a commit's P2 has
        # landed but its P3 has not yet run on the owner, this may be one
        # commit AHEAD of store.last_tid — that commit is durable, the
        # snapshot is honest (ADR-002 consequences).
        self._tid = int(boot.meta.get("next_tid", "1")) - 1
        root_meta = boot.meta.get("root_oid", "")
        self._root_oid: int | None = int(root_meta) if root_meta else None
        self._types = tuple(
            (cid, typename, tuple(fields)) for cid, typename, fields in boot.types
        )
        self._fields_by_cid: dict[int, tuple[str, ...]] = {}
        self._typename_by_cid: dict[int, str] = {}
        self._cids_by_typename: dict[str, list[int]] = {}
        for cid, typename, fields in self._types:
            self._fields_by_cid[cid] = tuple(fields)
            self._typename_by_cid[cid] = typename
            self._cids_by_typename.setdefault(typename, []).append(cid)
        self._lock = threading.Lock()
        self._cache: dict[int, EntityView] = {}
        self._closed = False

    # -- surface ---------------------------------------------------------

    @property
    def tid(self) -> int:
        """The commit watermark this snapshot pins (0 = empty store)."""
        return self._tid

    @property
    def types(self) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
        """The full type lineage at this watermark — ``(cid, typename,
        field names)`` rows, exactly what a COMMIT-DELTA consumer needs to
        bootstrap before applying deltas from ``tid`` onward."""
        return self._types

    @property
    def root(self) -> Any:
        """The committed root value (refs as :class:`Ref`, containers
        frozen), or ``None`` if no root was ever assigned."""
        if self._root_oid is None:
            return None
        return self.get(self._root_oid).value

    def get(self, ref: Ref | int) -> EntityView:
        """Resolve an OID or :class:`Ref` to its :class:`EntityView`."""
        oid = ref.oid if isinstance(ref, Ref) else ref
        with self._lock:
            self._guard()
            view = self._cache.get(oid)
            if view is None:
                rec = self._view.load_many([oid]).get(oid)
                if rec is None:
                    raise DataCrystalError(
                        f"no record for oid {oid} at watermark {self._tid}"
                    )
                view = self._materialize(rec.oid, rec.cid, rec.payload)
            return view

    def all(self, cls_or_typename: type | str) -> list[EntityView]:
        """Every committed entity of one type, across its full lineage
        (old field shapes decode by name, exactly like the live engine)."""
        if isinstance(cls_or_typename, str):
            typename = cls_or_typename
        elif isinstance(cls_or_typename, type):
            typename = type_info(cls_or_typename).typename  # loud if not @entity
        else:
            raise TypeError(
                f"all() takes an @entity class or a typename string, "
                f"got {cls_or_typename!r}"
            )
        out: list[EntityView] = []
        with self._lock:
            self._guard()
            for cid in self._cids_by_typename.get(typename, []):
                for rec in self._view.scan_type(cid):
                    view = self._cache.get(rec.oid)
                    if view is None:
                        view = self._materialize(rec.oid, rec.cid, rec.payload)
                    out.append(view)
        return out

    def index_bitmaps(self) -> Any:
        """Reserved API slot (KICKOFF M3): frozen index-bitmap views."""
        raise NotImplementedError(
            "[planned — M4] frozen index-bitmap views land together with the "
            "pyroaring bitmap indexes (ADR-001 bound decision 4)"
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._view.close()

    def __enter__(self) -> "Snapshot":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"tid={self._tid}"
        return f"<datacrystal.Snapshot {state}>"

    # -- internals ---------------------------------------------------------

    def _guard(self) -> None:
        if self._closed:
            raise StoreClosedError("this snapshot has been closed")

    def _materialize(self, oid: int, cid: int, payload: bytes) -> EntityView:
        """Decode one record into a cached view — by NAME through its own
        persisted shape, missing live fields filled from dataclass defaults
        (the same additive-evolution rules as live hydration)."""
        typename = self._typename_by_cid.get(cid)
        persisted = self._fields_by_cid.get(cid)
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
            # No live class in this process: present the persisted shape.
            for name, value in by_name.items():
                values[name] = _freeze(value)
        else:
            for name in ti.field_names:
                if name in by_name:
                    values[name] = _freeze(by_name[name])
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
        view = EntityView(oid, typename, values)
        self._cache[oid] = view
        return view
