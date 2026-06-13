"""The Store facade: open, root, store, commit, query, get, get_many.

Concurrency contract (ADR-001, accepted): a store and its live graph are
**owner-confined** — bound at ``open()`` to the opening thread. Foreign
threads get ``WrongThreadError`` with the escape recipe in the message.

The commit pipeline is the ratified three-phase machine (ADR-001 bound
decision 2): P1 captures, encodes, FLIPS the captured set back to CLEAN and
re-arms the hooks — all await-free on the owner, so a write racing P2
re-dirties and lands in the next commit; P2 applies the batch (bytes only)
on the store's single IO worker thread; P3 finalizes indexes and the
watermark on the owner. A failed P2 compensates: captured entities return
to their buffers (unless a racing write already re-buffered them) and the
TID is reused — a rejected commit leaves the sequence gapless. The
synchronous ``commit()`` blocks until P3; ``aopen()``'s commit awaits P2 so
the event loop stays free.

Write model (buffer-until-commit): ``store.store(obj)`` registers an object
graph; mutating a CLEAN entity buffers it via the one-shot hook; in-place
mutation of a list/dict field buffers its owner via the owner-bound
persistent containers (``_containers.py``); nothing touches storage until
``commit()``. The storer holds strong references to pending objects, so
uncommitted work cannot be garbage-collected; the root holder is pinned so
the root graph keeps stable identity.
"""

from __future__ import annotations

import threading
import warnings
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, cast

from datacrystal._conditions import (
    And,
    Condition,
    Not,
    Or,
    Pred,
    apply_window,
    query_target,
    validate_window,
)
from datacrystal._containers import PersistentDict, PersistentList, wrap_value
from datacrystal._entity import (
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
from datacrystal._state import STATE_CLEAN, STATE_DELETED, STATE_DIRTY, STATE_NEW
from datacrystal._errors import (
    ConsumerDetachedWarning,
    DanglingRefError,
    DataCrystalError,
    DeletedEntityError,
    EntityEscapeError,
    LeaseLostError,
    NotAnEntityError,
    QueryError,
    SchemaMismatchError,
    StoreClosedError,
    UniqueViolationError,
    UnregisteredTypeError,
    UnseenTypeWarning,
    UntrackedMutationWarning,
    WrongThreadError,
)
from datacrystal._ids import FORMAT_VERSION, IdAllocator, OID_BASE, TID_BASE
from datacrystal._indexes import IndexManager, QueryPlan, explain_plan, plan
from datacrystal._lazy import Lazy, LazyReferenceManager
from datacrystal._pipeline import DeltaConsumer, build_delta
from datacrystal._records import RefToken, crc as _crc, decode_payload, encode_payload
from datacrystal._registry import ObjectRegistry
from datacrystal._snapshot import Ref, Snapshot
from datacrystal._storage.protocol import CommitBatch, StorageBackend, StoredRecord
from datacrystal.contract.applier import DeltaGapError

# Exact-type membership for the hydration fast path (decode produces exact
# builtin types, never subclasses — subclass-shaped values still take _resolve).
_SCALAR_TYPES = frozenset((str, int, float, bool, bytes))

_THREAD_RECIPE = (
    "live entities and their store are confined to the thread that opened the "
    "store (ADR-001); send work to the owner via store.submit(fn) — the owner "
    "runs it at its next store call or store.run_pending() — and return plain "
    "data, never live entities; for cross-thread reads take a store.snapshot()"
)


@entity
class _Root:
    """Internal holder so ``store.root`` may be any persistable value."""

    value: Any = None


class _Capture:
    """Everything P1 hands to P2/P3 — and enough to compensate a failed P2."""

    __slots__ = ("tid", "batch", "index_entries", "flipped", "delta", "deletes")

    def __init__(self, tid: int, batch: CommitBatch,
                 index_entries: list[tuple[int, TypeInfo, dict[str, Any]]],
                 flipped: list[tuple[int, Any, int]],
                 delta: dict[str, Any] | None,
                 deletes: list[tuple[int, TypeInfo, Any | None]]) -> None:
        self.tid = tid
        self.batch = batch
        self.index_entries = index_entries
        self.flipped = flipped  # (oid, obj, state before the P1 flip)
        self.delta = delta  # COMMIT-DELTA-v1 map; built only when consumers watch
        self.deletes = deletes  # (oid, ti, live instance or None) — ADR-003


class Store:
    """An open datacrystal store. Create via :meth:`Store.open`."""

    def __init__(self, backend: StorageBackend, lock: Any | None, *,
                 p2_inline: bool = False, debug: bool = False,
                 lazy_timeout: float | None = None,
                 lazy_clock: Callable[[], float] | None = None) -> None:
        self._backend = backend
        self._lock = lock
        self._owner = threading.get_ident()
        self._closed = False
        self._registry = ObjectRegistry()
        self._new: dict[int, Any] = {}
        self._dirty: dict[int, Any] = {}
        # delete() buffer (ADR-003): oid → (TypeInfo, live instance or None).
        # The strong reference keeps the doomed instance alive until P3 so a
        # pre-commit read cannot rehydrate a mutable CLEAN twin beside it.
        self._deleted: dict[int, tuple[TypeInfo, Any | None]] = {}
        # upsert()'s same-batch memory: (cls, key field, value) → the entity
        # an earlier upsert buffered. Lives across a failed P2 (rollback
        # re-buffers those entities); P3 clears it — from then on the
        # committed unique map answers.
        self._pending_upserts: dict[tuple[type, str, Any], Any] = {}
        # P2 runs on this single-worker executor (created on first commit).
        # p2_inline is the fallback for sqlite3 builds that are not
        # serialized (threadsafety < 3) — same phases, owner-thread I/O.
        self._io: ThreadPoolExecutor | None = None
        self._p2_inline = p2_inline
        # submit() queue: foreign threads append, the owner drains at its
        # API boundaries (sync piggyback) or when woken (async). deque
        # append/popleft are atomic — no extra lock needed.
        self._submitted: deque[tuple[Callable[[], Any], Future[Any]]] = deque()
        self._pumping = False
        self._wake: Callable[[], None] | None = None  # set by aopen()
        # debug=True: the msgspec-fingerprint safety net (KICKOFF M2 risk-1
        # mitigation). Every hydration/commit records crc(payload) per OID;
        # every commit re-encodes the live CLEAN entities and warns + commits
        # any that changed without a hook firing. Trades O(live set) commit
        # work and one int per live entity for detection — development tool.
        self._debug = debug
        self._fingerprints: dict[int, int] = {}
        # lazy_timeout=None means no demotion (the default): root pinning
        # plus explicit Lazy cut points already bound memory; the manager
        # adds *time*-based release on top (timeout-only in v0.1).
        # lazy_clock is the injectable test clock (never sleep in tests).
        self._lazyman: LazyReferenceManager | None = None
        if lazy_timeout is not None:
            self._lazyman = (
                LazyReferenceManager(lazy_timeout, lazy_clock)
                if lazy_clock is not None
                else LazyReferenceManager(lazy_timeout)
            )
        # The root holder is PINNED (strong reference): everything reachable
        # from store.root stays live — without this, a CLEAN root graph with
        # no user references would be collected and silently rehydrated,
        # losing identity and any in-place mutations. Lazy[T] is the explicit
        # cut point where pinning (and memory) stops.
        self._root_holder: Any = None
        # COMMIT-DELTA-v1 consumers (ROADMAP item 3). Commits build and
        # deliver deltas only while this list is non-empty — an unwatched
        # store pays nothing for the pipeline (spec §5).
        self._consumers: list[DeltaConsumer] = []

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
        # Type lineage (additive schema evolution): one typename may own
        # SEVERAL cids — one per field shape it ever had. New commits use the
        # latest cid; every old cid stays decodable through its own persisted
        # field list (records are hydrated by NAME, missing fields filled
        # from dataclass defaults, removed fields ignored).
        self._cid_by_typename: dict[str, int] = {}          # typename → latest
        self._cids_by_typename: dict[str, list[int]] = {}   # full lineage
        self._typename_by_cid: dict[int, str] = {}
        self._persisted_fields: dict[int, list[str]] = {}
        # cids whose types-table row is durably persisted. A commit batch
        # re-includes every non-durable lineage row, so a commit that failed
        # in P2 cannot leave later records pointing at a cid the store never
        # learned about.
        self._durable_cids: set[int] = set()
        for cid, typename, fields in boot.types:  # ordered by cid: last wins
            self._cid_by_typename[typename] = cid
            self._cids_by_typename.setdefault(typename, []).append(cid)
            self._typename_by_cid[cid] = typename
            self._persisted_fields[cid] = fields
            self._durable_cids.add(cid)
        self._ti_by_cid: dict[int, TypeInfo] = {}
        self._plan_by_cid: dict[int, list[tuple[Any, int | None, Any]]] = {}
        self._index = IndexManager(backend, self._lineage_for)

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, *, durability: str = "interval",
             lock_ttl: float = 10.0, debug: bool = False,
             lazy_timeout: float | None = None) -> "Store":
        """Open (creating if needed) the store directory at ``path``.

        The directory holds ``data.sqlite`` and the single-writer lease file
        ``used.lock`` — a second concurrent opener fails with
        ``StoreLockedError``.

        ``durability`` is the fsync triad (KICKOFF M2): ``"commit"`` fsyncs
        every commit (power-loss durable), ``"interval"`` (default) group-
        commits at WAL checkpoints (process crash loses nothing; OS crash
        may lose the last commits, never corrupts), ``"never"`` is for
        benchmarks and scratch stores only.

        ``debug=True`` arms the fingerprint safety net: every commit
        re-encodes the live CLEAN entities and raises an
        ``UntrackedMutationWarning`` for (and commits) any that changed
        without the dirty-tracking hook firing. Development tool — it costs
        O(live entities) per commit.

        ``lazy_timeout`` (seconds) enables the LazyReferenceManager: loaded
        ``Lazy`` handles idle past the timeout demote back to unloaded,
        releasing the subgraph behind the cut point. Demotion runs only on
        the owner (sweeps piggyback on your store calls; under ``aopen()``
        an owner-loop task sweeps). Timeout-only in v0.1.
        """
        import sqlite3  # noqa: PLC0415 — stays lazy (dep-budget fitness #3)

        from datacrystal._storage.lock import LeaseLock  # sqlite3/locking stay lazy
        from datacrystal._storage.sqlite import SqliteBackend

        directory = Path(path)
        directory.mkdir(parents=True, exist_ok=True)
        lock = LeaseLock(directory / "used.lock", ttl=lock_ttl)
        lock.acquire()
        try:
            backend = SqliteBackend(directory / "data.sqlite", durability=durability)
            # Off-thread P2 shares the connection with owner-thread reads;
            # that requires a serialized sqlite3 build (CPython's default).
            return cls(backend, lock, p2_inline=sqlite3.threadsafety < 3,
                       debug=debug, lazy_timeout=lazy_timeout)
        except BaseException:
            lock.release()
            raise

    @classmethod
    def _from_backend(cls, backend: StorageBackend, *, debug: bool = False,
                      lazy_timeout: float | None = None,
                      lazy_clock: Callable[[], float] | None = None) -> "Store":
        """Open over an explicit backend (tests; no lock file)."""
        return cls(backend, None, debug=debug, lazy_timeout=lazy_timeout,
                   lazy_clock=lazy_clock)

    def close(self) -> None:
        """Close the store. Uncommitted changes are discarded (commit first)."""
        if self._closed:
            return
        self._closed = True
        self._root_holder = None  # unpin: let the graph be collected
        while True:  # pending submissions can never run now — fail them loudly
            try:
                _fn, future = self._submitted.popleft()
            except IndexError:
                break
            if future.set_running_or_notify_cancel():
                future.set_exception(
                    StoreClosedError("the store closed before this submission ran")
                )
        if self._io is not None:
            self._io.shutdown(wait=True)
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
        self._enter()
        if self._root_oid is None:
            return None
        if self._root_holder is None:
            try:
                self._root_holder = self._load_oid(self._root_oid)
            except DanglingRefError as exc:
                raise DanglingRefError(
                    f"{exc} — the root graph references a deleted entity "
                    "(ADR-003 unchecked deletes); assigning store.root "
                    "replaces the root and recovers the store"
                ) from None
        return self._root_holder.value

    @root.setter
    def root(self, value: Any) -> None:
        """Assigning the root captures it immediately: lists/dicts come back
        from ``store.root`` as tracked persistent containers (mutate them in
        place — ``commit()`` sees it), and new entities in the value are
        registered for the next commit."""
        self._enter()
        if self._root_oid is None:
            holder = _Root(value=value)
            self._root_oid = self._register_graph(holder)
        else:
            holder = self._root_holder
            if holder is None:
                try:
                    holder = self._load_oid(self._root_oid)
                except DanglingRefError:
                    # The old root graph references a deleted entity (ADR-003
                    # unchecked deletes). Recovery path: replace the holder
                    # and delete its now-orphaned record in the same commit.
                    self._deleted[self._root_oid] = (type_info(_Root), None)
                    holder = _Root(value=value)
                    self._root_oid = self._register_graph(holder)
                    self._root_holder = holder
                    return
            holder.value = value  # one-shot hook buffers the holder
            self._register_graph(holder)
        self._root_holder = holder

    @property
    def last_tid(self) -> int:
        """The current commit watermark (0 before the first commit)."""
        return self._last_tid

    def store(self, obj: Any) -> int:
        """Register ``obj`` (and every new entity reachable from it) for the
        next commit; returns its OID."""
        self._enter()
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        return self._register_graph(obj)

    def mark_dirty(self, obj: Any) -> None:
        """Explicitly buffer an entity for the next commit. Rarely needed —
        attribute writes and in-place container mutation are tracked
        automatically; this is the escape hatch for anything exotic."""
        self._enter()
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        if state_of(obj) == STATE_DELETED:
            raise DeletedEntityError(
                f"this {type(obj).__name__} was deleted via store.delete(); "
                "create a new entity instead — OIDs are never reused"
            )
        oid = oid_of(obj)
        if oid is None:
            self._register_graph(obj)
        elif state_of(obj) == STATE_CLEAN:
            set_state(obj, STATE_DIRTY)
            self._dirty[oid] = obj

    def delete(self, obj_or_cls: Any, /, **unique_key: Any) -> bool:
        """Delete an entity's record at the next ``commit()`` (ADR-003).

        Two call shapes::

            store.delete(mineral)                      # by live instance
            store.delete(Mineral, qid="Q43010")        # by unique key, no hydration

        Returns ``True`` if a deletion was buffered, ``False`` if there was
        nothing to delete (unknown key, already deleted) — idempotent, never
        raises for a miss. Deleting a NEW (never-committed) entity cancels
        its pending insert. A buffered delete **wins** over any buffered
        write to the same OID in the same commit.

        Deletes are *unchecked* in v0.x: nothing stops you deleting an
        entity other records still reference — following such a stale
        reference raises :class:`~datacrystal._errors.DanglingRefError`.
        Checked deletes (cascade/orphan validation) arrive with the v1
        reverse-reference index. After the commit, a live instance you still
        hold is a detached plain object: reads work, writes raise
        :class:`~datacrystal._errors.DeletedEntityError`.
        """
        self._enter()
        if isinstance(obj_or_cls, type):
            ti = type_info(obj_or_cls)  # loud for non-entity classes
            if len(unique_key) != 1:
                raise TypeError(
                    "delete(EntityClass, ...) takes exactly one unique-field "
                    "keyword argument"
                )
            (field, value), = unique_key.items()
            spec = ti.spec(field)
            if spec is None or not spec.unique:
                raise QueryErrorFor(obj_or_cls, field)
            if self._cid_by_typename.get(ti.typename) is None:
                return False
            ci = self._index.ensure(ti)
            oid = ci.unique[field].get(value)
            if oid is None or oid in self._deleted:
                return False
            return self._delete_oid(self._registry.get(oid), oid, ti)
        if unique_key:
            raise TypeError(
                "delete(entity) takes no keyword arguments — use "
                "delete(EntityClass, field=value) for key-based deletion"
            )
        obj = obj_or_cls
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        oid = oid_of(obj)
        if oid is None:
            return False  # never registered with a store — nothing to delete
        if state_of(obj) == STATE_DELETED:
            return False  # idempotent: delete-twice ≡ delete-once
        owner = object.__getattribute__(obj, "__dc_store__")()
        if owner is not self:
            raise DataCrystalError(
                f"this {type(obj).__name__} belongs to a different (or "
                "closed) store"
            )
        return self._delete_oid(obj, oid, type_info(obj))

    def _delete_oid(self, obj: Any | None, oid: int, ti: TypeInfo) -> bool:
        if oid == self._root_oid:
            raise DataCrystalError(
                "the root holder cannot be deleted — assign store.root instead"
            )
        if obj is not None and state_of(obj) == STATE_NEW:
            # Cancel the pending insert: no record ever existed, so nothing
            # reaches storage, the indexes, or the delta stream.
            self._new.pop(oid, None)
            self._dirty.pop(oid, None)
            set_state(obj, STATE_DELETED)
            return True
        self._dirty.pop(oid, None)  # ADR-003 precedence: the delete wins
        if obj is not None:
            set_state(obj, STATE_DELETED)
        self._deleted[oid] = (ti, obj)
        return True

    def upsert(self, obj: Any, /, key: str | None = None) -> Any:
        """Insert ``obj``, or merge it into the entity that already owns its
        natural key — the KICKOFF M4 upsert-by-natural-key, ETL-loop shaped::

            for row in feed:
                store.upsert(Mineral(qid=row["qid"], name=row["name"]))
            store.commit()

        ``key`` names a ``dc.Unique`` field and may be omitted when the
        class has exactly one. On a match the existing live instance is the
        survivor (identity is never broken): every persisted field is
        overwritten with ``obj``'s value, but only fields that actually
        *changed* are written — re-importing an unchanged dataset buffers
        nothing and the commit is O(changed), not O(rows). Returns the
        canonical instance (the survivor, or ``obj`` itself when its key
        was unseen).

        Matching covers committed state plus entities buffered by earlier
        ``upsert`` calls in the same batch (so one batch may see the same
        key twice). Entities buffered via plain ``store()`` are not matched
        — a duplicate there stays what it always was: a loud
        ``UniqueViolationError`` from ``commit()``.
        """
        self._enter()
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        ti = type_info(obj)
        unique_fields = [s.name for s in ti.specs if s.unique]
        if key is None:
            if len(unique_fields) != 1:
                raise TypeError(
                    f"{type(obj).__name__} has {len(unique_fields)} Unique "
                    "fields — pass key=<field name> to choose the natural key"
                    if unique_fields else
                    f"{type(obj).__name__} has no Unique field — upsert needs "
                    "a natural key (mark one field Annotated[..., dc.Unique])"
                )
            key = unique_fields[0]
        elif key not in unique_fields:
            raise QueryErrorFor(cast("type[object]", type(obj)), key)
        value = getattr(obj, key)
        if value is None:
            raise QueryError(
                f"{type(obj).__name__}.{key} is None — a natural key must "
                "have a value (None never matches, SQL-style)"
            )
        existing = self._find_by_key(ti, key, value)
        if existing is None:
            self.store(obj)
            self._pending_upserts[(ti.cls, key, value)] = obj
            return obj
        if existing is obj:
            return obj
        if oid_of(obj) is not None:
            raise UniqueViolationError(
                f"{type(obj).__name__}.{key}={value!r} already belongs to "
                "another entity, and the given instance is itself registered "
                "with the store — upsert fresh (untracked) instances or the "
                "canonical instance"
            )
        for name in ti.field_names:
            new = getattr(obj, name)
            cur = getattr(existing, name)
            if not _equivalent(cur, new):
                setattr(existing, name, new)  # the one-shot hook buffers it
        return existing

    def _find_by_key(self, ti: TypeInfo, field: str, value: Any) -> Any | None:
        """The upsert lookup: committed unique map (a key freed by a
        buffered delete is reusable, ADR-003), then earlier upserts of this
        batch — self-healing against mid-batch key mutation or deletion."""
        if self._cid_by_typename.get(ti.typename) is not None:
            ci = self._index.ensure(ti)
            oid = ci.unique[field].get(value)
            if oid is not None and oid not in self._deleted:
                return self._load_oid(oid)
        pending = self._pending_upserts.get((ti.cls, field, value))
        if pending is not None:
            if (state_of(pending) != STATE_DELETED
                    and getattr(pending, field) == value):
                return pending
            del self._pending_upserts[(ti.cls, field, value)]
        return None

    def commit(self) -> int | None:
        """Atomically persist all buffered changes; returns the new commit
        TID, or ``None`` if there was nothing to commit."""
        self._enter()
        capture = self._p1_capture()
        if capture is None:
            return None
        try:
            self._run_p2(capture.batch)
        except BaseException:
            self._p2_rollback(capture)
            raise
        return self._p3_finalize(capture)

    # -- the three commit phases (ADR-001 bound decision 2) ------------------

    def _p1_capture(self) -> _Capture | None:
        """P1 (owner, await-free): discover, validate, encode — then flip the
        captured set CLEAN and re-arm the hooks. The flip happens BEFORE P2
        so a write racing P2 re-dirties through the normal hook path and
        lands in the *next* commit; a failed P2 compensates via
        :meth:`_p2_rollback`."""
        if self._lock is not None and self._lock.lost:
            raise LeaseLostError(
                "this process lost the single-writer lease (paused too long?); "
                "another process may own the store now — refusing to write"
            )
        if self._debug:
            self._sweep_untracked()  # before discovery: rescued entities get walked too
        self._discover_new_graphs()
        pending = {**self._new, **self._dirty}
        deletes = [(oid, ti, obj) for oid, (ti, obj) in self._deleted.items()]
        for oid, _, _ in deletes:
            # ADR-003 precedence: a buffered delete wins over a buffered
            # write to the same OID (a twin hydrated and mutated after the
            # delete was buffered — delete() itself already drops the direct
            # case).
            pending.pop(oid, None)
        if not pending and not deletes:
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
        self._index.check_unique(index_entries, deleted=set(self._deleted))
        new_types: list[tuple[int, str, list[str]]] = []
        encoded: list[tuple[int, int, bytes]] = []
        for oid, obj in pending.items():
            ti = type_info(obj)
            cid = self._cid_for(ti, new_types)
            values = [getattr(obj, name) for name in ti.field_names]
            # Encoding can reject values (e.g. ints beyond msgpack's 64-bit
            # range) — it must run BEFORE the TID allocation so a rejected
            # commit consumes no TID and the buffers stay intact for a
            # fixed-up retry (gapless sequence, invariant 5).
            encoded.append((oid, cid, encode_payload(values, self._oid_for_encode)))
        # Prior payloads for the delta stream (COMMIT-DELTA-v1 §3): read
        # back the last durable payload of every already-persisted record —
        # O(delta) reads, only while consumers watch, and like encoding
        # strictly before the TID allocation (a failed read rejects the
        # commit without consuming a TID). Deletes always have a persisted
        # record (NEW entities never enter the delete buffer), and their
        # tombstones carry it as prior (spec §3.1, strictly verified by the
        # reference applier).
        priors: dict[int, bytes] = {}
        delete_ops: list[tuple[int, int, bytes]] = []
        if self._consumers:
            persisted_oids = [oid for oid in pending if oid not in self._new]
            want = persisted_oids + [oid for oid, _, _ in deletes]
            prior_records = self._backend.load_many(want) if want else {}
            for oid in persisted_oids:
                rec = prior_records.get(oid)
                if rec is None:
                    raise DataCrystalError(
                        f"internal error: dirty entity oid {oid} has no "
                        "persisted record to take a prior payload from"
                    )
                priors[oid] = rec.payload
            for oid, _, _ in deletes:
                rec = prior_records.get(oid)
                if rec is None:
                    raise DataCrystalError(
                        f"internal error: deleted entity oid {oid} has no "
                        "persisted record to take a tombstone prior from"
                    )
                delete_ops.append((oid, rec.cid, rec.payload))
        tid = self._alloc.next_tid()
        records = [
            StoredRecord(oid=oid, cid=cid, tid=tid, payload=payload)
            for oid, cid, payload in encoded
        ]
        batch = CommitBatch(
            tid=tid,
            records=records,
            new_types=new_types,
            meta={
                "next_oid": str(self._alloc.oid_watermark),
                "next_cid": str(self._alloc.cid_watermark),
                "next_tid": str(self._alloc.tid_watermark),
                "root_oid": str(self._root_oid) if self._root_oid is not None else "",
                # Re-stamped per commit (invariant 9, format honesty): an
                # older store upgrades its stamp exactly when this library
                # first writes payload bytes the old reader could misread.
                "format_version": str(FORMAT_VERSION),
            },
            deletes=[oid for oid, _, _ in deletes],
        )
        delta = (
            build_delta(tid, records, new_types, self._root_oid, priors, delete_ops)
            if self._consumers
            else None
        )
        flipped: list[tuple[int, Any, int]] = []
        for oid, obj in pending.items():
            ti = type_info(obj)
            if ti.frozen:
                # Frozen __init__ bypasses the tracked __setattr__, so its
                # containers are still plain — bind them now so post-commit
                # in-place mutation raises instead of silently doing nothing.
                for name in ti.field_names:
                    value = getattr(obj, name)
                    if isinstance(value, (list, dict, tuple)):
                        set_field(obj, name, wrap_value(value, obj))
            flipped.append((oid, obj, state_of(obj)))
            set_state(obj, STATE_CLEAN)
            self._registry.add(oid, obj)
        self._new.clear()
        self._dirty.clear()
        self._deleted.clear()
        return _Capture(tid, batch, index_entries, flipped, delta, deletes)

    def _run_p2(self, batch: CommitBatch) -> None:
        """P2: backend I/O on bytes only, off the owner thread."""
        if self._p2_inline:
            self._backend.apply(batch)
        else:
            self._io_executor().submit(self._backend.apply, batch).result()

    def _p2_rollback(self, capture: _Capture) -> None:
        """A failed P2 was never durable: re-buffer the captured set (unless
        a racing write already re-buffered an entity) and reuse the TID —
        the sequence stays gapless (invariant 5)."""
        self._alloc._next_tid = capture.tid  # pyright: ignore[reportPrivateUsage]  # gapless TID reuse, invariant 5
        for oid, obj, prior in capture.flipped:
            if prior == STATE_NEW:
                set_state(obj, STATE_NEW)
                self._dirty.pop(oid, None)  # racing write during failed P2
                self._new[oid] = obj
            else:
                set_state(obj, STATE_DIRTY)
                self._dirty[oid] = obj
        for oid, ti, obj in capture.deletes:  # the deletions stay buffered too
            self._deleted[oid] = (ti, obj)

    def _p3_finalize(self, capture: _Capture) -> int:
        """P3 (owner): indexes and watermark reflect the now-durable batch."""
        self._index.apply(capture.index_entries)
        self._index.apply_deletes([(oid, ti) for oid, ti, _ in capture.deletes])
        for oid, _, obj in capture.deletes:
            # The identity contract ends with the record: write-bar any live
            # instance (incl. twins hydrated after the delete was buffered),
            # then forget the OID — a later load raises DanglingRefError.
            live = obj if obj is not None else self._registry.get(oid)
            if live is not None:
                set_state(live, STATE_DELETED)
            self._registry.discard(oid)
            self._fingerprints.pop(oid, None)
        self._pending_upserts.clear()  # the committed unique map takes over
        self._durable_cids.update(cid for cid, _, _ in capture.batch.new_types)
        if self._debug:
            for rec in capture.batch.records:
                self._fingerprints[rec.oid] = _crc(rec.payload)
        self._last_tid = capture.tid
        if capture.delta is not None:
            self._deliver(capture.delta)
        return capture.tid

    def _deliver(self, delta: dict[str, Any]) -> None:
        """Hand the now-durable commit's delta to every attached consumer,
        in TID order, on the owner thread. A consumer that raises (or that
        fails to advance its watermark) is detached with a loud warning —
        sidecars are rebuildable derived data (invariant 11); the store
        never holds writes hostage to one."""
        tid = delta["tid"]
        for consumer in list(self._consumers):
            try:
                consumer.apply(delta)
                if consumer.watermark != tid:
                    raise DataCrystalError(
                        f"consumer applied tid {tid} but reports watermark "
                        f"{consumer.watermark} — it violates COMMIT-DELTA-v1 §4.3"
                    )
            except Exception as exc:
                self._consumers.remove(consumer)
                warnings.warn(
                    ConsumerDetachedWarning(
                        f"delta consumer {consumer!r} failed on tid {tid} and "
                        f"was detached ({exc!r}); the commit is durable and the "
                        "store is healthy — rebuild the sidecar (e.g. from "
                        "store.snapshot()) and attach() it again"
                    ),
                    stacklevel=4,
                )

    def _sweep_untracked(self) -> None:
        """debug=True: warn about (and rescue) CLEAN entities whose
        re-encoded record no longer matches their last known fingerprint —
        a mutation slipped past the hooks (KICKOFF risk 1)."""
        for oid, obj in self._registry.items():
            if oid in self._new or oid in self._dirty:
                continue
            if state_of(obj) != STATE_CLEAN:
                continue
            expected = self._fingerprints.get(oid)
            if expected is None:
                continue
            ti = type_info(obj)
            try:
                payload = encode_payload(
                    [getattr(obj, name) for name in ti.field_names],
                    self._oid_for_encode,
                )
                changed = _crc(payload) != expected
            except BaseException:
                # It encoded fine when its fingerprint was taken, so the
                # failure itself proves an untracked change (e.g. a brand-new
                # entity attached via a bypass write). Rescue it: the commit
                # path will register reachable new entities or raise loudly.
                changed = True
            if changed:
                warnings.warn(
                    UntrackedMutationWarning(
                        f"{ti.typename} (oid {oid}) changed without the "
                        "dirty-tracking hook firing; committing it anyway — "
                        "fix the write path (use plain attribute assignment "
                        "or the persistent containers)"
                    ),
                    stacklevel=4,
                )
                set_state(obj, STATE_DIRTY)
                self._dirty[oid] = obj

    def _io_executor(self) -> ThreadPoolExecutor:
        if self._io is None:
            self._io = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="datacrystal-io"
            )
        return self._io

    def submit(self, fn: Callable[[], Any]) -> Future[Any]:
        """Run ``fn()`` on the owner thread; returns a Future for its result.

        The sanctioned cross-thread write path (ADR-001): foreign threads
        must not touch live entities, but they may ship a closure here. The
        owner executes pending submissions whenever it next calls into the
        store (piggyback), or explicitly via :meth:`run_pending`; under
        ``aopen()`` the loop is woken instead. A result that would carry a
        live entity (directly or inside a container) fails the Future with
        ``EntityEscapeError`` — return plain data.
        """
        if self._closed:
            raise StoreClosedError("this store has been closed")
        future: Future[Any] = Future()
        if threading.get_ident() == self._owner:
            self._execute_submission(fn, future)  # owner: run inline, same rules
            return future
        self._submitted.append((fn, future))
        wake = self._wake
        if wake is not None:
            wake()
        return future

    def run_pending(self) -> int:
        """Execute all queued :meth:`submit` work now (owner thread only);
        returns the number of submissions run."""
        self._guard()
        count = self._pump()
        if self._lazyman is not None:  # an explicit boundary sweeps too
            self._lazyman.maybe_sweep()
        return count

    def _pump(self) -> int:
        """Drain the submit() queue on the owner. Called from the public API
        boundaries; reentrant calls (a submission touching the store) no-op."""
        if self._pumping or not self._submitted:
            return 0
        self._pumping = True
        count = 0
        try:
            while True:
                try:
                    fn, future = self._submitted.popleft()
                except IndexError:
                    return count
                self._execute_submission(fn, future)
                count += 1
        finally:
            self._pumping = False

    def _execute_submission(self, fn: Callable[[], Any], future: Future[Any]) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = fn()
        except BaseException as exc:
            future.set_exception(exc)
            return
        offender = _find_escapee(result)
        if offender is not None:
            future.set_exception(EntityEscapeError(
                f"the submit() result would carry a live entity ({offender}) "
                f"across the owner boundary — return plain data instead; "
                + _THREAD_RECIPE
            ))
            return
        future.set_result(result)

    def attach(self, consumer: DeltaConsumer) -> None:
        """Attach a COMMIT-DELTA-v1 consumer: from the next commit on it
        receives every delta, in TID order, on the owner thread, strictly
        after the commit is durable (ROADMAP item 3; spec §4 obligations).

        The consumer's watermark must equal ``store.last_tid``. Deltas are
        not retained (spec §5), so a consumer that is *behind* cannot be
        caught up — rebuild it from ``store.snapshot()`` (the snapshot's
        ``tid`` and ``types`` are exactly the bootstrap it needs). A
        consumer *ahead* of the store means the store was restored to an
        older point — the sidecar is stale and must be rebuilt (fitness #13).
        """
        self._enter()
        watermark = consumer.watermark
        if watermark < self._last_tid:
            raise DeltaGapError(
                f"consumer watermark {watermark} is behind the store "
                f"({self._last_tid}) and deltas are not retained "
                "(COMMIT-DELTA-v1 §5) — rebuild from store.snapshot(), then "
                "attach at the snapshot's tid"
            )
        if watermark > self._last_tid:
            raise DeltaGapError(
                f"consumer watermark {watermark} is ahead of the store "
                f"({self._last_tid}) — the store was restored to an older "
                "point? The sidecar is stale: rebuild it from scratch"
            )
        if any(existing is consumer for existing in self._consumers):
            raise DataCrystalError("this consumer is already attached")
        self._consumers.append(consumer)

    def detach(self, consumer: DeltaConsumer) -> None:
        """Detach a previously attached delta consumer."""
        self._enter()
        for i, existing in enumerate(self._consumers):
            if existing is consumer:
                del self._consumers[i]
                return
        raise DataCrystalError("this consumer is not attached")

    def snapshot(self) -> Snapshot:
        """A frozen, read-only view of the committed state at the current
        durable commit watermark — **callable from any thread**, even while
        the owner commits (ADR-001 rider 2; ADR-002 read views).

        Use it as a context manager and close it promptly: on the sqlite
        backend an open snapshot pins a WAL read transaction. Views are
        plain immutable data (:class:`EntityView`); references come back as
        :class:`Ref` tokens for ``snapshot.get()`` — never live entities.
        """
        # Deliberately NOT _enter(): no owner guard, no piggyback work.
        # The read view isolates itself from the live engine entirely.
        if self._closed:
            raise StoreClosedError("this store has been closed")
        return Snapshot(self._backend.read_view())

    def get(self, cls: type, **unique_key: Any) -> Any | None:
        """Look up one entity by a unique secondary key, e.g.
        ``store.get(Mineral, qid="Q43010")``. Returns ``None`` if absent.
        Reflects committed state."""
        self._enter()
        if len(unique_key) != 1:
            raise TypeError("get() takes exactly one unique-field keyword argument")
        ti = type_info(cls)
        (field, value), = unique_key.items()
        spec = ti.spec(field)
        if spec is None or not spec.unique:
            raise QueryErrorFor(cls, field)
        if self._cid_by_typename.get(ti.typename) is None:
            return None
        ci = self._index.ensure(ti)
        oid = ci.unique[field].get(value)
        return None if oid is None else self._load_oid(oid)

    def get_many(self, refs: Iterable[Any] | type, /, **unique_key: Any) -> list[Any]:
        """Batch-hydrate in one storage round-trip (SDA delta 5: N+1 is
        never the user's problem). Two call shapes::

            store.get_many(mixed)                      # OIDs / Lazy / Ref / entities
            store.get_many(Mineral, qid=["Q1", "Q2"])  # bulk unique-key lookup

        The unique-key form returns a list aligned with the given values,
        ``None`` where a key is absent — the bulk twin of :meth:`get`, for
        ETL upserts that fetch thousands of natural keys at once."""
        self._enter()
        if unique_key:
            if not isinstance(refs, type):
                raise TypeError(
                    "get_many(EntityClass, field=values) needs an @entity "
                    "class as its first argument"
                )
            return self._get_many_by_key(refs, unique_key)
        items: list[Any] = list(refs)  # type: ignore[arg-type]  # a class without kwargs falls through
        wanted: list[int] = []
        for item in items:
            if isinstance(item, Lazy):
                lazy = cast("Lazy[Any]", item)
                oid = lazy.oid
                if not lazy.loaded and oid is not None:
                    wanted.append(oid)
            elif isinstance(item, int):
                wanted.append(item)
            elif isinstance(item, Ref):
                wanted.append(item.oid)
            elif not is_entity(item):
                raise NotAnEntityError(
                    f"get_many() accepts OIDs, Lazy refs, snapshot Refs and "
                    f"entities, got {type(item).__name__}"
                )
        missing = [oid for oid in wanted if self._registry.get(oid) is None]
        cache = self._backend.load_many(missing) if missing else {}
        out: list[Any] = []
        for item in items:
            if isinstance(item, Lazy):
                lazy = cast("Lazy[Any]", item)
                oid = lazy.oid
                if lazy.loaded or oid is None:
                    out.append(lazy.get())
                else:
                    out.append(self._load_oid(oid, cache))
            elif isinstance(item, int):
                out.append(self._load_oid(item, cache))
            elif isinstance(item, Ref):
                out.append(self._load_oid(item.oid, cache))
            else:
                out.append(item)
        return out

    def _get_many_by_key(self, cls: type, unique_key: dict[str, Any]) -> list[Any]:
        if len(unique_key) != 1:
            raise TypeError(
                "get_many() takes exactly one unique-field keyword argument"
            )
        ti = type_info(cls)
        (field, values), = unique_key.items()
        spec = ti.spec(field)
        if spec is None or not spec.unique:
            raise QueryErrorFor(cls, field)
        values = list(values)
        if self._cid_by_typename.get(ti.typename) is None:
            return [None] * len(values)  # silent like get(): the miss idiom
        ci = self._index.ensure(ti)
        oids = [ci.unique[field].get(value) for value in values]
        missing = [
            oid for oid in oids
            if oid is not None and self._registry.get(oid) is None
        ]
        cache = self._backend.load_many(missing) if missing else {}
        return [
            None if oid is None else self._load_oid(oid, cache) for oid in oids
        ]

    def query(self, target: type | Condition, *, limit: int | None = None,
              offset: int = 0) -> list[Any]:
        """Hydrated committed entities matching ``target`` — a Condition,
        or an entity class for the **full extent**.

        ``query(Mineral)`` is the honest spelling of the expensive shape
        (every committed Mineral is hydrated — the same cost any
        non-indexed predicate already pays); symmetric with ``count()``/
        ``pluck()``/``Snapshot.all()`` (decided 2026-06-12). The plan is
        deterministic and inspectable: :meth:`explain`.

        ``limit=``/``offset=`` window the result (#14). On a fully-indexed
        (no-residual) query the slice is applied to the candidate OIDs
        *before* hydration — ``query(C, limit=10)`` loads 10 records, not the
        extent. A residual predicate must decode-to-filter first, so the
        window there only trims the materialized list (it cannot prune the
        scan). Result order is deterministic (ascending OID)."""
        self._enter()
        validate_window(limit, offset)
        cls, cond = query_target(target, "query")
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return []
        ci = self._index.ensure(ti)
        if cond is None:
            bitmap, residual = None, None
        else:
            bitmap, residual = plan(cond, ci)
        oids = list(bitmap) if bitmap is not None else list(ci.extent)
        if residual is None:
            return self.get_many(apply_window(oids, limit, offset))
        objs = [o for o in self.get_many(oids) if residual.evaluate(o)]
        return apply_window(objs, limit, offset)

    def explain(self, target: type | Condition) -> QueryPlan:
        """The deterministic plan for ``target``: what answers from
        bitmaps, what evaluates as a Python residual, and over how many
        candidates — ``query()`` hydrates at most ``plan.candidates``
        entities, ``count()``/``pluck()`` decode the same candidates
        without constructing any.

        Exactly two rules, never an optimizer (the analytics planner is
        DuckDB over the ``[arrow]`` mirror): ``==``/``.in_()`` on
        ``dc.Index``/``dc.Unique`` fields → bitmaps; everything else →
        residual. Read-only; builds the class indexes on first use (the
        same one-time O(extent) cost as a first query)."""
        self._enter()
        cls, cond = query_target(target, "explain")
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return QueryPlan(
                ti.typename, None if cond is None else repr(cond),
                False, None, 0, 0,
            )
        return explain_plan(ti.typename, self._index.ensure(ti), cond)

    def count(self, target: type | Condition) -> int:
        """How many committed entities match — without hydrating any.

        ``count(Mineral)`` is the class extent's cardinality; a fully
        bitmap-answerable condition (``==``/``.in_()`` on indexed fields) is
        bitmap cardinality — both O(1)-ish, zero record loads. A residual
        predicate falls back to a decode-level scan of the candidates:
        records are read and decoded but **no entity is constructed**.
        Reads committed state (like :meth:`snapshot`, unlike :meth:`query`,
        whose hydrated results show uncommitted in-memory changes)."""
        self._enter()
        cls, cond = query_target(target, "count")
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return 0
        ci = self._index.ensure(ti)
        if cond is None:
            return len(ci.extent)
        bitmap, residual = plan(cond, ci)
        if residual is None:
            return len(bitmap) if bitmap is not None else len(ci.extent)
        oids = list(bitmap) if bitmap is not None else list(ci.extent)
        raw_cond = _raw_condition(residual)
        matches = 0
        for _oid, row in self._iter_raw(ti, oids):
            if raw_cond.evaluate(_RawView(row)):
                matches += 1
        return matches

    def pluck(self, target: type | Condition, *fields: str,
              limit: int | None = None, offset: int = 0) -> list[Any]:
        """Project fields without constructing entities — the decode-level
        column read (ROADMAP item 4; full columnar speed is the
        ``datacrystal[arrow]`` mirror's job).

        ``pluck(Mineral, "name")`` returns a list of values;
        ``pluck(cond, "name", "mohs")`` a list of tuples. Entity references
        come back as :class:`~datacrystal.Ref` tokens (feed them to
        :meth:`get_many` to hydrate), containers as plain lists/dicts.
        Reads committed state, like :meth:`count`.

        ``limit=``/``offset=`` window the result (#14) with the same
        stop-early semantics as :meth:`query`: a fully-indexed read decodes
        only the windowed OIDs; a residual read decodes-to-filter, then
        trims."""
        self._enter()
        validate_window(limit, offset)
        if not fields:
            raise TypeError("pluck() takes at least one field name")
        cls, cond = query_target(target, "pluck")
        ti = type_info(cls)
        known = set(ti.field_names)
        for name in fields:
            if name not in known:
                raise QueryError(
                    f"{cls.__name__}.{name} is not a persisted field "
                    f"(fields: {', '.join(ti.field_names)})"
                )
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return []
        ci = self._index.ensure(ti)
        if cond is not None:
            bitmap, residual = plan(cond, ci)
        else:
            bitmap, residual = None, None
        oids = list(bitmap) if bitmap is not None else list(ci.extent)
        windowed_early = residual is None
        if windowed_early:
            oids = apply_window(oids, limit, offset)
        raw_cond = _raw_condition(residual) if residual is not None else None
        single = fields[0] if len(fields) == 1 else None
        out: list[Any] = []
        for _oid, row in self._iter_raw(ti, oids):
            if raw_cond is not None and not raw_cond.evaluate(_RawView(row)):
                continue
            if single is not None:
                out.append(_publish(row[single]))
            else:
                out.append(tuple(_publish(row[name]) for name in fields))
        return out if windowed_early else apply_window(out, limit, offset)

    def iter(self, target: type | Condition) -> Iterator[Any]:
        """Stream hydrated committed entities matching ``target`` chunk by
        chunk, with **bounded memory** — the streaming complement to
        :meth:`query` (whole list) and :meth:`pluck`/:meth:`count`
        (decode-level). Yields live entities with ``query()``'s semantics
        (committed result set; live-instance field reads). Like
        ``count()``/``pluck()`` it reads committed state at **iteration**
        time, not call time.

        The ADR-001 owner guard is re-asserted on every pull, so a foreign
        thread or a closed store stops the stream mid-flight
        (``WrongThreadError`` / ``StoreClosedError``). The peak live set is
        O(chunk), never O(extent) — walk millions of matches in bounded RAM.

        ``store.iter()`` is the freeze-clean streaming surface (additive;
        ``query()``'s signature and list return type stay frozen)."""
        self._enter()
        cls, cond = query_target(target, "iter")
        ti = type_info(cls)
        return self._iter_stream(ti, cond)

    def _iter_stream(self, ti: TypeInfo, cond: Condition | None) -> Iterator[Any]:
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return
        ci = self._index.ensure(ti)
        if cond is None:
            bitmap, residual = None, None
        else:
            bitmap, residual = plan(cond, ci)
        oids = list(bitmap) if bitmap is not None else list(ci.extent)
        for start in range(0, len(oids), _RAW_CHUNK):
            # get_many hydrates one chunk; reassigning `chunk` each round lets
            # the previous chunk's non-retained entities be collected (the
            # O(chunk) bound). get_many() re-enters the owner guard per chunk.
            chunk = self.get_many(oids[start:start + _RAW_CHUNK])
            if residual is not None:
                chunk = [o for o in chunk if residual.evaluate(o)]
            for obj in chunk:
                self._guard()  # ADR-001: re-assert on EVERY __next__
                yield obj

    # -- engine internals ----------------------------------------------------

    def _warn_unseen(self, ti: TypeInfo) -> None:
        warnings.warn(
            UnseenTypeWarning(
                f"the store has no committed records of {ti.cls.__name__} — "
                "the result is empty (first run? forgot to commit()? opened "
                "a different store file?)"
            ),
            stacklevel=3,
        )

    def _iter_raw(self, ti: TypeInfo, oids: list[int]):
        """Decode-level row scan: committed records → field-value dicts via
        the per-cid hydration plan (additive evolution honored). Loads in
        chunks to bound peak memory; constructs no entities, touches no
        registry — the machinery behind count()/pluck() and gate #19."""
        for start in range(0, len(oids), _RAW_CHUNK):
            chunk = oids[start:start + _RAW_CHUNK]
            records = self._backend.load_many(chunk)
            for oid in chunk:
                rec = records.get(oid)
                if rec is None:
                    raise DataCrystalError(
                        f"internal error: indexed oid {oid} has no record"
                    )
                hydration = self._hydration_plan(rec.cid, ti)
                values = decode_payload(rec.payload)
                row: dict[str, Any] = {}
                for spec, index, factory in hydration:
                    row[spec.name] = values[index] if index is not None else factory()
                yield oid, row

    def _guard(self) -> None:
        if self._closed:
            raise StoreClosedError("this store has been closed")
        if threading.get_ident() != self._owner:
            raise WrongThreadError(_THREAD_RECIPE)

    def _enter(self) -> None:
        """Public API boundary: guard, then piggyback the work the owner
        owes — the submit() queue and the lazy-demotion sweep (ADR-001
        daemon principle: both only ever run on the owner)."""
        self._guard()
        self._pump()
        if self._lazyman is not None:
            self._lazyman.maybe_sweep()

    def _on_first_write(self, obj: Any) -> None:
        """First write to a CLEAN entity (called by the one-shot hook,
        BEFORE the mutation lands)."""
        if threading.get_ident() != self._owner:
            raise WrongThreadError(_THREAD_RECIPE)
        if self._closed:
            raise StoreClosedError("this store has been closed")
        set_state(obj, STATE_DIRTY)
        oid = oid_of(obj)
        assert oid is not None  # CLEAN implies stamped
        self._dirty[oid] = obj

    def _register_graph(self, obj: Any, walked: set[int] | None = None) -> int:
        if walked is None:
            walked = set()
        queue: deque[Any] = deque([obj])
        while queue:
            current = queue.popleft()
            if state_of(current) == STATE_DELETED:
                raise DeletedEntityError(
                    f"this {type(current).__name__} was deleted via "
                    "store.delete() and cannot be stored again — create a "
                    "new entity instead (OIDs are never reused)"
                )
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
        if value is None or isinstance(value, (str, float, int, bytes)):
            return  # overwhelmingly the common case: scalars reference nothing
        if is_entity(value):
            oid = oid_of(value)
            if oid is None or oid in self._new or oid in self._dirty:
                queue.append(value)
        elif isinstance(value, Lazy):
            target = cast("Lazy[Any]", value).peek()
            if target is not None:
                self._walk_value(target, queue)
        elif isinstance(value, (list, tuple)):
            for item in cast("tuple[Any, ...]", value):
                self._walk_value(item, queue)
        elif isinstance(value, dict):
            for item in cast("dict[Any, object]", value).values():
                self._walk_value(item, queue)

    def _oid_for_encode(self, obj: Any) -> int:
        oid = oid_of(obj)
        if oid is None:
            raise DataCrystalError(
                f"internal error: {type(obj).__name__} escaped P1 discovery"
            )
        return oid

    def _lineage_for(self, ti: TypeInfo) -> list[tuple[int, list[str]]]:
        """Every (cid, persisted field list) this typename ever committed."""
        return [
            (cid, self._persisted_fields[cid])
            for cid in self._cids_by_typename.get(ti.typename, [])
        ]

    def _cid_for(self, ti: TypeInfo, new_types: list[tuple[int, str, list[str]]]) -> int:
        cid = self._cid_by_typename.get(ti.typename)
        if cid is not None and self._persisted_fields[cid] != list(ti.field_names):
            cid = None  # field shape changed: start a new lineage row
        if cid is None:
            cid = self._alloc.next_cid()
            self._cid_by_typename[ti.typename] = cid
            self._cids_by_typename.setdefault(ti.typename, []).append(cid)
            self._typename_by_cid[cid] = ti.typename
            self._persisted_fields[cid] = list(ti.field_names)
            self._ti_by_cid[cid] = ti
        # Batch every lineage row that is not yet durable — NOT just freshly
        # allocated ones: after a failed P2 the cid stays cached in the maps
        # above, and the retry's batch must carry its types row again or the
        # store ends up with records pointing at a cid it never learned.
        if cid not in self._durable_cids and all(row[0] != cid for row in new_types):
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
        self._ti_by_cid[cid] = ti
        return ti

    def _hydration_plan(self, cid: int, ti: TypeInfo) -> list[tuple[Any, int | None, Any]]:
        """Per-(cid → live class) decode plan: for every live field, where its
        value comes from — a position in the persisted record, or a default
        factory (additive evolution). Removed persisted fields are ignored."""
        plan = self._plan_by_cid.get(cid)
        if plan is None:
            persisted = self._persisted_fields.get(cid, [])
            position = {name: i for i, name in enumerate(persisted)}
            plan = cast("list[tuple[Any, int | None, Any]]", [])
            for spec in ti.specs:
                index = position.get(spec.name)
                if index is None and spec.renamed_from is not None:
                    # #26 (a): the new name isn't persisted, but the old one is
                    # — bind the old column so the rename follows the code
                    # (additive, never rewrites the record).
                    index = position.get(spec.renamed_from)
                if index is not None:
                    plan.append((spec, index, None))
                    continue
                factory = ti.defaults.get(spec.name)
                if factory is None:
                    raise SchemaMismatchError(
                        f"{ti.typename}.{spec.name} does not exist in records "
                        f"persisted with fields {persisted} and has no default "
                        "— give the new field a default value to enable "
                        "additive schema evolution"
                    )
                plan.append((spec, None, factory))
            self._plan_by_cid[cid] = plan
        return plan

    def _load_oid(self, oid: int, cache: dict[int, StoredRecord] | None = None) -> Any:
        self._guard()
        obj = self._registry.get(oid)
        if obj is not None:
            return obj
        rec = cache.get(oid) if cache else None
        if rec is None:
            rec = self._backend.load_many([oid]).get(oid)
        if rec is None:
            raise DanglingRefError(
                f"no record for oid {oid} in the store — deleted (v0.x "
                "deletes are unchecked, ADR-003) or never committed; the "
                "reference you followed is stale"
            )
        return self._materialize(rec, cache)

    def _materialize(self, rec: StoredRecord, cache: dict[int, StoredRecord] | None) -> Any:
        ti = self._ti_for_cid(rec.cid)
        plan = self._hydration_plan(rec.cid, ti)
        obj = cast("Any", object.__new__(ti.cls))
        stamp(obj, rec.oid, self, STATE_CLEAN)
        self._registry.add(rec.oid, obj)  # before fills: breaks reference cycles
        try:
            values = decode_payload(rec.payload)
            persisted = self._persisted_fields.get(rec.cid, [])
            if len(values) != len(persisted):
                raise SchemaMismatchError(
                    f"{ti.typename}: record has {len(values)} fields, its type "
                    f"dictionary row has {len(persisted)} — the store is damaged"
                )
            fill = object.__setattr__  # bound once: this loop is the hot path
            for spec, index, factory in plan:
                raw = values[index] if index is not None else factory()
                if raw is None or type(raw) in _SCALAR_TYPES:
                    fill(obj, spec.name, raw)  # scalars skip the resolve call
                else:
                    fill(obj, spec.name,
                         self._resolve(raw, spec.lazy_refs, cache, obj))
        except BaseException:
            # A failed hydration (dangling eager ref, schema mismatch) must
            # not leave a half-filled corpse behind the identity contract.
            self._registry.discard(rec.oid)
            raise
        if self._debug:
            # Fingerprint via the same encode path the sweep uses (NOT the
            # stored payload: an old-lineage record re-encodes through the
            # live class shape, which must not read as a mutation).
            try:
                self._fingerprints[rec.oid] = _crc(encode_payload(
                    [getattr(obj, name) for name in ti.field_names],
                    self._oid_for_encode,
                ))
            except BaseException:
                self._fingerprints.pop(rec.oid, None)
        return obj

    def _resolve(self, value: Any, lazy: bool, cache: dict[int, StoredRecord] | None,
                 owner: Any) -> Any:
        if value is None or isinstance(value, (str, float, int, bytes)):
            return value  # scalar fast path: nothing to swizzle back
        if isinstance(value, RefToken):
            if lazy:
                existing = self._registry.get(value.oid)
                if existing is not None:
                    # engine-only Lazy constructors (users use Lazy.of)
                    handle: Lazy[Any] = Lazy[Any]._loaded(  # pyright: ignore[reportPrivateUsage]
                        existing, value.oid, self
                    )
                    if self._lazyman is not None:
                        self._lazyman.track(handle)
                    return handle
                # engine-only Lazy constructors (users use Lazy.of)
                unloaded: Lazy[Any] = Lazy._unloaded(value.oid, self)  # pyright: ignore[reportPrivateUsage]
                return unloaded
            return self._load_oid(value.oid, cache)
        if isinstance(value, list):
            return PersistentList(
                (self._resolve(item, lazy, cache, owner)
                 for item in cast("list[object]", value)),
                owner=owner,
            )
        if isinstance(value, dict):
            return PersistentDict(
                ((k, self._resolve(v, lazy, cache, owner))
                 for k, v in cast("dict[Any, object]", value).items()),
                owner=owner,
            )
        return value

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"tid={self._last_tid}"
        return f"<datacrystal.Store {state}>"


_RAW_CHUNK = 8192  # records per load_many in decode-level scans (peak-RAM bound)


class _RawView:
    """A decoded record posing as an entity for ``Condition.evaluate`` —
    attribute reads come from the row dict, nothing else exists."""

    __slots__ = ("_row",)

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    def __getattr__(self, name: str) -> Any:
        try:
            return self._row[name]
        except KeyError:
            raise AttributeError(name) from None


def _ref_target_oid(value: Any) -> int | None:
    """The OID a reference-shaped value points at (entity or Lazy handle),
    or None for plain values and not-yet-stored targets."""
    if is_entity(value):
        return oid_of(value)
    if isinstance(value, Lazy):
        if value.oid is not None:
            return value.oid
        target = cast("Lazy[Any]", value).peek()
        return oid_of(target) if target is not None else None
    return None


def _equivalent(cur: Any, new: Any) -> bool:
    """Would persisting ``new`` over ``cur`` write the same bytes? Decides
    whether upsert() skips a field. Conservative: references match by
    target OID (a Lazy handle and a direct reference encode identically);
    a type change (e.g. ``1`` → ``True``) re-writes even when ``==`` says
    equal, because msgpack bytes differ; wrapped containers compare to the
    plain ones they came from."""
    if cur is new:
        return True
    cur_ref, new_ref = _ref_target_oid(cur), _ref_target_oid(new)
    if cur_ref is not None or new_ref is not None:
        return cur_ref == new_ref and cur_ref is not None
    if cur.__class__ is new.__class__:
        return bool(cur == new)
    if isinstance(cur, list) and isinstance(new, list):
        return bool(cur == new)  # PersistentList vs the plain list
    if isinstance(cur, dict) and isinstance(new, dict):
        return bool(cur == new)
    return False


def _raw_value(value: Any) -> Any:
    """Map entity/Lazy predicate values onto the decoded representation
    (RefTokens compare by OID), so residuals evaluate without hydration."""
    if is_entity(value):
        oid = oid_of(value)
        if oid is None:
            raise QueryError(
                "cannot match an entity that was never stored — it has no OID"
            )
        return RefToken(oid)
    if isinstance(value, Lazy):
        target = cast("Lazy[Any]", value).peek()  # mirror swizzle(): a loaded handle knows best
        if target is not None:
            return _raw_value(target)
        if value.oid is None:
            raise QueryError("cannot match an unloaded Lazy without an OID")
        return RefToken(value.oid)
    return value


def _raw_condition(cond: Condition) -> Condition:
    if isinstance(cond, Pred):
        if cond.op == "in":
            return Pred(cond.cls, cond.field, "in",
                        tuple(_raw_value(v) for v in cond.value))
        return Pred(cond.cls, cond.field, cond.op, _raw_value(cond.value))
    if isinstance(cond, And):
        return And(tuple(_raw_condition(p) for p in cond.parts))
    if isinstance(cond, Or):
        return Or(tuple(_raw_condition(p) for p in cond.parts))
    if isinstance(cond, Not):
        return Not(_raw_condition(cond.part))
    return cond


def _publish(value: Any) -> Any:
    """Decoded payload value → pluck() output: refs become public Ref
    tokens (get_many() hydrates them), containers plain lists/dicts."""
    if isinstance(value, RefToken):
        return Ref(value.oid)
    if isinstance(value, list):
        return [_publish(item) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        return {key: _publish(item)
                for key, item in cast("dict[Any, object]", value).items()}
    return value


def _find_escapee(value: Any) -> str | None:
    """Name the first live entity (or handle to one) inside a submit()
    result, or None if the value is plain data (ADR-001: EntityEscapeError)."""
    if is_entity(value):
        return type(value).__name__
    if isinstance(value, Lazy):
        target = cast("Lazy[Any]", value).peek()
        return f"Lazy[{type(target).__name__}]" if target is not None else "Lazy"
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in cast("frozenset[object]", value):
            found = _find_escapee(item)
            if found is not None:
                return found
    elif isinstance(value, dict):
        for key, item in cast("dict[Any, object]", value).items():
            found = _find_escapee(key) or _find_escapee(item)
            if found is not None:
                return found
    return None


def QueryErrorFor(cls: type, field: str) -> Exception:
    from datacrystal._errors import QueryError

    return QueryError(
        f"{cls.__name__}.{field} is not a Unique field; "
        "get() looks up unique secondary keys only — use query() for the rest"
    )
