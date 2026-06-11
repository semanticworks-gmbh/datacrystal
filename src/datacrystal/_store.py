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
from typing import Any, Callable, Iterable

from datacrystal._conditions import Condition
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
from datacrystal._state import STATE_CLEAN, STATE_DIRTY, STATE_NEW
from datacrystal._errors import (
    DataCrystalError,
    EntityEscapeError,
    LeaseLostError,
    NotAnEntityError,
    SchemaMismatchError,
    StoreClosedError,
    UnregisteredTypeError,
    UntrackedMutationWarning,
    WrongThreadError,
)
from datacrystal._ids import IdAllocator, OID_BASE, TID_BASE
from datacrystal._indexes import IndexManager, plan
from datacrystal._lazy import Lazy
from datacrystal._records import RefToken, crc as _crc, decode_payload, encode_payload
from datacrystal._registry import ObjectRegistry
from datacrystal._storage.protocol import CommitBatch, StorageBackend, StoredRecord

_THREAD_RECIPE = (
    "live entities and their store are confined to the thread that opened the "
    "store (ADR-001); send work to the owner via store.submit(fn) — the owner "
    "runs it at its next store call or store.run_pending() — and return plain "
    "data, never live entities; cross-thread reads via store.snapshot() land "
    "at M3"
)


@entity
class _Root:
    """Internal holder so ``store.root`` may be any persistable value."""

    value: Any = None


class _Capture:
    """Everything P1 hands to P2/P3 — and enough to compensate a failed P2."""

    __slots__ = ("tid", "batch", "index_entries", "flipped")

    def __init__(self, tid: int, batch: CommitBatch,
                 index_entries: list[tuple[int, TypeInfo, dict[str, Any]]],
                 flipped: list[tuple[int, Any, int]]) -> None:
        self.tid = tid
        self.batch = batch
        self.index_entries = index_entries
        self.flipped = flipped  # (oid, obj, state before the P1 flip)


class Store:
    """An open datacrystal store. Create via :meth:`Store.open`."""

    def __init__(self, backend: StorageBackend, lock: Any | None, *,
                 p2_inline: bool = False, debug: bool = False) -> None:
        self._backend = backend
        self._lock = lock
        self._owner = threading.get_ident()
        self._closed = False
        self._registry = ObjectRegistry()
        self._new: dict[int, Any] = {}
        self._dirty: dict[int, Any] = {}
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
        # The root holder is PINNED (strong reference): everything reachable
        # from store.root stays live — without this, a CLEAN root graph with
        # no user references would be collected and silently rehydrated,
        # losing identity and any in-place mutations. Lazy[T] is the explicit
        # cut point where pinning (and memory) stops.
        self._root_holder: Any = None

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
             lock_ttl: float = 10.0, debug: bool = False) -> "Store":
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
            return cls(backend, lock, p2_inline=sqlite3.threadsafety < 3, debug=debug)
        except BaseException:
            lock.release()
            raise

    @classmethod
    def _from_backend(cls, backend: StorageBackend, *, debug: bool = False) -> "Store":
        """Open over an explicit backend (tests; no lock file)."""
        return cls(backend, None, debug=debug)

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
        self._guard()
        self._pump()
        if self._root_oid is None:
            return None
        if self._root_holder is None:
            self._root_holder = self._load_oid(self._root_oid)
        return self._root_holder.value

    @root.setter
    def root(self, value: Any) -> None:
        """Assigning the root captures it immediately: lists/dicts come back
        from ``store.root`` as tracked persistent containers (mutate them in
        place — ``commit()`` sees it), and new entities in the value are
        registered for the next commit."""
        self._guard()
        self._pump()
        if self._root_oid is None:
            holder = _Root(value=value)
            self._root_oid = self._register_graph(holder)
        else:
            holder = self._root_holder
            if holder is None:
                holder = self._load_oid(self._root_oid)
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
        self._guard()
        self._pump()
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        return self._register_graph(obj)

    def mark_dirty(self, obj: Any) -> None:
        """Explicitly buffer an entity for the next commit. Rarely needed —
        attribute writes and in-place container mutation are tracked
        automatically; this is the escape hatch for anything exotic."""
        self._guard()
        self._pump()
        if not is_entity(obj):
            raise NotAnEntityError(
                f"{type(obj).__name__} is not an @entity class instance"
            )
        oid = oid_of(obj)
        if oid is None:
            self._register_graph(obj)
        elif state_of(obj) == STATE_CLEAN:
            set_state(obj, STATE_DIRTY)
            self._dirty[oid] = obj

    def commit(self) -> int | None:
        """Atomically persist all buffered changes; returns the new commit
        TID, or ``None`` if there was nothing to commit."""
        self._guard()
        self._pump()
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
        return _Capture(tid, batch, index_entries, flipped)

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
        self._alloc._next_tid = capture.tid
        for oid, obj, prior in capture.flipped:
            if prior == STATE_NEW:
                set_state(obj, STATE_NEW)
                self._dirty.pop(oid, None)  # racing write during failed P2
                self._new[oid] = obj
            else:
                set_state(obj, STATE_DIRTY)
                self._dirty[oid] = obj

    def _p3_finalize(self, capture: _Capture) -> int:
        """P3 (owner): indexes and watermark reflect the now-durable batch."""
        self._index.apply(capture.index_entries)
        self._durable_cids.update(cid for cid, _, _ in capture.batch.new_types)
        if self._debug:
            for rec in capture.batch.records:
                self._fingerprints[rec.oid] = _crc(rec.payload)
        self._last_tid = capture.tid
        return capture.tid

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
        return self._pump()

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

    def get(self, cls: type, **unique_key: Any) -> Any | None:
        """Look up one entity by a unique secondary key, e.g.
        ``store.get(Mineral, qid="Q43010")``. Returns ``None`` if absent.
        Reflects committed state."""
        self._guard()
        self._pump()
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
        self._pump()
        items = list(refs)
        wanted: list[int] = []
        for item in items:
            if isinstance(item, Lazy):
                oid = item.oid
                if not item.loaded and oid is not None:
                    wanted.append(oid)
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
                oid = item.oid
                if item.loaded or oid is None:
                    out.append(item.get())
                else:
                    out.append(self._load_oid(oid, cache))
            elif isinstance(item, int):
                out.append(self._load_oid(item, cache))
            else:
                out.append(item)
        return out

    def query(self, cond: Condition) -> list[Any]:
        """Evaluate a Condition over the *committed* state of one entity
        class; returns hydrated entities."""
        self._guard()
        self._pump()
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
        oid = oid_of(obj)
        assert oid is not None  # CLEAN implies stamped
        self._dirty[oid] = obj

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
            plan = []
            for spec in ti.specs:
                index = position.get(spec.name)
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
            raise DataCrystalError(f"no record for oid {oid} in the store")
        return self._materialize(rec, cache)

    def _materialize(self, rec: StoredRecord, cache: dict[int, StoredRecord] | None) -> Any:
        ti = self._ti_for_cid(rec.cid)
        plan = self._hydration_plan(rec.cid, ti)
        obj = object.__new__(ti.cls)
        stamp(obj, rec.oid, self, STATE_CLEAN)
        self._registry.add(rec.oid, obj)  # before fills: breaks reference cycles
        values = decode_payload(rec.payload)
        persisted = self._persisted_fields.get(rec.cid, [])
        if len(values) != len(persisted):
            raise SchemaMismatchError(
                f"{ti.typename}: record has {len(values)} fields, its type "
                f"dictionary row has {len(persisted)} — the store is damaged"
            )
        for spec, index, factory in plan:
            raw = values[index] if index is not None else factory()
            set_field(obj, spec.name, self._resolve(raw, spec.lazy_refs, cache, obj))
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
        if isinstance(value, RefToken):
            if lazy:
                existing = self._registry.get(value.oid)
                if existing is not None:
                    return Lazy.of(existing)
                return Lazy._unloaded(value.oid, self)
            return self._load_oid(value.oid, cache)
        if isinstance(value, list):
            return PersistentList(
                (self._resolve(item, lazy, cache, owner) for item in value), owner=owner
            )
        if isinstance(value, dict):
            return PersistentDict(
                ((k, self._resolve(v, lazy, cache, owner)) for k, v in value.items()),
                owner=owner,
            )
        return value

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"tid={self._last_tid}"
        return f"<datacrystal.Store {state}>"


def _find_escapee(value: Any) -> str | None:
    """Name the first live entity (or handle to one) inside a submit()
    result, or None if the value is plain data (ADR-001: EntityEscapeError)."""
    if is_entity(value):
        return type(value).__name__
    if isinstance(value, Lazy):
        target = value.peek()
        return f"Lazy[{type(target).__name__}]" if target is not None else "Lazy"
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            found = _find_escapee(item)
            if found is not None:
                return found
    elif isinstance(value, dict):
        for key, item in value.items():
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
