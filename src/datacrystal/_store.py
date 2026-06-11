"""The Store facade: open, root, store, commit, query, get, get_many.

Concurrency contract (ADR-001, accepted): a store and its live graph are
**owner-confined** — bound at ``open()`` to the opening thread. Foreign
threads get ``WrongThreadError`` with the escape recipe in the message.

The commit pipeline is structured in the ratified three-phase shape from its
first version — P1 capture+encode on the owner, P2 backend I/O on bytes
only, P3 flip+re-arm+watermark on the owner — even though v0.1 runs all
three synchronously on the owner thread. M2 moves P2 off-thread without
changing the logic (this sequencing IS the crystallization point; see
KICKOFF.md M2). The M1 scaffold to be deleted at M2 is the *scheduling*,
not the phases.

Write model (buffer-until-commit): ``store.store(obj)`` registers an object
graph; mutating a CLEAN entity buffers it via the one-shot hook; nothing
touches storage until ``commit()``. The storer holds strong references to
pending objects, so uncommitted work cannot be garbage-collected.

Known v0.1 limitation (documented, M2 fixes it with PersistentList/Dict +
the fingerprint safety net): in-place mutation of a list/dict *inside* a
CLEAN entity is invisible to the hook — reassign the field or call
``store.mark_dirty(entity)``.
"""

from __future__ import annotations

import threading
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from datacrystal._conditions import Condition
from datacrystal._entity import (
    STATE_CLEAN,
    STATE_DIRTY,
    TYPES_BY_NAME,
    TypeInfo,
    entity,
    is_entity,
    oid_of,
    set_field,
    set_state,
    stamp,
    state_of,
    type_info,
)
from datacrystal._errors import (
    DataCrystalError,
    LeaseLostError,
    NotAnEntityError,
    SchemaMismatchError,
    StoreClosedError,
    UnregisteredTypeError,
    WrongThreadError,
)
from datacrystal._ids import IdAllocator, OID_BASE, TID_BASE
from datacrystal._indexes import IndexManager, plan
from datacrystal._lazy import Lazy
from datacrystal._records import RefToken, decode_payload, encode_payload
from datacrystal._registry import ObjectRegistry
from datacrystal._storage.protocol import CommitBatch, StorageBackend, StoredRecord

_UNSET = object()

_THREAD_RECIPE = (
    "live entities and their store are confined to the thread that opened the "
    "store (ADR-001); read across threads via store.snapshot() and send writes "
    "to the owner via store.submit(fn) (both land in v0.x)"
)


@entity
class _Root:
    """Internal holder so ``store.root`` may be any persistable value."""

    value: Any = None


class Store:
    """An open datacrystal store. Create via :meth:`Store.open`."""

    def __init__(self, backend: StorageBackend, lock: Any | None) -> None:
        self._backend = backend
        self._lock = lock
        self._owner = threading.get_ident()
        self._closed = False
        self._registry = ObjectRegistry()
        self._new: dict[int, Any] = {}
        self._dirty: dict[int, Any] = {}
        self._root_pending: Any = _UNSET

        boot = backend.boot()
        meta = boot.meta
        self._alloc = IdAllocator(
            next_oid=int(meta.get("next_oid", OID_BASE)),
            next_cid=int(meta.get("next_cid", 1)),
            next_tid=int(meta.get("next_tid", TID_BASE)),
        )
        self._last_tid = self._alloc.tid_watermark - 1
        root_meta = meta.get("root_oid", "")
        self._root_oid: int | None = int(root_meta) if root_meta else None
        self._cid_by_typename: dict[str, int] = {}
        self._typename_by_cid: dict[int, str] = {}
        self._persisted_fields: dict[int, list[str]] = {}
        for cid, typename, fields in boot.types:
            self._cid_by_typename[typename] = cid
            self._typename_by_cid[cid] = typename
            self._persisted_fields[cid] = fields
        self._ti_by_cid: dict[int, TypeInfo] = {}
        self._index = IndexManager(backend, self._cid_by_typename.get)

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, *, durability: str = "full",
             lock_ttl: float = 10.0) -> "Store":
        """Open (creating if needed) the store directory at ``path``.

        The directory holds ``data.sqlite`` and the single-writer lease file
        ``used.lock`` — a second concurrent opener fails with
        ``StoreLockedError``.
        """
        from datacrystal._storage.lock import LeaseLock  # sqlite3/locking stay lazy
        from datacrystal._storage.sqlite import SqliteBackend

        directory = Path(path)
        directory.mkdir(parents=True, exist_ok=True)
        lock = LeaseLock(directory / "used.lock", ttl=lock_ttl)
        lock.acquire()
        try:
            backend = SqliteBackend(directory / "data.sqlite", durability=durability)
            return cls(backend, lock)
        except BaseException:
            lock.release()
            raise

    @classmethod
    def _from_backend(cls, backend: StorageBackend) -> "Store":
        """Open over an explicit backend (tests; no lock file)."""
        return cls(backend, None)

    def close(self) -> None:
        """Close the store. Uncommitted changes are discarded (commit first)."""
        if self._closed:
            return
        self._closed = True
        self._backend.close()
        if self._lock is not None:
            self._lock.release()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- public surface ------------------------------------------------------

    @property
    def root(self) -> Any:
        self._guard()
        if self._root_pending is not _UNSET:
            return self._root_pending
        if self._root_oid is None:
            return None
        return self._load_oid(self._root_oid).value

    @root.setter
    def root(self, value: Any) -> None:
        self._guard()
        self._root_pending = value

    @property
    def last_tid(self) -> int:
        """The current commit watermark (0 before the first commit)."""
        return self._last_tid

    def store(self, obj: Any) -> int:
        """Register ``obj`` (and every new entity reachable from it) for the
        next commit; returns its OID."""
        self._guard()
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        return self._register_graph(obj)

    def mark_dirty(self, obj: Any) -> None:
        """Explicitly buffer an entity for the next commit (the escape hatch
        for in-place container mutation, until M2's PersistentList/Dict)."""
        self._guard()
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        if oid_of(obj) is None:
            self._register_graph(obj)
        elif state_of(obj) == STATE_CLEAN:
            set_state(obj, STATE_DIRTY)
            self._dirty[oid_of(obj)] = obj

    def commit(self) -> int | None:
        """Atomically persist all buffered changes; returns the new commit
        TID, or ``None`` if there was nothing to commit."""
        self._guard()
        if self._lock is not None and self._lock.lost:
            raise LeaseLostError(
                "this process lost the single-writer lease (paused too long?); "
                "another process may own the store now — refusing to write"
            )
        # ---- P1 (owner): capture + encode ---------------------------------
        if self._root_pending is not _UNSET:
            self._capture_root()
        self._discover_new_graphs()
        pending = {**self._new, **self._dirty}
        if not pending:
            return None
        # Validate before allocating the TID: a rejected commit must leave
        # the TID sequence gapless (replay determinism).
        index_entries: list[tuple[int, TypeInfo, dict[str, Any]]] = []
        for oid, obj in pending.items():
            ti = type_info(obj)
            relevant = {s.name for s in ti.specs if s.indexed or s.unique}
            if relevant:
                index_entries.append(
                    (oid, ti, {name: getattr(obj, name) for name in relevant})
                )
        self._index.check_unique(index_entries)
        tid = self._alloc.next_tid()
        new_types: list[tuple[int, str, list[str]]] = []
        records: list[StoredRecord] = []
        for oid, obj in pending.items():
            ti = type_info(obj)
            cid = self._cid_for(ti, new_types)
            values = [getattr(obj, name) for name in ti.field_names]
            payload = encode_payload(values, self._oid_for_encode)
            records.append(StoredRecord(oid=oid, cid=cid, tid=tid, payload=payload))
        batch = CommitBatch(
            tid=tid,
            records=records,
            new_types=new_types,
            meta={
                "next_oid": str(self._alloc.oid_watermark),
                "next_cid": str(self._alloc.cid_watermark),
                "next_tid": str(self._alloc.tid_watermark),
                "root_oid": str(self._root_oid) if self._root_oid is not None else "",
            },
        )
        # ---- P2: backend I/O on bytes only (off-thread at M2) --------------
        try:
            self._backend.apply(batch)
        except BaseException:
            self._alloc._next_tid = tid  # the TID was never durable; reuse it
            raise
        # ---- P3 (owner): flip, re-arm, advance the watermark ----------------
        for oid, obj in pending.items():
            set_state(obj, STATE_CLEAN)
            self._registry.add(oid, obj)
        self._index.apply(index_entries)
        self._new.clear()
        self._dirty.clear()
        self._last_tid = tid
        return tid

    def get(self, cls: type, **unique_key: Any) -> Any | None:
        """Look up one entity by a unique secondary key, e.g.
        ``store.get(Mineral, qid="Q43010")``. Returns ``None`` if absent.
        Reflects committed state."""
        self._guard()
        if len(unique_key) != 1:
            raise TypeError("get() takes exactly one unique-field keyword argument")
        ti = type_info(cls)
        (field, value), = unique_key.items()
        spec = next((s for s in ti.specs if s.name == field), None)
        if spec is None or not spec.unique:
            raise QueryErrorFor(cls, field)
        if self._cid_by_typename.get(ti.typename) is None:
            return None
        ci = self._index.ensure(ti)
        oid = ci.unique[field].get(value)
        return None if oid is None else self._load_oid(oid)

    def get_many(self, refs: Iterable[Any]) -> list[Any]:
        """Batch-hydrate a sequence of OIDs / Lazy handles / entities in one
        storage round-trip (SDA delta 5: N+1 is never the user's problem)."""
        self._guard()
        items = list(refs)
        wanted: list[int] = []
        for item in items:
            if isinstance(item, Lazy):
                if not item.loaded:
                    wanted.append(item.oid)  # type: ignore[arg-type]
            elif isinstance(item, int):
                wanted.append(item)
            elif not is_entity(item):
                raise NotAnEntityError(
                    f"get_many() accepts OIDs, Lazy refs and entities, "
                    f"got {type(item).__name__}"
                )
        missing = [oid for oid in wanted if self._registry.get(oid) is None]
        cache = self._backend.load_many(missing) if missing else {}
        out = []
        for item in items:
            if isinstance(item, Lazy):
                out.append(item.get() if item.loaded else self._load_oid(item.oid, cache))
            elif isinstance(item, int):
                out.append(self._load_oid(item, cache))
            else:
                out.append(item)
        return out

    def query(self, cond: Condition) -> list[Any]:
        """Evaluate a Condition over the *committed* state of one entity
        class; returns hydrated entities."""
        self._guard()
        if not isinstance(cond, Condition):
            raise TypeError(
                f"query() takes a Condition (e.g. Cls.field == value), "
                f"got {type(cond).__name__}"
            )
        cls = cond.entity_class()
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            return []  # no committed records of this type yet
        ci = self._index.ensure(ti)
        bitmap, residual = plan(cond, ci)
        oids = list(bitmap) if bitmap is not None else list(ci.extent)
        objs = self.get_many(oids)
        if residual is not None:
            objs = [o for o in objs if residual.evaluate(o)]
        return objs

    # -- engine internals ----------------------------------------------------

    def _guard(self) -> None:
        if self._closed:
            raise StoreClosedError("this store has been closed")
        if threading.get_ident() != self._owner:
            raise WrongThreadError(_THREAD_RECIPE)

    def _on_first_write(self, obj: Any) -> None:
        """First write to a CLEAN entity (called by the one-shot hook,
        BEFORE the mutation lands)."""
        if threading.get_ident() != self._owner:
            raise WrongThreadError(_THREAD_RECIPE)
        if self._closed:
            raise StoreClosedError("this store has been closed")
        set_state(obj, STATE_DIRTY)
        self._dirty[oid_of(obj)] = obj

    def _capture_root(self) -> None:
        if self._root_oid is None:
            holder = _Root(value=self._root_pending)
            self._root_oid = self._register_graph(holder)
        else:
            holder = self._load_oid(self._root_oid)
            holder.value = self._root_pending  # one-shot hook buffers it
            if state_of(holder) != STATE_DIRTY:  # store-less edge: force it
                set_state(holder, STATE_DIRTY)
                self._dirty[self._root_oid] = holder
        self._root_pending = _UNSET

    def _register_graph(self, obj: Any, walked: set[int] | None = None) -> int:
        if walked is None:
            walked = set()
        queue: deque[Any] = deque([obj])
        while queue:
            current = queue.popleft()
            oid = oid_of(current)
            if oid is None:
                oid = self._alloc.next_oid()
                stamp(current, oid, self, state_of(current))
                self._new[oid] = current
            if oid in walked:
                continue
            walked.add(oid)
            if oid in self._new or oid in self._dirty:
                for name in type_info(current).field_names:
                    self._walk_value(getattr(current, name), queue)
        return oid_of(obj)  # type: ignore[return-value]

    def _discover_new_graphs(self) -> None:
        """P1 discovery: dirty/new objects may reference brand-new entities."""
        walked: set[int] = set()
        for obj in [*self._new.values(), *self._dirty.values()]:
            self._register_graph(obj, walked)

    def _walk_value(self, value: Any, queue: deque[Any]) -> None:
        if is_entity(value):
            if oid_of(value) is None or oid_of(value) in self._new or oid_of(value) in self._dirty:
                queue.append(value)
        elif isinstance(value, Lazy):
            target = value.peek()
            if target is not None:
                self._walk_value(target, queue)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._walk_value(item, queue)
        elif isinstance(value, dict):
            for item in value.values():
                self._walk_value(item, queue)

    def _oid_for_encode(self, obj: Any) -> int:
        oid = oid_of(obj)
        if oid is None:
            raise DataCrystalError(
                f"internal error: {type(obj).__name__} escaped P1 discovery"
            )
        return oid

    def _cid_for(self, ti: TypeInfo, new_types: list[tuple[int, str, list[str]]]) -> int:
        cid = self._cid_by_typename.get(ti.typename)
        if cid is None:
            cid = self._alloc.next_cid()
            self._cid_by_typename[ti.typename] = cid
            self._typename_by_cid[cid] = ti.typename
            self._persisted_fields[cid] = list(ti.field_names)
            self._ti_by_cid[cid] = ti
            new_types.append((cid, ti.typename, list(ti.field_names)))
        return cid

    def _ti_for_cid(self, cid: int) -> TypeInfo:
        ti = self._ti_by_cid.get(cid)
        if ti is not None:
            return ti
        typename = self._typename_by_cid.get(cid)
        if typename is None:
            raise DataCrystalError(f"unknown type id {cid} in store")
        ti = TYPES_BY_NAME.get(typename)
        if ti is None:
            raise UnregisteredTypeError(
                f"the store contains records of {typename!r} but no @entity "
                "class with that name is defined in this process — import it "
                "before opening the data"
            )
        persisted = self._persisted_fields.get(cid, [])
        if persisted != list(ti.field_names):
            raise SchemaMismatchError(
                f"{typename}: persisted fields {persisted} != live class fields "
                f"{list(ti.field_names)}; schema evolution lands post-v0.1 — "
                "keep the class shape stable until then"
            )
        self._ti_by_cid[cid] = ti
        return ti

    def _load_oid(self, oid: int, cache: dict[int, StoredRecord] | None = None) -> Any:
        self._guard()
        obj = self._registry.get(oid)
        if obj is not None:
            return obj
        rec = cache.get(oid) if cache else None
        if rec is None:
            rec = self._backend.load_many([oid]).get(oid)
        if rec is None:
            raise DataCrystalError(f"no record for oid {oid} in the store")
        return self._materialize(rec, cache)

    def _materialize(self, rec: StoredRecord, cache: dict[int, StoredRecord] | None) -> Any:
        ti = self._ti_for_cid(rec.cid)
        obj = object.__new__(ti.cls)
        stamp(obj, rec.oid, self, STATE_CLEAN)
        self._registry.add(rec.oid, obj)  # before fills: breaks reference cycles
        values = decode_payload(rec.payload)
        if len(values) != len(ti.field_names):
            raise SchemaMismatchError(
                f"{ti.typename}: record has {len(values)} fields, class has "
                f"{len(ti.field_names)}"
            )
        for spec, value in zip(ti.specs, values):
            set_field(obj, spec.name, self._resolve(value, spec.lazy_refs, cache))
        return obj

    def _resolve(self, value: Any, lazy: bool, cache: dict[int, StoredRecord] | None) -> Any:
        if isinstance(value, RefToken):
            if lazy:
                existing = self._registry.get(value.oid)
                if existing is not None:
                    return Lazy.of(existing)
                return Lazy._unloaded(value.oid, self)
            return self._load_oid(value.oid, cache)
        if isinstance(value, list):
            return [self._resolve(item, lazy, cache) for item in value]
        if isinstance(value, dict):
            return {k: self._resolve(v, lazy, cache) for k, v in value.items()}
        return value

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"tid={self._last_tid}"
        return f"<datacrystal.Store {state}>"


def QueryErrorFor(cls: type, field: str) -> Exception:
    from datacrystal._errors import QueryError

    return QueryError(
        f"{cls.__name__}.{field} is not a Unique field; "
        "get() looks up unique secondary keys only — use query() for the rest"
    )
