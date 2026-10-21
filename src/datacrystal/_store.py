"""The Store facade: open, root, store, commit, query, get, get_many.

Owner-confined (ADR-001): foreign threads get ``WrongThreadError``. Commit is ADR-001's three-phase
machine: P1 captures on the owner, P2 applies on the IO worker, P3 finalizes on the owner; a failed
P2 reuses its TID, keeping the sequence gapless. Writes buffer until ``commit()``; the storer holds
pending objects strongly and pins the root.
Details: docs/design/ADR-001-concurrency-contract.md, "Implementation notes".
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
import warnings
from collections import deque
from collections.abc import Callable, Generator, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, BinaryIO, NoReturn, Self, cast

from pyroaring import BitMap64

from datacrystal._actors import Actor, Principal
from datacrystal._conditions import (
    And,
    Condition,
    Not,
    Or,
    Pred,
    apply_window,
    order_by_values,
    parse_order_by,
    query_target,
    validate_window,
    window_iter,
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
from datacrystal._errors import (
    ConflictError,
    ConsumerDetachedWarning,
    DanglingDeleteWarning,
    DanglingRefError,
    DataCrystalError,
    DeletedEntityError,
    EntityEscapeError,
    LeaseLostError,
    NotAnEntityError,
    QueryError,
    ReadDeniedError,
    SchemaMismatchError,
    SponsorRequiredError,
    StoreClosedError,
    UncommittedActorError,
    UniqueViolationError,
    UnknownActorError,
    UnregisteredTypeError,
    UnseenTypeWarning,
    UntrackedMutationWarning,
    WriteDeniedError,
    WrongThreadError,
)
from datacrystal._ids import FORMAT_VERSION, OID_BASE, TID_BASE, IdAllocator
from datacrystal._index_cache import IndexCache
from datacrystal._indexes import (
    ClassIndexes,
    IndexManager,
    QueryPlan,
    explain_plan,
    plan,
    readable_bitmap,
    windowed_index_order,
)
from datacrystal._lazy import BlobHandle, BlobSource, Lazy, LazyReferenceManager
from datacrystal._permissions import (
    NO_STANDING,
    PERM_FIELDS,
    PERM_LEGACY_FILLS,
    VIEWER,
    LabelColumns,
    authority_towards,
    can_read_row,
    is_root,
    level,
)
from datacrystal._pipeline import DeltaConsumer, build_delta
from datacrystal._records import (
    BlobToken,
    RefToken,
    decode_payload,
    encode_payload,
    fingerprint_payload,
)
from datacrystal._records import (
    crc as _crc,
)
from datacrystal._redacted import Redacted, make_twin
from datacrystal._registry import ObjectRegistry
from datacrystal._snapshot import (
    Ref,
    Snapshot,
    _SnapshotCore,  # the per-watermark core ctor
)
from datacrystal._state import STATE_CLEAN, STATE_DELETED, STATE_DIRTY, STATE_NEW
from datacrystal._storage.protocol import (
    CommitBatch,
    StorageBackend,
    StoredBlob,
    StoredRecord,
    StreamedBlob,
)
from datacrystal.contract.applier import DeltaGapError

if TYPE_CHECKING:
    from datacrystal._follower import HttpClient
    from datacrystal._storage.lock import LeaseLock  # imported lazily at runtime (see Store.open)

# Exact-type membership for the hydration fast path (decode produces exact
# builtin types, never subclasses — subclass-shaped values still take _resolve).
_SCALAR_TYPES = frozenset((str, int, float, bool, bytes))

# Values that reference nothing, so a graph walk or a ref harvest skips them.
_LEAF_TYPES = (str, float, int, bytes)

# ``sqlite3.threadsafety`` of a fully serialized build, the level off-thread P2 needs.
_SQLITE_SERIALIZED = 3

# The four ``_dc_`` columns of a protected record: (owner, groups, read floor, write floor).
type _Labels = tuple[int, list[int], int, int]

type _IndexEntries = list[tuple[int, TypeInfo, dict[str, Any]]]
type _RefEntries = list[tuple[int, set[int]]]  # (referrer oid, {target oids}) — #20

# Sentinel for Store._perm_positions: "not computed yet" (None already means
# a pre-protection lineage cid — the R7 legacy fill, ADR-008).
_PERM_POS_UNSET: Any = object()

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


class _EncodedBatch:
    """What P1's encode step collects: record payloads, new type rows and out-of-line blobs."""

    __slots__ = ("blob_data", "blob_swaps", "new_types", "records", "stream_data")

    def __init__(self) -> None:
        self.new_types: list[tuple[int, str, list[str]]] = []
        self.records: list[tuple[int, int, bytes]] = []  # (oid, cid, payload)
        # Out-of-line blob values (ADR-007 / #82): (oid, size, hash, data); the record keeps a
        # BLOB_EXT descriptor.
        self.blob_data: list[tuple[int, int, bytes, bytes]] = []
        # Streamed blobs (ADR-007 §4): (oid, size, hash, open_chunks) — the bytes are NOT
        # resident; they fill a zeroblob cell in P2.
        self.stream_data: list[tuple[int, int, bytes, Callable[[], Iterable[bytes]]]] = []
        # (obj, field, blob_oid, size, hash) — applied in P3 after durability.
        self.blob_swaps: list[tuple[Any, str, int, int, bytes]] = []


class _Capture:
    """Everything P1 hands to P2/P3 — and enough to compensate a failed P2."""

    __slots__ = (
        "batch",
        "blob_swaps",
        "deletes",
        "delta",
        "flipped",
        "index_entries",
        "ref_entries",
        "tid",
    )

    def __init__(self, tid: int, batch: CommitBatch,
                 index_entries: _IndexEntries,
                 ref_entries: _RefEntries,
                 flipped: list[tuple[int, Any, int]],
                 delta: dict[str, Any] | None,
                 deletes: list[tuple[int, TypeInfo, Any | None]],
                 blob_swaps: list[tuple[Any, str, int, int, bytes]]) -> None:
        self.tid = tid
        self.batch = batch
        self.index_entries = index_entries
        self.ref_entries = ref_entries  # (referrer oid, {target oids}) — #20
        self.flipped = flipped  # (oid, obj, state before the P1 flip)
        self.delta = delta  # COMMIT-DELTA-v2 map; built only when consumers watch
        self.deletes = deletes  # (oid, ti, live instance or None) — ADR-003
        # (obj, field, blob_oid, size, hash): swap each committed streamed-write
        # source to a readable BlobHandle in P3, AFTER durability (ADR-007 §4).
        self.blob_swaps = blob_swaps


class _CommitAttempt:
    """One iteration of a :meth:`Store.committing` loop: run the block, commit on a clean exit.

    A ``ConflictError`` discards the buffer, ``sync()``s and signals a re-run against fresh state.
    Any other exception propagates unchanged: only an OCC conflict is retried. On a single-node
    store ``commit()`` never conflicts, so the first attempt commits.
    """

    __slots__ = ("_store", "committed", "conflict", "used")

    def __init__(self, store: Store) -> None:
        self._store = store
        self.committed = False
        self.conflict: ConflictError | None = None
        self.used = False

    def __enter__(self) -> Self:
        self.used = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc_type is not None:
            return False  # the block raised — don't commit, don't retry, don't swallow
        try:
            self._store.commit()
        except ConflictError as conflict:
            # A conflict only arises on a follower (a single-node commit cannot),
            # so recover so the loop can re-run: drop the rejected edit, then pull
            # the coordinator's change that moved the entity under us.
            self._store.discard()
            self._store.sync()
            self.conflict = conflict
            return True  # swallow — committing() retries or re-raises after exhaustion
        self.committed = True
        return False


class Store:
    """An open datacrystal store. Create via :meth:`Store.open`."""

    def __init__(self, backend: StorageBackend, lock: LeaseLock | None, *,
                 p2_inline: bool = False, debug: bool = False,
                 strict_deletes: bool = False,
                 lazy_timeout: float | None = None,
                 lazy_clock: Callable[[], float] | None = None,
                 cache_dir: Path | None = None,
                 principal: Principal | None = None) -> None:
        self._backend = backend
        self._lock = lock
        self._owner = threading.get_ident()
        self._closed = False
        self._registry = ObjectRegistry()
        self._new: dict[int, Any] = {}
        self._dirty: dict[int, Any] = {}
        # Birth/inherited labels of fresh protected records (ADR-008 W2): the gate's baseline for a
        # NEW record. Inheritance and birth defaults are library policy, not the principal's grant,
        # so only explicit verb changes on top are ceiling-checked. Keyed by oid: set in
        # _stamp_perm_labels, read by _check_write_gate, cleared with the buffer (kept across a
        # denied commit so a fixed-up retry still has it).
        self._birth_labels: dict[int, _Labels] = {}
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
        # strict_deletes (#110, ADR-003): the eager dangling-ref check at commit. debug=True arms
        # it as a warning (DanglingDeleteWarning), strict_deletes=True as a raised
        # DanglingRefError. Unarmed, the commit path is unchanged and the reverse index stays
        # unbuilt (spec §5).
        self._strict_deletes = strict_deletes
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
        self._root_holder: _Root | None = None
        # Session identity (epic #168 W1): the ambient principal commits are stamped with
        # (COMMIT-DELTA-v2). It is config-trusted: the store that holds the Actor registry cannot
        # resolve its own opener. A ContextVar, not a plain stack, so an acting_as scope is
        # task-confined under aopen(). acting_as() pushes scoped identities on top; uid 0 is
        # anonymous.
        self._principal = principal if principal is not None else Principal(uid=0)
        self._acting: ContextVar[tuple[Principal, ...]] = ContextVar(
            "datacrystal-acting", default=()
        )
        # The v2 `at` stamp's clock — int nanoseconds, injectable (a PRIVATE
        # seam per ADR-008 batch 1: goldens/vectors/fitness pin it; the
        # public API stays clock-free). Wall clocks step and skew, so TID
        # remains the only ordering truth (COMMIT-DELTA-v2 §5).
        self._clock: Callable[[], int] = time.time_ns
        # COMMIT-DELTA-v2 consumers (ROADMAP item 3). Commits build and
        # deliver deltas only while this list is non-empty — an unwatched
        # store pays nothing for the pipeline (spec §5).
        self._consumers: list[DeltaConsumer] = []
        # Follower catch-up hook (#151, ROADMAP item 21): open_follower installs
        # a closure that fetches + applies the coordinator's new deltas and
        # returns the new watermark; None for a normal (non-follower) store.
        self._sync_fn: Callable[[int], int] | None = None
        # Follower contribute hook (#153): open_follower installs a closure that
        # serializes the buffered (entity, base) items and POSTs them to the
        # coordinator's /v1/submit, returning the applied TID; None for a normal
        # store (commit() then takes the local P1/P2/P3 path).
        self._contribute_fn: (
            Callable[[list[tuple[Any, str | None]]], int | None] | None
        ) = None

        # The index-cache sidecar (ADR-005 / #12) is per-session (tied to the
        # store dir); created here, then the (re)loadable state below reads it.
        self._index_cache = (
            IndexCache(cache_dir / "index.cache") if cache_dir is not None else None
        )
        self._load_state(use_cache=True)

    def _load_state(self, *, use_cache: bool) -> None:
        """(Re)derive all boot-time state from the backend, in one place.

        Called at open (``use_cache=True``) and by a follower's :meth:`sync` (``use_cache=False``,
        #151). Allocator, watermark, root OID, type lineage and index manager are re-read from
        ``backend.boot()``. A refresh rebuilds the index from records; the sidecar is only read at
        open (invariant 11).
        """
        backend = self._backend
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
        # Per-cid positions of the four _dc_ columns in the persisted shape
        # (None = a pre-protection cid → R7 legacy fill). The write gate's
        # prior-label partial decode caches through here (W2-5).
        self._perm_positions: dict[int, tuple[int, int, int, int] | None] = {}
        cached = (
            self._index_cache.read(self._last_tid)
            if use_cache and self._index_cache is not None else None
        )
        cache_blobs = cached["classes"] if cached is not None else None
        reverse_blob = cached["reverse"] if cached is not None else None
        self._index = IndexManager(backend, self._lineage_for,
                                   self._persisted_fields.keys, cache_blobs,
                                   reverse_blob)

    def _refresh_from_backend(self) -> None:
        """Discard in-memory caches and re-derive all state from the backend.

        For a follower whose backend just received new committed deltas
        (:meth:`sync`, #151): the OID registry, the pinned root, and the
        type-lineage + index manager are rebuilt so the next read reflects the
        new state. Read-only — :meth:`sync` guards that no local writes are
        buffered, so the dirty/new/deleted maps are clear here.
        """
        self._registry = ObjectRegistry()
        self._root_holder = None
        self._new.clear()
        self._dirty.clear()
        self._birth_labels.clear()
        self._deleted.clear()
        self._pending_upserts.clear()
        self._fingerprints.clear()
        self._load_state(use_cache=False)

    def sync(self) -> int:
        """Pull and apply the coordinator's new deltas and return the watermark (#151).

        Synchronous and owner-thread (the v0 follower contract); only an ``open_follower`` store
        can sync. A live reference to a CLEAN entity read before a sync keeps stale values:
        re-query.
        Raises ``DeltaGapError`` (resync from 0), ``RuntimeError`` for a non-follower or buffered
        writes. The OCC recovery loop: docs/design/COMMIT-DELTA-v2.md, section 8.
        """
        self._enter()
        if self._sync_fn is None:
            raise RuntimeError("sync() needs a follower store — see dc.open_follower")
        if self._new or self._dirty or self._deleted:
            raise RuntimeError("commit or discard buffered writes before sync()")
        before = self._last_tid
        applied = self._sync_fn(before)
        if applied > before:
            self._refresh_from_backend()
        return self._last_tid

    def discard(self) -> int:
        """Drop all buffered writes and re-read committed state; return the committed watermark.

        The rollback that :meth:`sync` names: a rejected follower ``commit()`` leaves its buffer
        held. OCC recovery loop: discard, sync, re-read, re-apply, commit (#153); works on any
        store. A live reference read before the discard is detached: re-read for current values.
        """
        self._enter()
        self._refresh_from_backend()
        return self._last_tid

    def committing(self, *, retries: int = 3) -> Iterator[_CommitAttempt]:
        """Run a block in a commit loop that retries on conflict, the same code on any topology.

        Use ``for txn in store.committing(): with txn: ...``; a clean exit commits. A follower's
        stale write re-runs the block on fresh state (``discard``, ``sync``) up to ``retries``
        times, then raises ``ConflictError``. Never call :meth:`commit` inside;
        ``WriteDeniedError`` is not retried (ADR-008).
        Details: docs/reference.md, "Behaviour notes".
        """
        self._enter()
        tries = 0
        while True:
            attempt = _CommitAttempt(self)
            yield attempt
            if attempt.committed:
                return
            if not attempt.used:
                raise RuntimeError(
                    "committing() loop body must be `with txn: ...` — the yielded "
                    "attempt was never entered"
                )
            # __exit__ caught a ConflictError and already recovered (discard + sync)
            if tries >= retries:
                assert attempt.conflict is not None
                raise attempt.conflict
            tries += 1

    # -- session identity (epic #168 W1) --------------------------------------

    @property
    def principal(self) -> Principal:
        """The principal in effect: the innermost :meth:`acting_as` scope, else the ambient one.

        Commits are stamped with the committing principal's uid (COMMIT-DELTA-v2), not the
        buffering one. Under ``aopen()`` the scope is task-confined (contextvars). A foreign thread
        has no scopes and reads the ambient identity.
        Details: docs/reference.md, "Behaviour notes".
        """
        stack = self._acting.get()
        return stack[-1] if stack else self._principal

    @contextmanager
    def acting_as(self, subject: int | Principal) -> Generator[Principal]:
        """Switch the session identity for a scope (epic #168 W1).

        A :class:`~datacrystal.Principal` is used as-is; an ``int`` uid resolves through the
        committed ``dc.Actor`` registry, where a non-human needs a sponsor. Scopes nest; under
        ``aopen()`` a task spawned inside the scope inherits its identity.
        Raises ``UnknownActorError``, ``UncommittedActorError``, ``SponsorRequiredError``.
        Details: docs/reference.md, "Behaviour notes".
        """
        self._enter()
        if isinstance(subject, Principal):
            principal = subject
        else:
            actor = self._resolve_registry_actor(subject)
            principal = Principal(uid=actor.uid, memberships=dict(actor.memberships))
        token = self._acting.set((*self._acting.get(), principal))
        try:
            yield principal
        finally:
            try:
                self._acting.reset(token)
            except ValueError:
                # The scope was finalized in a foreign context (e.g. a
                # generator holding it was GC'd in another task) — reset
                # cannot apply there; best-effort pop keeps the stack sane
                # instead of dying with a raw ContextVar error.
                stack = self._acting.get()
                if stack and stack[-1] is principal:
                    self._acting.set(stack[:-1])

    def _resolve_registry_actor(self, uid: int) -> Actor:
        """Resolve ``uid`` through the committed Actor registry, enforcing the sponsor gates.

        A non-human must name a sponsor that resolves to a committed human actor. Reads via
        :meth:`_get_by_key_unchecked`, not :meth:`get` (ADR-008 W3-3): a fenced lookup would raise
        ``UnknownActorError`` for a session that cannot yet read its own registry row.
        See docs/design/ADR-008-permissions-contract.md, "R14 deref checkpoint".
        """
        actor = self._get_by_key_unchecked(Actor, "uid", uid)
        if actor is None:
            raise UnknownActorError(
                f"no dc.Actor with uid={uid} is registered in this "
                "store — store an Actor row first, or pass a dc.Principal "
                "directly when identity comes from verified auth claims"
            )
        if state_of(actor) in (STATE_NEW, STATE_DIRTY):
            raise UncommittedActorError(
                f"the dc.Actor row for uid={uid} has uncommitted changes — "
                "commit the registry change before acting as it (identity "
                "must be durable before it acts)"
            )
        if not actor.human:
            if actor.sponsor is None:
                raise SponsorRequiredError(
                    f"actor uid={uid} ({actor.display or 'technical user'}) "
                    "is non-human and names no sponsor — set Actor.sponsor to "
                    "the natural person who answers for it"
                )
            sponsor = self._get_by_key_unchecked(Actor, "uid", actor.sponsor)
            if sponsor is None or not sponsor.human:
                raise SponsorRequiredError(
                    f"actor uid={uid} names sponsor {actor.sponsor}, which "
                    "does not resolve to a registered human actor — the "
                    "sponsor must be a natural person"
                )
        return actor

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path, *, durability: str = "interval",
             lock_ttl: float = 10.0, debug: bool = False,
             strict_deletes: bool = False,
             lazy_timeout: float | None = None, cache_index: bool = True,
             principal: Principal | None = None) -> Store:
        """Open (creating if needed) the store directory at ``path``.

        The directory holds ``data.sqlite`` and the lease ``used.lock`` (``StoreLockedError``).
        ``durability`` is the fsync triad: ``"commit"``, ``"interval"`` (default), ``"never"``.
        ``debug`` fingerprints live entities per commit; ``strict_deletes`` is the ADR-003 check.
        ``cache_index`` writes the index sidecar (ADR-005); ``lazy_timeout`` demotes idle handles.
        ``principal`` is the ambient identity (default ``uid=0``); reference.md, "Behaviour notes".
        """
        import sqlite3

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
            return cls(backend, lock, p2_inline=sqlite3.threadsafety < _SQLITE_SERIALIZED,
                       debug=debug, strict_deletes=strict_deletes,
                       lazy_timeout=lazy_timeout,
                       cache_dir=directory if cache_index else None,
                       principal=principal)
        except BaseException:
            lock.release()
            raise

    @classmethod
    def follower(
        cls,
        url: str,
        *,
        api_key: str | None = None,
        path: str | Path | None = None,
        client: HttpClient | None = None,
    ) -> Store:
        """Open a read replica synced from a coordinator's federation endpoint.

        The sibling of :meth:`open` (FEDERATION-WIRE-v1): it replays COMMIT-DELTA-v2 from TID 0.
        :meth:`sync` catches up and ``commit`` contributes back through the coordinator.
        ``path=None`` keeps the replica in memory; ``client`` is a test transport. The HTTP
        transport is the lazily imported ``datacrystal[follower]`` extra.
        """
        # lazy: open_follower lives in datacrystal._follower, which imports Store —
        # importing it at module load would be a cycle.
        from datacrystal._follower import open_follower

        return open_follower(url, api_key=api_key, path=path, client=client)

    @classmethod
    def _from_backend(cls, backend: StorageBackend, *, debug: bool = False,
                      strict_deletes: bool = False,
                      lazy_timeout: float | None = None,
                      lazy_clock: Callable[[], float] | None = None,
                      principal: Principal | None = None) -> Store:
        """Open over an explicit backend (tests; no lock file)."""
        return cls(backend, None, debug=debug, strict_deletes=strict_deletes,
                   lazy_timeout=lazy_timeout, lazy_clock=lazy_clock,
                   principal=principal)

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
        # Persist the built indexes to the sidecar (ADR-005 / #12) so the next
        # open loads them instead of rebuilding — outside any txn, stamped with
        # the committed watermark; a no-op if nothing was indexed this session.
        if self._index_cache is not None:
            blobs = self._index.dump_for_cache()
            reverse = self._index.dump_reverse()  # #63: cache incoming()'s index too
            if blobs or reverse is not None:
                self._index_cache.write(self._last_tid, blobs, reverse)
        self._backend.close()
        if self._lock is not None:
            self._lock.release()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public surface ------------------------------------------------------

    @property
    def root(self) -> Any:  # noqa: ANN401 - the root is whatever value the caller persisted
        self._enter()
        if self._root_oid is None:
            return None
        if self._root_holder is None:
            try:
                self._root_holder = self._load_root_holder(self._root_oid)
            except DanglingRefError as exc:
                raise DanglingRefError(
                    f"{exc} — the root graph references a deleted entity "
                    "(ADR-003 unchecked deletes); assigning store.root "
                    "replaces the root and recovers the store"
                ) from None
        return self._root_holder.value

    @root.setter
    def root(self, value: object) -> None:
        """Capture the assigned root at once: containers are tracked, new entities registered.

        Lists and dicts read from ``store.root`` are persistent containers, so in-place mutation
        reaches ``commit()``.
        Raises ``DeletedEntityError`` for a ``store.delete()``-d entity in the value: assign a
        fresh instance.
        """
        self._enter()
        if self._root_oid is None:
            holder = _Root(value=value)
            self._root_oid = self._register_graph(holder)
        else:
            holder = self._root_holder
            if holder is None:
                try:
                    holder = self._load_root_holder(self._root_oid)
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

    def store(self, obj: object) -> int:
        """Register ``obj`` and new entities reachable from it for the next commit; return its OID.

        Raises ``NotAnEntityError`` for a non-entity and ``DeletedEntityError`` for a deleted one
        (OIDs are never reused).
        """
        self._enter()
        _require_entity(obj)
        return self._register_graph(obj)

    def mark_dirty(self, obj: object) -> None:
        """Buffer an entity for the next commit explicitly, the rarely needed escape hatch.

        Attribute writes and in-place container mutation are tracked automatically.
        Raises ``NotAnEntityError``, ``DeletedEntityError`` and ``ReadDeniedError`` for a
        ``dc.Redacted`` twin (ADR-008 R14).
        """
        self._enter()
        _require_entity(obj)
        _forbid_redacted(obj)
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

    def delete(self, obj_or_cls: object, /, **unique_key: object) -> bool:
        """Delete an entity's record at the next ``commit()`` (ADR-003).

        Call it by instance, ``store.delete(m)``, or by key, ``store.delete(Mineral, qid="Q1")``.
        Return ``True`` if a deletion was buffered, else ``False``; it wins over a buffered write.
        Deletes are unchecked in v0.x: a stale reference raises ``DanglingRefError`` (ADR-003). A
        held instance is detached after commit: reads work, writes raise ``DeletedEntityError``.
        Raises ``NotAnEntityError``, ``QueryError`` (key not Unique), ``TypeError`` (call shape).
        """
        self._enter()
        if isinstance(obj_or_cls, type):
            return self._delete_by_key(obj_or_cls, unique_key)
        if unique_key:
            raise TypeError(
                "delete(entity) takes no keyword arguments — use "
                "delete(EntityClass, field=value) for key-based deletion"
            )
        return self._delete_instance(obj_or_cls)

    def _delete_by_key(self, cls: type, unique_key: dict[str, object]) -> bool:
        ti = type_info(cls)  # loud for non-entity classes
        if len(unique_key) != 1:
            raise TypeError(
                "delete(EntityClass, ...) takes exactly one unique-field "
                "keyword argument"
            )
        (field, value), = unique_key.items()
        _require_unique(cls, ti, field)
        if self._cid_by_typename.get(ti.typename) is None:
            return False
        ci = self._index.ensure(ti)
        oid = ci.unique[field].get(value)
        if oid is None or oid in self._deleted:
            return False
        return self._delete_oid(self._registry.get(oid), oid, ti)

    def _delete_instance(self, obj: object) -> bool:
        _require_entity(obj)
        _forbid_redacted(obj)
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

    def _delete_oid(self, obj: object | None, oid: int, ti: TypeInfo) -> bool:
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

    def upsert(self, obj: object, /, key: str | None = None) -> Any:  # noqa: ANN401 - the survivor is an instance of the caller's own class
        """Insert ``obj``, or merge it into the entity that already owns its natural key.

        ``key`` names a ``dc.Unique`` field, optional when the class has exactly one. The live
        survivor is returned and only changed fields are written. Matching sees committed state and
        earlier upserts of the batch, not plain ``store()``. An unreadable protected survivor
        raises ``ReadDeniedError`` (ADR-008 W4-6).
        Raises ``TypeError``, ``QueryError``, ``UniqueViolationError``.
        """
        self._enter()
        _require_entity(obj)
        _forbid_redacted(obj)
        ti = type_info(obj)
        key = _natural_key_field(obj, ti, key)
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
        _merge_data_fields(existing, obj, ti)
        return existing

    def _payload_digest(self, ti: TypeInfo, field: str, value: object) -> str | None:
        """Return the persisted payload's hex SHA-256 for unique ``field == value``, else ``None``.

        The OCC base token of ``/v1/submit`` (#155, FEDERATION-WIRE-v1). It hashes the persisted
        bytes, not a re-encode of the live entity, whose lineage or defaults can diverge. Runs on
        the owner thread inside the ``submit`` fan-in.
        See docs/design/COMMIT-DELTA-v2.md, section 8.
        """
        if self._cid_by_typename.get(ti.typename) is None:
            return None
        ci = self._index.ensure(ti)
        oid = ci.unique[field].get(value)
        if oid is None:
            return None
        rec = self._backend.load_many([oid]).get(oid)
        return hashlib.sha256(rec.payload).hexdigest() if rec is not None else None

    def _find_by_key(self, ti: TypeInfo, field: str, value: object) -> object | None:
        """Find the entity an upsert merges into: the committed unique map, then earlier upserts.

        A key freed by a buffered delete is reusable (ADR-003). The lookup self-heals against
        mid-batch key mutation or deletion.
        """
        if self._cid_by_typename.get(ti.typename) is not None:
            ci = self._index.ensure(ti)
            oid = ci.unique[field].get(value)
            if oid is not None and oid not in self._deleted:
                # The LOOKUP is read-unfenced by design (ADR-008 W3-3): a filtered lookup would
                # duplicate-then-collide. The RETURN is fenced (W4-6): a committed survivor the
                # principal cannot READ is denied, failing closed on absent labels too. The write
                # gate still fences the merge for a readable survivor.
                if ti.protected and not is_root(self.principal):
                    labels = ci.committed_labels(oid)
                    if labels is None or not can_read_row(self.principal, *labels):
                        raise ReadDeniedError(
                            f"upsert of {ti.cls.__name__} ({field}={value!r}) is "
                            "denied: a record with that natural key exists but is "
                            "not readable by the current principal. upsert is a "
                            "read-modify-write, so it fails closed on an "
                            "unreadable survivor (ADR-008 W4-6, closing the W3-3 "
                            "exposure) — only its owner or root may upsert it."
                        )
                return self._load_oid(oid)
        pending = self._pending_upserts.get((ti.cls, field, value))
        if pending is not None:
            if (state_of(pending) != STATE_DELETED
                    and getattr(pending, field) == value):
                return pending
            del self._pending_upserts[(ti.cls, field, value)]
        return None

    # Commit: _p1_capture (validate, TID, flip state clean, build delta), _run_p2 (backend I/O,
    # _p2_rollback on failure), _p3_finalize (indexes, fingerprints, last_tid), _deliver.
    def commit(self) -> int | None:
        """Atomically persist all buffered changes; return the new TID, or ``None`` if none.

        Validation runs in P1 before the TID, so a failure keeps the sequence gapless and the
        buffers. A ``WriteDeniedError`` (ADR-008 R10) is deterministic: ``committing()`` never
        retries it. Also raises ``LeaseLostError``, ``UniqueViolationError`` and
        ``DanglingRefError`` (strict deletes).
        """
        self._enter()
        if self._contribute_fn is not None:  # a follower: fan in to the coordinator
            return self._contribute()
        capture = self._p1_capture()
        if capture is None:
            return None
        try:
            self._run_p2(capture.batch)
        except BaseException:
            self._p2_rollback(capture)
            raise
        return self._p3_finalize(capture)

    def _contribute(self) -> int | None:
        """Fan a follower's buffered writes into the coordinator instead of a local commit (#153).

        A NEW entity carries ``base=None``, an edited one the SHA-256 of the payload it was read at
        (OCC). They go to ``_contribute_fn`` (``POST /v1/submit``); on success :meth:`sync` pulls
        the delta back, but only when a sync function is set.
        A reject raises ``ConflictError`` or ``SchemaSkewError`` with the buffers intact.
        See docs/design/COMMIT-DELTA-v2.md, section 8.
        """
        assert self._contribute_fn is not None
        if self._deleted:  # /v1/submit is upsert-only (FEDERATION-WIRE-v1 §3) —
            raise NotImplementedError(  # fail loud, never silently drop the delete
                "a follower cannot contribute deletes — /v1/submit is upsert-only "
                "(v0); delete on the coordinator instead"
            )
        items: list[tuple[Any, str | None]] = []
        for obj in self._new.values():
            items.append((obj, None))
        for oid, obj in self._dirty.items():
            if oid in self._new:
                continue
            rec = self._backend.load_many([oid]).get(oid)
            base = hashlib.sha256(rec.payload).hexdigest() if rec is not None else None
            items.append((obj, base))
        if not items:
            return None
        applied_tid = self._contribute_fn(items)
        self._new.clear()
        self._dirty.clear()
        if self._sync_fn is not None:
            self.sync()  # read-your-writes: pull the committed delta back
        return applied_tid

    # -- the three commit phases (ADR-001 bound decision 2) ------------------

    def _p1_capture(self) -> _Capture | None:
        """P1 (owner, await-free): discover, validate, encode — then flip the
        captured set CLEAN and re-arm the hooks. The flip happens BEFORE P2
        so a write racing P2 re-dirties through the normal hook path and
        lands in the *next* commit; a failed P2 compensates via
        :meth:`_p2_rollback`.
        """
        self._assert_lease_held()
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
        index_entries, ref_entries, gate_records = self._validate_before_tid(pending, deletes)
        encoded = self._encode_pending(pending)
        priors, delete_ops = self._read_delta_priors(pending, deletes, gate_records)
        tid = self._alloc.next_tid()
        records = [
            StoredRecord(oid=oid, cid=cid, tid=tid, payload=payload)
            for oid, cid, payload in encoded.records
        ]
        batch = self._build_batch(tid, records, encoded, deletes)
        delta = (
            build_delta(tid, records, encoded.new_types, self._root_oid, priors, delete_ops,
                        actor=self.principal.uid, at=self._clock())
            if self._consumers
            else None
        )
        flipped = self._flip_captured(pending)
        self._new.clear()
        self._dirty.clear()
        self._deleted.clear()
        self._birth_labels.clear()  # baselines consumed; a retry re-stamps
        return _Capture(tid, batch, index_entries, ref_entries, flipped, delta,
                        deletes, encoded.blob_swaps)

    def _assert_lease_held(self) -> None:
        if self._lock is not None and self._lock.lost:
            raise LeaseLostError(
                "this process lost the single-writer lease (paused too long?); "
                "another process may own the store now — refusing to write"
            )

    def _validate_before_tid(
        self,
        pending: dict[int, Any],
        deletes: list[tuple[int, TypeInfo, Any | None]],
    ) -> tuple[_IndexEntries, _RefEntries, dict[int, StoredRecord]]:
        """Run every P1 check that must reject before the TID exists; return what P2 and P3 reuse.

        Unique, sorted-temporal, dangling-delete and write-gate failures all raise here, so a
        rejected commit keeps the TID sequence gapless (invariant 5).
        """
        index_entries: _IndexEntries = []
        prot_pending: list[tuple[int, Any, TypeInfo]] = []
        for oid, obj in pending.items():
            ti = type_info(obj)
            if ti.protected:  # collected for the write gate — free in this loop
                prot_pending.append((oid, obj, ti))
            relevant = {s.name for s in ti.specs if s.indexed or s.unique or s.sorted}
            if relevant:
                index_entries.append(
                    (oid, ti, {name: getattr(obj, name) for name in relevant})
                )
        prot_deletes = [(oid, ti) for oid, ti, _ in deletes if ti.protected]
        # #110: the eager dangling-ref check (debug/strict_deletes) needs the reverse index built,
        # so force it before the ref-harvest gate; unarmed, the gate below stays lazy (spec §5).
        dangling_check_armed = (self._debug or self._strict_deletes) and bool(deletes)
        if dangling_check_armed:
            self._index.ensure_reverse()
        # #20: harvest outgoing refs for the reverse index — only when it is
        # already built (spec §5: an unwatched store pays nothing for it).
        ref_entries: _RefEntries = (
            [(oid, self._harvest_live_refs(obj)) for oid, obj in pending.items()]
            if self._index.reverse_built else []
        )
        # #110: name any surviving record still pointing at a just-deleted OID,
        # BEFORE the TID is allocated (a strict raise must leave the sequence
        # gapless, invariant 5; validation precedes allocation, same as encoding
        # and unique checks above).
        if dangling_check_armed:
            self._check_dangling_deletes(deletes, ref_entries)
        self._index.check_unique(index_entries, deleted=set(self._deleted))
        # #106 / ADR-004 §4: reject a SortedIndex datetime field mixing naive +
        # aware values BEFORE the TID is allocated (gapless sequence, invariant 5)
        # — a mixed sorted run would raise a bare TypeError deep in bisect/insort.
        self._index.check_sorted_temporal(index_entries)
        # The write gate (ADR-008, W2-5) — the consistency-before-commit family:
        # a denial rejects like a unique violation, strictly pre-TID, and BEFORE
        # the encode loop (a denied commit must not stream-hash blob sources).
        # Runs only when something protected is buffered — an all-unprotected
        # commit does literally zero gate work (W2-9's structural gate pins it).
        gate_records: dict[int, StoredRecord] = (
            self._check_write_gate(prot_pending, prot_deletes)
            if prot_pending or prot_deletes else {}
        )
        return index_entries, ref_entries, gate_records

    def _encode_pending(self, pending: dict[int, Any]) -> _EncodedBatch:
        """Encode every buffered entity to its record payload, collecting types and blobs.

        Encoding can reject values (e.g. ints beyond msgpack's 64-bit range), so it runs BEFORE
        the TID allocation: a rejected commit consumes no TID and the buffers stay intact for a
        fixed-up retry (gapless sequence, invariant 5).
        """
        out = _EncodedBatch()
        blob_sink = partial(self._sink_blob, out=out)
        for oid, obj in pending.items():
            ti = type_info(obj)
            cid = self._cid_for(ti, out.new_types)
            values = [getattr(obj, name) for name in ti.field_names]
            blob_positions = self._blob_positions(ti)
            payload = encode_payload(
                values, self._oid_for_encode,
                blob_positions=blob_positions,
                blob_sink=blob_sink if blob_positions else None,
            )
            out.records.append((oid, cid, payload))
            _note_streamed_swaps(obj, ti, values, blob_positions, payload, out.blob_swaps)
        return out

    def _sink_blob(self, value: object, *, out: _EncodedBatch) -> tuple[int, int, bytes]:
        """Allocate a blob OID for a ``dc.Blob`` field value and queue its bytes (ADR-007 / #82).

        The sink runs during encoding, before the TID exists, so it records ``(oid, size, hash,
        data)`` now and the StoredBlobs are stamped with this commit's tid later.
        """
        blob_oid = self._alloc.next_oid()
        if isinstance(value, BlobSource):
            # Pre-TID pass 1: hash + length-check WITHOUT holding the bytes
            # whole. A size mismatch raises here, before the TID is taken →
            # the sequence stays gapless (invariant 5). P2 reads the source
            # AGAIN to fill the cell (hence BlobSource must be re-readable).
            digest = _hash_blob_source(value)
            out.stream_data.append((blob_oid, value.size, digest, value.open_chunks))
            return blob_oid, value.size, digest
        if isinstance(value, (bytes, bytearray)):
            data = bytes(value)
            digest = hashlib.sha256(data).digest()
            out.blob_data.append((blob_oid, len(data), digest, data))
            return blob_oid, len(data), digest
        raise TypeError(
            f"a dc.Blob field accepts bytes or a dc.BlobSource, not "
            f"{type(value).__name__}"
        )

    def _read_delta_priors(
        self,
        pending: dict[int, Any],
        deletes: list[tuple[int, TypeInfo, Any | None]],
        gate_records: dict[int, StoredRecord],
    ) -> tuple[dict[int, bytes], list[tuple[int, int, bytes]]]:
        """Read the last durable payload of every persisted record for the delta stream.

        COMMIT-DELTA-v1 §3: O(delta) and only while consumers watch, before the TID allocation (a
        failed read consumes no TID). Deletes always have a persisted record, and their tombstones
        carry it as prior (spec §3.1).
        """
        priors: dict[int, bytes] = {}
        delete_ops: list[tuple[int, int, bytes]] = []
        if not self._consumers:
            return priors, delete_ops
        persisted_oids = [oid for oid in pending if oid not in self._new]
        want = persisted_oids + [oid for oid, _, _ in deletes]
        # Merge with the write gate's loads: a protected prior is read
        # exactly once even when consumers watch. The `if self._consumers:`
        # guard itself stays — spec §5, an unwatched store pays nothing
        # for DELTA priors (the gate pays only for protected records).
        missing = [oid for oid in want if oid not in gate_records]
        prior_records = dict(gate_records)
        if missing:
            prior_records.update(self._backend.load_many(missing))
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
        return priors, delete_ops

    def _build_batch(
        self,
        tid: int,
        records: list[StoredRecord],
        encoded: _EncodedBatch,
        deletes: list[tuple[int, TypeInfo, Any | None]],
    ) -> CommitBatch:
        return CommitBatch(
            tid=tid,
            records=records,
            new_types=encoded.new_types,
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
            blobs=[
                StoredBlob(oid=boid, tid=tid, size=size, hash=h, data=data)
                for boid, size, h, data in encoded.blob_data
            ],
            blob_streams=[
                StreamedBlob(oid=boid, tid=tid, size=size, hash=h, open_chunks=oc)
                for boid, size, h, oc in encoded.stream_data
            ],
        )

    def _flip_captured(self, pending: dict[int, Any]) -> list[tuple[int, Any, int]]:
        """Flip the captured set to CLEAN and register it; return each prior state."""
        flipped: list[tuple[int, Any, int]] = []
        for oid, obj in pending.items():
            ti = type_info(obj)
            if ti.frozen:
                _bind_frozen_containers(obj, ti)
            flipped.append((oid, obj, state_of(obj)))
            set_state(obj, STATE_CLEAN)
            self._registry.add(oid, obj)
        return flipped

    def _prior_labels(self, rec: StoredRecord) -> _Labels:
        """Return the four ``_dc_`` label values of a prior payload, decoded without entities.

        A cid whose shape lacks the columns predates protection and gets the shared R7 legacy fill,
        never the R6 birth defaults (those read legacy write floors as VIEWER).
        """
        pos = self._perm_positions.get(rec.cid, _PERM_POS_UNSET)
        if pos is _PERM_POS_UNSET:
            persisted = self._persisted_fields.get(rec.cid, [])
            try:
                pos = cast(
                    "tuple[int, int, int, int]",
                    tuple(persisted.index(n) for n in PERM_FIELDS),
                )
            except ValueError:
                pos = None  # pre-protection lineage → R7
            self._perm_positions[rec.cid] = pos
        if pos is None:
            owner, groups, read_floor, write_floor = (
                PERM_LEGACY_FILLS[n]() for n in PERM_FIELDS
            )
            return owner, groups, read_floor, write_floor
        values = decode_payload(rec.payload)
        return (values[pos[0]], list(values[pos[1]]), values[pos[2]], values[pos[3]])

    def _prior_actor_memberships(self, rec: StoredRecord) -> dict[int, int]:
        """Return the prior ``Actor.memberships`` map of a persisted payload, without entities.

        Used by the F1 re-level ceiling, so an unchanged membership is never re-cleared (ADR-008
        R8). A cid whose shape predates the field decodes as ``{}``: every entry reads as newly
        granted, which fails safe.
        """
        persisted = self._persisted_fields.get(rec.cid, [])
        try:
            idx = persisted.index("memberships")
        except ValueError:
            return {}
        raw = decode_payload(rec.payload)[idx]
        return {int(g): int(lvl) for g, lvl in dict(raw).items()}

    def _check_write_gate(
        self,
        prot_pending: list[tuple[int, Any, TypeInfo]],
        prot_deletes: list[tuple[int, TypeInfo]],
    ) -> dict[int, StoredRecord]:
        """Enforce the ADR-008 write fence in P1, before the TID (R10); return the priors loaded.

        Every buffered protected write must clear the record's current write floor; every changed
        floor must be at most the committer's authority (R8). Root bypasses (R9), decided from the
        principal alone; root commits still stamp. Denial raises ``WriteDeniedError`` with the
        buffers intact; ``committing()`` never retries it.
        See docs/design/ADR-008-permissions-contract.md, "Birth labels and the write fence".
        """
        p = self.principal
        if is_root(p):
            return {}
        persisted_oids = [oid for oid, _, _ in prot_pending if oid not in self._new]
        persisted_oids += [oid for oid, _ in prot_deletes]
        records = self._backend.load_many(persisted_oids) if persisted_oids else {}
        labels = {oid: self._prior_labels(rec) for oid, rec in records.items()}
        for oid, obj, ti in prot_pending:
            prior = labels.get(oid)
            if prior is None:
                base = self._gate_new_record(p, oid, obj, ti)
            else:
                base = _gate_persisted_record(p, oid, obj, ti, prior)
            _gate_label_changes(p, oid, obj, ti, base)
            if ti.cls is Actor:
                prior_m = (self._prior_actor_memberships(records[oid])
                           if prior is not None else {})
                _gate_actor_memberships(p, obj, prior_m)
        for oid, ti in prot_deletes:
            _gate_delete(p, oid, ti, labels[oid])
        return records

    def _gate_new_record(self, p: Principal, oid: int, obj: LabelColumns, ti: TypeInfo) -> _Labels:
        """Gate a NEW protected record and return its baseline: the birth/inherited labels.

        There is no floor to clear. The anonymous case is normally refused at the stamp site
        (W2-2); this closes the remaining paths. The baseline is not empty: inheritance is library
        policy, so the principal answers only for what it staged on top.
        """
        if p.uid == 0:
            _deny_write(p, f"cannot create a protected {ti.cls.__name__} as the "
                           "anonymous principal (a record nobody owns must not "
                           "exist, R6/R7a)")
        base = self._birth_labels.get(oid, (obj._dc_owner, [], VIEWER, VIEWER))
        if obj._dc_owner != base[0]:
            # F2 (ADR-008 R6): the birth owner is the acting uid stamped in
            # _stamp_perm_labels. Setting `_dc_owner` between store() and commit() would
            # forge it, planting a record in a victim's owner-only space or an owner=0
            # orphan; this pins ownership at birth like the persisted branch does.
            _deny_write(p, f"cannot stamp {ti.cls.__name__} oid={oid} with owner "
                           f"{obj._dc_owner}: a new record's owner is the acting "
                           f"principal ({base[0]}), never assigned by hand (R6)")
        return base

    def _run_p2(self, batch: CommitBatch) -> None:
        """P2: backend I/O on bytes only, off the owner thread."""
        if self._p2_inline:
            self._backend.apply(batch)
        else:
            self._io_executor().submit(self._backend.apply, batch).result()

    def _p2_rollback(self, capture: _Capture) -> None:
        """Compensate a failed P2: re-buffer the captured set and reuse the TID (invariant 5).

        A failed P2 was never durable. An entity that a racing write already re-buffered is left as
        that write made it.
        """
        self._alloc._next_tid = capture.tid  # gapless TID reuse, invariant 5
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
        self._index.apply_reverse(capture.ref_entries)  # #20 reverse-ref fold
        self._index.apply_deletes([(oid, ti) for oid, ti, _ in capture.deletes])
        self._index.remove_reverse([oid for oid, _, _ in capture.deletes])  # #20-B delete-fold
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
        # A streamed-write source has now been consumed and durably stored; swap
        # the live field from the opaque BlobSource to a readable BlobHandle so
        # .bytes()/open_blob work without a reopen (ADR-007 §4). Untracked
        # (set_field) — this is the post-commit rehydration, not a mutation. A
        # plain bytes value is left as-is (it stays readable as itself).
        for obj, field, boid, size, digest in capture.blob_swaps:
            # engine-only BlobHandle constructor (users never call _bind)
            handle = BlobHandle._bind(boid, size, digest, self)
            set_field(obj, field, handle)
        if self._debug:
            # A blob-bearing entity's stored payload carries the real blob OID but the sweep
            # recomputes a content-only (oid-0) descriptor, so fingerprint it through the same
            # _fingerprint_payload epoch (a BlobSource was swapped to a BlobHandle above). Non-blob
            # records keep the cheap stored-crc.
            flipped_by_oid = {oid: obj for oid, obj, _ in capture.flipped}
            for rec in capture.batch.records:
                obj = flipped_by_oid.get(rec.oid)
                if obj is not None and self._blob_positions(type_info(obj)):
                    self._fingerprints[rec.oid] = _crc(
                        self._fingerprint_payload(obj, type_info(obj))
                    )
                else:
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
        never holds writes hostage to one.
        """
        tid = delta["tid"]
        for consumer in list(self._consumers):
            try:
                consumer.apply(delta)
                if consumer.watermark != tid:
                    raise DataCrystalError(
                        f"consumer applied tid {tid} but reports watermark "
                        f"{consumer.watermark} — it violates COMMIT-DELTA-v1 §4.3"
                    )
            except Exception as exc:  # noqa: BLE001 - a raising consumer must never break a durable commit
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

    def _check_dangling_deletes(
        self,
        deletes: list[tuple[int, TypeInfo, Any | None]],
        ref_entries: list[tuple[int, set[int]]],
    ) -> None:
        """Name any surviving record that still points at an OID this commit deletes (#110).

        ``strict_deletes=True`` raises, ``debug=True`` alone warns; it runs in P1 before the TID
        (invariant 5). The flagged set equals ``incoming(dead)`` after this commit's P3 folds:
        pre-commit referrers, reconciled with this commit's refs, minus co-deleted ones.
        Reconstruction: docs/design/ADR-003-delete-semantics.md, "Dev-time bridge".
        """
        deleted_oids = {oid for oid, _, _ in deletes}
        rev = self._index.ensure_reverse()  # pre-commit map (forced built by P1)
        pending_targets = dict(ref_entries)  # referrer oid → its targets THIS commit
        # deleted oid → its post-fold surviving referrers
        flagged: dict[int, set[int]] = {}
        for dead in deleted_oids:
            referrers = set(rev.get(dead, ()))  # pre-commit referrers
            for referrer, targets in pending_targets.items():
                if dead in targets:
                    referrers.add(referrer)  # NEW/DIRTY referrer points here now
                else:
                    referrers.discard(referrer)  # DIRTY referrer dropped the ref
            referrers -= deleted_oids  # a co-deleted referrer is not a dangle
            if referrers:
                flagged[dead] = referrers
        if not flagged:
            return
        typenames = self._typenames_for(
            {r for refs in flagged.values() for r in refs}
        )
        deleted_typename = {oid: ti.typename for oid, ti, _ in deletes}
        parts: list[str] = []
        for dead, referrers in flagged.items():
            named = ", ".join(
                f"{typenames.get(r, '?')} (oid {r})" for r in sorted(referrers)
            )
            parts.append(
                f"{deleted_typename.get(dead, '?')} (oid {dead}) is still "
                f"referenced by {named}"
            )
        message = (
            "this commit deletes records other surviving records still point at "
            "(ADR-003 unchecked delete: following such a stale reference later "
            "raises DanglingRefError). " + "; ".join(parts)
        )
        if self._strict_deletes:
            raise DanglingRefError(message)
        warnings.warn(DanglingDeleteWarning(message), stacklevel=4)

    def _typenames_for(self, oids: set[int]) -> dict[int, str]:
        """Resolve OID → typename for a set of referrer OIDs without hydrating.
        A same-commit NEW/DIRTY referrer is not yet in the registry or on disk,
        so consult the live commit buffers first, then a registry instance, then
        one ``load_many`` via the persisted cid lineage. Best-effort — an OID
        with no record (race) is simply absent (the message shows '?').
        """
        out: dict[int, str] = {}
        unresolved: list[int] = []
        for oid in oids:
            live = self._new.get(oid) or self._dirty.get(oid) or self._registry.get(oid)
            if live is not None:
                out[oid] = type_info(live).typename
            else:
                unresolved.append(oid)
        if unresolved:
            for oid, rec in self._backend.load_many(unresolved).items():
                name = self._typename_by_cid.get(rec.cid)
                if name is not None:
                    out[oid] = name
        return out

    def _sweep_untracked(self) -> None:
        """debug=True: warn about (and rescue) CLEAN entities whose
        re-encoded record no longer matches their last known fingerprint —
        a mutation slipped past the hooks (KICKOFF risk 1).
        """
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
                payload = self._fingerprint_payload(obj, ti)
                changed = _crc(payload) != expected
            except BaseException:  # noqa: BLE001 - any failure to re-encode proves an untracked change
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
        """Run ``fn()`` on the owner thread and return a Future for its result.

        The sanctioned cross-thread write path (ADR-001): the owner runs pending closures on its
        next store call. A result carrying a live entity fails the Future with
        ``EntityEscapeError``: return plain data.
        Raises ``StoreClosedError`` if the store is closed.
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
        returns the number of submissions run.

        Raises:
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._guard()
        count = self._pump()
        if self._lazyman is not None:  # an explicit boundary sweeps too
            self._lazyman.maybe_sweep()
        return count

    def _pump(self) -> int:
        """Drain the submit() queue on the owner. Called from the public API
        boundaries; reentrant calls (a submission touching the store) no-op.
        """
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
        # Identity isolation (epic #168 W1): the pump piggybacks on owner calls that may sit inside
        # an acting_as scope. A queued foreign-thread closure must not inherit it (its commit would
        # stamp another user's actor): queued work runs as the AMBIENT principal, and a closure
        # that should act as someone opens its own acting_as.
        token = self._acting.set(())
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 - every error, even a BaseException, is handed to the waiting future
            future.set_exception(exc)
            return
        finally:
            self._acting.reset(token)
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
        """Attach a COMMIT-DELTA-v2 consumer that gets each later delta in TID order, once durable.

        The consumer's watermark must equal ``store.last_tid``: deltas are not retained (spec §5),
        so a consumer behind rebuilds from ``store.snapshot()``, and one ahead means a stale
        sidecar (fitness #13).
        Raises ``DeltaGapError`` for either mismatch and ``DataCrystalError`` if already attached.
        See docs/design/COMMIT-DELTA-v2.md, section 8.
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
        """Detach a previously attached delta consumer.

        Raises:
            DataCrystalError: this consumer is not currently attached.
            WrongThreadError: called from a thread other than the store's
                owner (ADR-001).
            StoreClosedError: the store has already been closed.
        """
        self._enter()
        for i, existing in enumerate(self._consumers):
            if existing is consumer:
                del self._consumers[i]
                return
        raise DataCrystalError("this consumer is not attached")

    def snapshot(self, *, principal: Principal | None = None) -> Snapshot:
        """Return a frozen read-only view of the committed state at the watermark, from any thread.

        Safe during owner commits (ADR-002); close promptly, it pins a sqlite WAL read transaction.
        Bound to one principal (ADR-008 R15): ``None`` takes the acting principal in effect now.
        :meth:`Snapshot.for_principal` derives a sibling handle for another identity in O(1). Views
        are immutable :class:`EntityView` data; references are :class:`Ref` tokens.
        """
        # Deliberately NOT _enter(): no owner guard, no piggyback work.
        # The read view isolates itself from the live engine entirely.
        if self._closed:
            raise StoreClosedError("this store has been closed")
        bound = principal if principal is not None else self.principal
        core = _SnapshotCore(self._backend.read_view())
        return Snapshot(core, bound)

    def open_blob(self, entity: object, field: str) -> BinaryIO:
        """Open a committed ``dc.Blob`` field as a binary stream (ADR-007 §3).

        Return an ``io.BufferedReader`` whose range reads pull only the spanned bytes. The stream
        rides a private snapshot view (ADR-002): readable from another thread; close it. The call
        is owner-confined (it reads the live ``entity``); the stream is not.
        Raises ``QueryError``, ``TypeError`` (not a Blob), ``ValueError`` (``None`` or uncommitted
        ``BlobSource``).
        """
        self._enter()
        ti = type_info(entity)
        spec = ti.spec(field)
        if spec is None:
            raise QueryError(f"{ti.typename} has no field {field!r}")
        if not spec.blob:
            raise TypeError(
                f"{ti.typename}.{field} is not a dc.Blob field — open_blob() "
                "streams out-of-line blob values only"
            )
        value = getattr(entity, field)
        if value is None:
            raise ValueError(f"{ti.typename}.{field} is None — no blob to open")
        if isinstance(value, BlobHandle):
            if ti.protected:  # ADR-008 W4-3: a blob is fenced with its owner
                oid = oid_of(entity)
                if oid is not None:
                    self._blob_read_gate(oid)
            view = self._backend.read_view()
            return view.open_blob_stream(value.blob_oid, on_close=view.close)
        if isinstance(value, (bytes, bytearray)):
            # Uncommitted raw assignment: already whole in RAM, stream a copy.
            return io.BytesIO(bytes(value))
        if isinstance(value, BlobSource):
            raise ValueError(  # noqa: TRY004 - ValueError is the documented contract
                f"{ti.typename}.{field} holds an uncommitted dc.BlobSource — "
                "commit() before opening a streamed-write blob for reading"
            )
        raise TypeError(
            f"{ti.typename}.{field} holds an unexpected blob value "
            f"{type(value).__name__!r}"
        )

    def get(self, cls: type, **unique_key: object) -> Any | None:  # noqa: ANN401 - an instance of whichever class the caller names
        """Return one entity by unique secondary key, or ``None``: ``store.get(Mineral, qid=...)``.

        The lookup reflects committed state; the returned instance is the live one, buffered edits
        included.
        On a ``protected=True`` class a found-but-unreadable key is also ``None`` (ADR-008 R12).
        Raises ``QueryError`` for a non-Unique keyword, ``TypeError`` unless exactly one is given.
        """
        self._enter()
        if len(unique_key) != 1:
            raise TypeError("get() takes exactly one unique-field keyword argument")
        ti = type_info(cls)
        (field, value), = unique_key.items()
        _require_unique(cls, ti, field)
        if self._cid_by_typename.get(ti.typename) is None:
            return None
        ci = self._index.ensure(ti)
        oid = ci.unique[field].get(value)
        if oid is None:
            return None
        if ti.protected:
            p = self.principal
            if not is_root(p):
                labels = ci.committed_labels(oid)
                if labels is None or not can_read_row(p, *labels):
                    return None  # found-but-denied ≡ absent (ADR-008 R12)
        return self._load_oid(oid)

    def get_many(self, refs: Iterable[Any] | type, /,
                 **unique_key: Iterable[object]) -> list[Any]:
        """Hydrate many entities in one storage round-trip (SDA delta 5).

        Shapes: ``get_many(mixed)`` (OIDs, ``Lazy``, ``Ref``, entities) and ``get_many(Mineral,
        qid=[...])``. The key form is aligned with the values, ``None`` where absent or denied
        (R12). The iterable form is a DEREF surface (R14): a denied item returns a ``Redacted``
        twin.
        Raises ``NotAnEntityError``, ``QueryError``, ``TypeError`` for bad items or shapes.
        """
        self._enter()
        if unique_key:
            if not isinstance(refs, type):
                raise TypeError(
                    "get_many(EntityClass, field=values) needs an @entity "
                    "class as its first argument"
                )
            return self._get_many_by_key(refs, unique_key)
        items: list[Any] = list(refs)  # a class without kwargs falls through
        wanted = _wanted_oids(items)
        missing = [oid for oid in wanted if self._registry.get(oid) is None]
        cache = self._backend.load_many(missing) if missing else {}
        return [self._deref_item(item, cache) for item in items]

    def _deref_item(self, item: object, cache: dict[int, StoredRecord]) -> object:
        """Resolve one ``get_many`` item at the R14 checkpoint; an entity passes through."""
        if isinstance(item, Lazy):
            lazy = cast("Lazy[Any]", item)
            oid = lazy.oid
            if lazy.loaded or oid is None:
                return lazy.get()
            return self._load_oid_deref(oid, cache)
        if isinstance(item, int):
            return self._load_oid_deref(item, cache)
        if isinstance(item, Ref):
            return self._load_oid_deref(item.oid, cache)
        return item

    def _get_many_by_key(self, cls: type, unique_key: dict[str, Iterable[object]]) -> list[Any]:
        if len(unique_key) != 1:
            raise TypeError(
                "get_many() takes exactly one unique-field keyword argument"
            )
        ti = type_info(cls)
        (field, key_values), = unique_key.items()
        _require_unique(cls, ti, field)
        values = list(key_values)
        if self._cid_by_typename.get(ti.typename) is None:
            return [None] * len(values)  # silent like get(): the miss idiom
        ci = self._index.ensure(ti)
        oids = [ci.unique[field].get(value) for value in values]
        missing = [
            oid for oid in oids
            if oid is not None and self._registry.get(oid) is None
        ]
        cache = self._backend.load_many(missing) if missing else {}
        if ti.protected:
            # get()'s twin for a bulk key lookup (ADR-008 W3-3): a denied row
            # is a None slot, exactly like a missing key (R12) — the list
            # stays aligned with `values` either way. `p`/`root` are hoisted
            # once, not per-oid.
            p = self.principal
            root = is_root(p)
            out: list[Any] = []
            for oid in oids:
                if oid is None:
                    out.append(None)
                    continue
                if not root:
                    labels = ci.committed_labels(oid)
                    if labels is None or not can_read_row(p, *labels):
                        out.append(None)  # found-but-denied ≡ absent (R12)
                        continue
                out.append(self._load_oid(oid, cache))
            return out
        return [
            None if oid is None else self._load_oid(oid, cache) for oid in oids
        ]

    def _get_many_unchecked(self, oids: Iterable[int]) -> list[Any]:
        """Hydrate committed OIDs with no read enforcement (ADR-008 W3-3), internal plumbing only.

        For callers that already filtered the OIDs under the relevant principal
        (``query``/``query_iter``, W3-2) or that must see every record whoever acts (``migrate``).
        Every public read goes through the checked ``get``/``get_many``/deref path instead.
        """
        oids = list(oids)
        missing = [o for o in oids if self._registry.get(o) is None]
        cache = self._backend.load_many(missing) if missing else {}
        return [self._load_oid(o, cache) for o in oids]

    def _get_by_key_unchecked[T](self, cls: type[T], field: str, value: object) -> T | None:
        """Return ``get()`` minus the read fence (ADR-008 W3-3), the identity-bootstrap seam.

        Used by :meth:`_resolve_registry_actor`: ``Actor`` is protected, so a fenced lookup would
        raise ``UnknownActorError`` for a session that cannot yet read its own registry row.
        Resolution is identity plumbing ("authenticate outside, remember inside"), not disclosure.
        """
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            return None
        oid = self._index.ensure(ti).unique[field].get(value)
        # the unique map of cls's type names an instance of it
        return None if oid is None else cast("T", self._load_oid(oid))

    def query(self, target: type | Condition, *, limit: int | None = None,
              offset: int = 0, order_by: object = None) -> list[Any]:
        """Return the hydrated committed entities matching ``target``, a Condition or entity class.

        ``query(Mineral)`` spells the full extent; :meth:`explain` shows the plan.
        ``limit``/``offset`` (#14) prune before hydration only on a fully-indexed query.
        ``order_by=(field, 'asc'|'desc')`` sorts the whole match set before windowing (#25). On
        ``protected=True`` candidates are intersected with the readable OIDs first (ADR-008 R12).
        Raises ``QueryError``, ``TypeError`` and ``ValueError`` (negative window).
        """
        self._enter()
        validate_window(limit, offset)
        cls, cond = query_target(target, "query")
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return []
        ci = self._index.ensure(ti)
        candidate, residual = self._candidates(ti, ci, cond, self.principal)
        if order_by is not None:
            field, descending = parse_order_by(order_by, ti)
            if residual is None:
                window = self._ordered_window(ti, ci, candidate, field, descending,
                                              limit, offset)
                return self._get_many_unchecked(window)
            objs = [o for o in self._get_many_unchecked(list(candidate))
                    if residual.evaluate(o)]
            return apply_window(_order_by_attr(objs, field, descending), limit, offset)
        if residual is None:
            # #51: take the window LAZILY — a small limit over a huge extent
            # stops after offset+limit instead of listing every candidate OID.
            return self._get_many_unchecked(window_iter(candidate, limit, offset))
        oids = list(candidate)  # a residual must consider every candidate, then window
        objs = [o for o in self._get_many_unchecked(oids) if residual.evaluate(o)]
        return apply_window(objs, limit, offset)

    def _candidates(self, ti: TypeInfo, ci: ClassIndexes, cond: Condition | None,
                    principal: Principal) -> tuple[BitMap64, Condition | None]:
        """Return the bitmap of candidate OIDs for ``cond`` and the residual left to evaluate."""
        if cond is None:
            bitmap, residual = None, None
        else:
            bitmap, residual = plan(cond, ci)
        candidate = bitmap if bitmap is not None else ci.extent
        if ti.protected:
            rb = readable_bitmap(principal, ci)
            if rb is not None:
                candidate = candidate & rb  # a NEW bitmap — never mutate ci.extent
        return candidate, residual

    def _ordered_window(self, ti: TypeInfo, ci: ClassIndexes, matched: BitMap64, field: str,
                        descending: bool, limit: int | None, offset: int) -> list[int]:
        """Return the ``(offset, limit)`` window of ``matched`` in order_by order (#25/#66).

        An indexed field windows straight from the index in O(offset+limit) when ``limit`` is set
        (#66). An un-indexed field decodes that one field for every matched OID (the honest
        O(matches) ceiling), then sorts (NULLs last, ascending-OID tiebreak) and slices.
        """
        if field in ci.eq:
            return windowed_index_order(ci, matched, field, descending, limit, offset)
        values = {oid: row[field] for oid, row in self._iter_raw(ti, list(matched))}
        ordered = order_by_values(matched, values.__getitem__, descending)
        return apply_window(ordered, limit, offset)

    def incoming(self, entity: object) -> list[Any]:
        """Return every committed entity that references ``entity``, for backlinks (ROADMAP 8).

        Answered from a rebuildable reverse index (invariant 11), built on first call. A deleted
        target keeps its postings: ``incoming(dead)`` names dangling referrers (ADR-003). A
        discovery surface (R12): an unreadable ``protected=True`` referrer is absent.
        :meth:`Snapshot.incoming` gives the same at a pinned watermark.
        """
        self._enter()
        if not is_entity(entity):
            raise NotAnEntityError(
                f"incoming() takes an @entity instance, got {type(entity).__name__}"
            )
        oid = oid_of(entity)
        if oid is None:
            return []  # never stored → nothing can reference it yet
        referrers = self._index.ensure_reverse().get(oid)
        if referrers is None:
            return []
        oids = list(referrers)
        p = self.principal
        if is_root(p):
            return self._get_many_unchecked(oids)
        survivors, to_load = self._readable_registered(oids, p)
        cache = self._backend.load_many(to_load) if to_load else {}
        survivors += self._readable_unregistered(to_load, cache, p)
        survivors.sort()  # deterministic ascending-OID order
        return [self._load_oid(o, cache) for o in survivors]

    def _readable_registered(self, oids: list[int], p: Principal) -> tuple[list[int], list[int]]:
        """Split referrer OIDs into the readable ones already live and those still to load."""
        survivors: list[int] = []
        to_load: list[int] = []
        for oid in oids:
            obj = self._registry.get(oid)
            if obj is None:
                to_load.append(oid)
                continue
            ti = type_info(obj)
            if not ti.protected:
                survivors.append(oid)
                continue
            labels = self._index.ensure(ti).committed_labels(oid)
            if labels is not None and can_read_row(p, *labels):
                survivors.append(oid)
        return survivors, to_load

    def _readable_unregistered(self, to_load: list[int], cache: dict[int, StoredRecord],
                               p: Principal) -> list[int]:
        """Return the loaded referrers the principal may read, judged on their committed labels."""
        survivors: list[int] = []
        for oid in to_load:
            rec = cache.get(oid)
            if rec is None:
                continue  # racing delete fold — the posting outlived the record (ADR-003)
            ti = self._ti_for_cid(rec.cid)
            if ti.protected:
                owner, groups, read_floor, _write_floor = self._prior_labels(rec)
                if not can_read_row(p, owner, groups, read_floor):
                    continue
            survivors.append(oid)
        return survivors

    def verify(self) -> list[tuple[str, int]]:
        """Return the ``(typename, oid)`` pairs that do not decode to their live class (#26 (c)).

        A read-only check: it never raises on a bad record (the owner guard still applies). Bad
        means a removed-then-re-added field without default or ``Glue``, an undefined type, a
        raising ``Glue``, or a corrupt record. An empty list means the store reads cleanly; run it
        before :meth:`migrate`.
        """
        self._enter()
        failures: list[tuple[str, int]] = []
        for typename in list(self._cids_by_typename):
            ti = TYPES_BY_NAME.get(typename)
            for cid in list(self._cids_by_typename.get(typename, ())):
                for rec in self._backend.scan_type(cid):
                    try:
                        if ti is None:
                            raise SchemaMismatchError(
                                f"no live @entity class named {typename!r} here"
                            )
                        self._decode_check(rec, ti)
                    except (SchemaMismatchError, ValueError, KeyError,
                            TypeError, IndexError):
                        failures.append((typename, rec.oid))
        return failures

    def migrate(self, *, batch: int = 10_000) -> int:
        """Rewrite every record to the newest shape of its live class; return how many (#26 (c)).

        The offline counterpart to read-time ``RenamedFrom``/``Glue``: stale records are
        re-committed under the current cid, so migrated values become real, indexable columns. It
        is additive, idempotent and crash-safe, committing in ``batch`` chunks. The write gate
        applies per chunk: legacy records carry the R7 ``ADMIN`` floor (migrate as admin or root).
        Raises ``SchemaMismatchError`` (run :meth:`verify` first) and ``WriteDeniedError``.
        """
        self._enter()
        migrated = 0
        for typename in list(self._cids_by_typename):
            ti = TYPES_BY_NAME.get(typename)
            if ti is None:
                continue  # no live class — can't re-encode; verify() reports it
            # the current-shape cid is the one whose persisted fields match the
            # LIVE class — NOT _cid_by_typename (the latest *persisted* cid, which
            # before any new-shape commit is still the old shape).
            target = list(ti.field_names)
            lineage = self._cids_by_typename.get(typename, ())
            current = next(
                (c for c in lineage if self._persisted_fields.get(c) == target), None
            )
            stale = [c for c in lineage if c != current]
            if not stale:
                continue
            oids = [rec.oid for c in stale for rec in self._backend.scan_type(c)]
            for start in range(0, len(oids), batch):
                # write plumbing (ADR-008 W3-3 bypass): migrate must see and
                # re-persist EVERY stale record regardless of the migrating
                # principal's read standing — labels ride verbatim, and the
                # commit-P1 write gate still fences each chunk.
                chunk = self._get_many_unchecked(oids[start:start + batch])
                for obj in chunk:
                    self.mark_dirty(obj)
                self.commit()
                migrated += len(chunk)
        return migrated

    def explain(self, target: type | Condition) -> QueryPlan:
        """Return the deterministic plan for ``target``: bitmap parts and the Python residual.

        ``query()`` hydrates at most ``plan.candidates``; ``count()``/``pluck()`` decode them. Two
        rules, no optimizer: ``==``/``.in_()`` on ``dc.Index``/``dc.Unique`` are bitmaps. The first
        use builds the class indexes (a one-time O(extent) cost). On ``protected=True`` the counts
        cover the readable subset only (R12).
        """
        self._enter()
        cls, cond = query_target(target, "explain")
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return QueryPlan(
                ti.typename, None if cond is None else repr(cond),
                False, None, 0, 0,
            )
        ci = self._index.ensure(ti)
        readable = readable_bitmap(self.principal, ci) if ti.protected else None
        return explain_plan(ti.typename, ci, cond, readable=readable)

    def count(self, target: type | Condition) -> int:
        """Count the committed entities matching ``target`` without hydrating any.

        ``count(Mineral)`` is the extent's cardinality, a bitmap-answerable condition is bitmap
        cardinality, and a residual decodes candidates without building entities. Reads committed
        state, unlike :meth:`query`. On ``protected=True`` it counts the principal's readable
        subset (ADR-008 R12).
        """
        self._enter()
        cls, cond = query_target(target, "count")
        ti = type_info(cls)
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return 0
        ci = self._index.ensure(ti)
        if cond is None:
            if ti.protected:
                rb = readable_bitmap(self.principal, ci)
                # rb is already a subset of ci.extent — no intersect needed.
                return len(ci.extent) if rb is None else len(rb)
            return len(ci.extent)
        bitmap, residual = plan(cond, ci)
        candidate = bitmap if bitmap is not None else ci.extent
        if ti.protected:
            rb = readable_bitmap(self.principal, ci)
            if rb is not None:
                candidate = candidate & rb  # a NEW bitmap — never mutate ci.extent
        if residual is None:
            return len(candidate)
        oids = list(candidate)
        raw_cond = _raw_condition(residual)
        matches = 0
        for _oid, row in self._iter_raw(ti, oids):
            if raw_cond.evaluate(_RawView(row)):
                matches += 1
        return matches

    def pluck(self, target: type | Condition, *fields: str,
              limit: int | None = None, offset: int = 0,
              order_by: object = None) -> list[Any]:
        """Project fields without constructing entities, the decode-level column read (ROADMAP 4).

        ``pluck(Mineral, "name")`` returns values, ``pluck(cond, "a", "b")`` tuples; refs are
        ``Ref`` tokens. It reads committed state like :meth:`count`; windowing and ``order_by``
        follow :meth:`query`. On ``protected=True`` only readable rows are decoded (ADR-008 R12).
        Raises ``QueryError``, ``TypeError`` (no fields) and ``ValueError``.
        """
        self._enter()
        validate_window(limit, offset)
        if not fields:
            raise TypeError("pluck() takes at least one field name")
        cls, cond = query_target(target, "pluck")
        ti = type_info(cls)
        _require_persisted_fields(cls, ti, fields)
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return []
        ci = self._index.ensure(ti)
        candidate, residual = self._candidates(ti, ci, cond, self.principal)
        raw_cond = _raw_condition(residual) if residual is not None else None
        project = _projector(fields)
        if order_by is not None:
            ofield, descending = parse_order_by(order_by, ti)
            if residual is None and ofield in ci.eq:
                # index-ordered (no decode of the sort field), short-circuiting to
                # O(offset+limit) when limit is set (#66), then decode+project only
                # the windowed OIDs.
                oids = windowed_index_order(ci, candidate, ofield, descending, limit, offset)
                return [project(row) for _oid, row in self._iter_raw(ti, oids)]
            # honest O(matches): decode every candidate, filter, order by the
            # sort field's decoded value, then window + project.
            rows = _order_rows(
                list(self._matching_rows(ti, list(candidate), raw_cond)), ofield, descending
            )
            return [project(row) for _oid, row in apply_window(rows, limit, offset)]

        windowed_early = residual is None
        # #51: no residual → take the window lazily (O(offset+limit)); a residual
        # must decode every candidate before the window can be applied.
        oids = window_iter(candidate, limit, offset) if windowed_early else list(candidate)
        out = [project(row) for _oid, row in self._matching_rows(ti, oids, raw_cond)]
        return out if windowed_early else apply_window(out, limit, offset)

    def _matching_rows(self, ti: TypeInfo, oids: list[int],
                       raw_cond: Condition | None) -> Iterator[tuple[int, dict[str, Any]]]:
        """Yield the decoded rows of ``oids`` that satisfy the raw residual, if there is one."""
        for oid, row in self._iter_raw(ti, oids):
            if raw_cond is None or raw_cond.evaluate(_RawView(row)):
                yield oid, row

    def query_iter(self, target: type | Condition) -> Iterator[Any]:
        """Iterate the committed result set of :meth:`query` chunk by chunk, in bounded memory.

        It hydrates exactly like ``query()`` and reads committed state at iteration time. The peak
        live set is O(chunk); the owner guard is re-asserted on every pull. On ``protected=True``
        the principal in effect at call time is captured (ADR-008 W3-2), so a stream opened inside
        ``acting_as(agent)`` keeps that view after the scope exits.
        """
        self._enter()
        cls, cond = query_target(target, "query_iter")
        ti = type_info(cls)
        principal = self.principal  # captured NOW (ADR-008 W3-2), not at iteration time
        return self._query_iter_stream(ti, cond, principal)

    def _query_iter_stream(self, ti: TypeInfo, cond: Condition | None,
                           principal: Principal) -> Iterator[Any]:
        if self._cid_by_typename.get(ti.typename) is None:
            self._warn_unseen(ti)
            return
        ci = self._index.ensure(ti)
        candidate, residual = self._candidates(ti, ci, cond, principal)
        oids = list(candidate)
        for start in range(0, len(oids), _RAW_CHUNK):
            self._guard()  # ADR-001: re-assert before hydrating each new chunk
            # _get_many_unchecked hydrates one chunk: candidates were filtered above under the
            # CAPTURED principal (ADR-008 W3-2), and a checked get_many() would re-check under the
            # ambient one if acting_as() has exited. Reassigning `chunk` each round lets the
            # previous chunk be collected (the O(chunk) bound).
            chunk = self._get_many_unchecked(oids[start:start + _RAW_CHUNK])
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

    def _iter_raw(self, ti: TypeInfo, oids: list[int]) -> Iterator[tuple[int, dict[str, Any]]]:
        """Decode-level row scan: committed records → field-value dicts via
        the per-cid hydration plan (additive evolution honored). Loads in
        chunks to bound peak memory; constructs no entities, touches no
        registry — the machinery behind count()/pluck() and gate #19.
        """
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
                by_name: dict[str, Any] | None = None
                row: dict[str, Any] = {}
                for spec, index, factory in hydration:
                    if index is not None:
                        row[spec.name] = values[index]
                    elif spec.glue is not None:  # #26 (b): derive from the old record
                        if by_name is None:
                            persisted = self._persisted_fields.get(rec.cid, [])
                            by_name = dict(zip(persisted, values))
                        row[spec.name] = spec.glue(by_name)
                    else:
                        row[spec.name] = factory()
                yield oid, row

    def _guard(self) -> None:
        if self._closed:
            raise StoreClosedError("this store has been closed")
        if threading.get_ident() != self._owner:
            raise WrongThreadError(_THREAD_RECIPE)

    def _enter(self) -> None:
        """Public API boundary: guard, then piggyback the work the owner
        owes — the submit() queue and the lazy-demotion sweep (ADR-001
        daemon principle: both only ever run on the owner).
        """
        self._guard()
        self._pump()
        if self._lazyman is not None:
            self._lazyman.maybe_sweep()

    def _on_first_write(self, obj: object) -> None:
        """First write to a CLEAN entity (called by the one-shot hook,
        BEFORE the mutation lands).
        """
        if threading.get_ident() != self._owner:
            raise WrongThreadError(_THREAD_RECIPE)
        if self._closed:
            raise StoreClosedError("this store has been closed")
        set_state(obj, STATE_DIRTY)
        oid = oid_of(obj)
        assert oid is not None  # CLEAN implies stamped
        self._dirty[oid] = obj

    def _register_graph(self, obj: object, walked: set[int] | None = None) -> int:
        if walked is None:
            walked = set()
        # Queue entries carry the ENQUEUING protected container (or None):
        # a fresh protected child inherits its container's groups + floors at
        # registration — write-time inheritance, ADR-008 / epic #168 W2-2.
        # Unprotected containers always pass None, so the unprotected walk
        # does no label work.
        queue: deque[tuple[Any, Any]] = deque([(obj, None)])
        while queue:
            current, container = queue.popleft()
            _require_storable(current)
            oid = oid_of(current)
            fresh = oid is None
            if oid is None:
                oid = self._adopt_unregistered(current)
            if oid in walked:
                continue
            walked.add(oid)
            if oid in self._new or oid in self._dirty:
                ti = type_info(current)
                if fresh and ti.protected:
                    self._stamp_perm_labels(current, container)
                self._queue_field_entities(current, ti, queue)
        return oid_of(obj)  # pyrefly: ignore[bad-return] - the walk above stamped obj, so it has an oid

    def _adopt_unregistered(self, entity: object) -> int:
        """Give a never-stored entity its OID, stamp it and buffer it as NEW; return the OID."""
        if type_info(entity).protected and self.principal.uid == 0:
            # R6 (derived): a record nobody owns must not be creatable
            # — refused BEFORE the entity is stamped or buffered, so a
            # doomed record never enters the buffer (commit-retry could
            # not re-stamp it; only discard() could). P1-discovered
            # children hit this pre-TID too (invariant 5).
            raise WriteDeniedError(
                f"cannot create a protected {type(entity).__name__} as "
                "the anonymous principal (uid 0) — a record nobody owns "
                "must not exist (ADR-008 R6/R7a); open the store with "
                "principal=... or wrap the write in store.acting_as(...)"
            )
        oid = self._alloc.next_oid()
        stamp(entity, oid, self, state_of(entity))
        self._new[oid] = entity
        return oid

    def _queue_field_entities(self, entity: object, ti: TypeInfo,
                              queue: deque[tuple[Any, Any]]) -> None:
        """Queue the entities the fields of ``entity`` reach, with ``entity`` as their container."""
        # Flat-entity fast-path (#52): a type none of whose fields can
        # hold an entity ref (a SOR row of scalars/strings, FKs kept as
        # plain str) has nothing to discover — skip the per-field walk
        # entirely. has_entity_refs mirrors _walk_value's leaf set, so a
        # ref-bearing field is never wrongly skipped.
        if ti.has_entity_refs:
            child_container = entity if ti.protected else None
            for spec in ti.specs:
                # eager_position drives the R11 runtime check: a field
                # whose refs do NOT decode as Lazy handles would
                # eager-materialize a protected child (ADR-008 R11 leak
                # backstop — see _walk_value).
                self._walk_value(getattr(entity, spec.name), queue,
                                 child_container,
                                 eager_position=not spec.lazy_refs)

    def _stamp_perm_labels(self, obj: LabelColumns, container: LabelColumns | None) -> None:
        """Stamp birth labels on a fresh protected record, once, from :meth:`_register_graph`.

        ``_dc_owner`` is the acting principal's uid, never inherited (R6 provenance). Groups and
        both floors copy from the enqueuing protected container, else the inert R6 defaults stay.
        ``object.__setattr__`` is frozen-safe (mirrors ``_fill_entity``).
        See docs/design/ADR-008-permissions-contract.md, "Birth labels and the write fence".
        """
        uid = self.principal.uid
        object.__setattr__(obj, "_dc_owner", uid)
        # The gate baseline is what inheritance/birth GIVES — independent of any
        # labels the user staged with verbs before store() (those ARE the
        # principal's grant and must be ceiling-checked against this baseline).
        if (container is not None
                and not obj._dc_groups
                and obj._dc_read_floor == obj._dc_write_floor == 0):  # VIEWER
            # Inherit only onto the untouched birth shape: non-default labels staged before store()
            # (dc.share/protect) win, as inheritance is a default. A child left at the birth
            # defaults looks untouched; relabel after store() to force owner-only in a shared
            # container.
            base_groups = list(container._dc_groups)
            base_read = container._dc_read_floor
            base_write = container._dc_write_floor
            object.__setattr__(obj, "_dc_groups", wrap_value(list(base_groups), obj))
            object.__setattr__(obj, "_dc_read_floor", base_read)
            object.__setattr__(obj, "_dc_write_floor", base_write)
        else:
            base_groups, base_read, base_write = [], VIEWER, VIEWER
        oid = oid_of(obj)
        assert oid is not None  # _stamp_perm_labels runs only after stamping
        self._birth_labels[oid] = (uid, base_groups, base_read, base_write)

    def _discover_new_graphs(self) -> None:
        """P1 discovery: dirty/new objects may reference brand-new entities."""
        walked: set[int] = set()
        for obj in [*self._new.values(), *self._dirty.values()]:
            self._register_graph(obj, walked)

    def _walk_value(self, value: object, queue: deque[tuple[Any, Any]],
                    container: object | None = None, *, eager_position: bool = False) -> None:
        if value is None or isinstance(value, _LEAF_TYPES):
            return  # overwhelmingly the common case: scalars reference nothing
        if is_entity(value):
            self._queue_entity(value, queue, container, eager_position=eager_position)
        elif isinstance(value, Lazy):
            target = cast("Lazy[Any]", value)._peek_unchecked()
            if target is not None:
                # Propagate the field's decode mode, NOT an unconditional
                # exemption: a Lazy wrapper in a lazy-typed field stays lazy
                # (no raise), but a Lazy in an eager position still persists
                # eager, so the check must still fire on its target.
                self._walk_value(target, queue, container,
                                 eager_position=eager_position)
        elif isinstance(value, (list, tuple)):
            for item in cast("tuple[Any, ...]", value):
                self._walk_value(item, queue, container,
                                 eager_position=eager_position)
        elif isinstance(value, dict):
            for item in cast("dict[Any, object]", value).values():
                self._walk_value(item, queue, container,
                                 eager_position=eager_position)

    def _queue_entity(self, entity: object, queue: deque[tuple[Any, Any]],
                      container: object | None, *, eager_position: bool) -> None:
        """Queue a not-yet-walked entity, refusing a protected one in an eager field (R11)."""
        if (eager_position and type_info(entity).protected
                and not isinstance(entity, Redacted)):
            # R11 runtime complement (ADR-008): _entity._check_r11_lazy_only cannot see
            # Any-typed fields, bare containers or store.root, so this closes it at the write
            # boundary. It keys on the field position's decode mode, not a Lazy wrapper:
            # `swizzle` collapses `Lazy.of(x)` to a bare RefToken in a non-`Lazy[...]` field,
            # which re-decodes eagerly. Only `spec.lazy_refs` is exempt.
            raise TypeError(
                f"{type(entity).__name__} is protected — protected classes "
                "are Lazy-referable only (ADR-008 R11). The FIELD must be "
                "typed dc.Lazy[...] (e.g. `dc.Lazy["
                f"{type(entity).__name__}]`): an Any/untyped field, a bare "
                "container, or store.root cannot hold a protected reference, "
                "because it persists and re-decodes as an eager edge even "
                "when the value is wrapped in dc.Lazy.of(...)"
            )
        oid = oid_of(entity)
        if oid is None or oid in self._new or oid in self._dirty:
            queue.append((entity, container))

    def _harvest_live_refs(self, obj: object) -> set[int]:
        """Return the OIDs ``obj`` references: entity and Lazy refs in fields and containers (#20).

        The live-object twin of ``_indexes.harvest_ref_oids``. A ref and a ``Lazy`` to the same
        entity persist as the same OID.
        """
        out: set[int] = set()
        stack: list[Any] = [getattr(obj, n) for n in type_info(obj).field_names]
        while stack:
            v = stack.pop()
            if v is None or isinstance(v, _LEAF_TYPES):
                continue
            if is_entity(v):
                vid = oid_of(v)
                if vid is not None:
                    out.add(vid)
            elif isinstance(v, Lazy):
                lz = cast("Lazy[Any]", v)
                # an unloaded handle carries its OID; a fresh Lazy.of(obj) does
                # not yet, so fall back to the loaded target's OID (mirrors
                # _walk_value, which follows peek()).
                vid = lz.oid
                if vid is None:
                    target = lz._peek_unchecked()
                    vid = oid_of(target) if target is not None else None
                if vid is not None:
                    out.add(vid)
            elif isinstance(v, (list, tuple)):
                stack.extend(cast("list[Any]", v))
            elif isinstance(v, dict):
                stack.extend(cast("dict[Any, object]", v).values())
        return out

    def _oid_for_encode(self, obj: object) -> int:
        oid = oid_of(obj)
        if oid is None:
            raise DataCrystalError(
                f"internal error: {type(obj).__name__} escaped P1 discovery"
            )
        return oid

    def _blob_positions(self, ti: TypeInfo) -> frozenset[int]:
        """Return the indices of the ``dc.Blob`` fields, in ``ti.field_names`` order.

        ``encode_payload`` needs this spec-driven split signal: a bytes value alone cannot be told
        from an inline ``bytes`` scalar (ADR-007 §2).
        """
        return frozenset(
            i for i, spec in enumerate(ti.specs) if spec.blob
        )

    def _fingerprint_payload(self, obj: object, ti: TypeInfo) -> bytes:
        """Re-encode an entity to a stable payload for the debug fingerprint, without blob I/O.

        A hydrated ``dc.Blob`` holds a ``BlobHandle`` whose descriptor is already stable. ``oid``
        is pinned to 0: fingerprints are only compared within one representation epoch.
        See docs/design/ADR-007-blob-fields.md, "Implementation notes".
        """
        values = [getattr(obj, name) for name in ti.field_names]
        positions = self._blob_positions(ti)
        if not positions:
            return encode_payload(values, self._oid_for_encode)
        # Present each blob slot as its (0, size, hash) descriptor: a BlobHandle
        # carries size/hash already; a live bytes value is hashed in place.
        fp_values: list[Any] = list(values)
        for i in positions:
            v = values[i]
            if isinstance(v, BlobHandle):
                fp_values[i] = BlobToken(0, v.size, v.hash)
            elif isinstance(v, bytes):
                fp_values[i] = BlobToken(0, len(v), hashlib.sha256(v).digest())
            elif isinstance(v, BlobSource):
                # Dead-defensive fallback: a fingerprint is only taken on a hydrated holder (a
                # BlobHandle) or a CLEAN sweep target, and P3 swaps a BlobSource to a BlobHandle
                # first. Unreachable in practice; kept so the encode never chokes on a stray
                # source.
                fp_values[i] = BlobToken(0, v.size, b"")
        return fingerprint_payload(fp_values, self._oid_for_encode)

    def _blob_read_gate(self, owner_oid: int) -> None:
        """Enforce that the current principal may read the record that owns a blob (ADR-008 W4-3).

        Called by :meth:`BlobHandle.bytes` and :meth:`open_blob` only for a PROTECTED owner, so an
        unprotected owner costs nothing. It judges committed labels like the deref checkpoint (D1);
        root passes.
        Raises ``ReadDeniedError`` when the owning record is unreadable.
        See docs/design/ADR-008-permissions-contract.md, "R14 deref checkpoint".
        """
        p = self.principal
        if is_root(p):
            return
        obj = self._registry.get(owner_oid)
        if obj is not None:
            ti = type_info(obj)
            labels = self._index.ensure(ti).committed_labels(owner_oid)
            if labels is not None and can_read_row(p, *labels):
                return
        else:
            rec = self._backend.load_many([owner_oid]).get(owner_oid)
            if rec is not None:
                owner, groups, read_floor, _wf = self._prior_labels(rec)
                if can_read_row(p, owner, groups, read_floor):
                    return
        raise ReadDeniedError(
            f"the record owning this blob (oid {owner_oid}) is not readable by "
            "the current principal — its blobs are fenced with it (ADR-008 W4-3)"
        )

    def _load_blob_bytes(self, blob_oid: int) -> bytes:
        """Fetch one whole blob value for a :class:`~datacrystal.Blob` handle
        (ADR-007). Re-asserts the ADR-001 owner-thread contract before any I/O —
        the same confinement the live read path enforces — then returns the raw
        bytes (the backend has already CRC-checked them).
        """
        self._guard()
        stored = self._backend.load_blob(blob_oid)
        if stored is None:
            raise DanglingRefError(
                f"no blob for oid {blob_oid} in the store — deleted (v0.x "
                "deletes are unchecked, ADR-003) or never committed; the blob "
                "reference you followed is stale"
            )
        return stored.data

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
        factory (additive evolution). Removed persisted fields are ignored.
        """
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
                if spec.glue is not None:
                    # #26 (b): the field is absent from this record's persisted
                    # shape — derive it from the old record at fill time (read
                    # only; never rewrites the record). Distinguished from a
                    # plain default by ``spec.glue`` in the fill loops.
                    plan.append((spec, None, None))
                    continue
                if ti.protected and spec.name in PERM_LEGACY_FILLS:
                    # R7 legacy fill (ADR-008): a persisted shape lacking the
                    # _dc_ columns predates protection — read-as-before,
                    # ADMIN-write; NOT the R6 birth defaults (write_floor
                    # would come out VIEWER = world-writable, the exact
                    # inversion of R7). One shared constant, three fill sites.
                    plan.append((spec, None, PERM_LEGACY_FILLS[spec.name]))
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

    def _decode_check(self, rec: StoredRecord, ti: TypeInfo) -> None:
        """Decode one record to ``ti``'s current shape WITHOUT constructing an
        entity — raises if it cannot (the :meth:`verify` probe). The hydration
        plan must bind every live field (else ``SchemaMismatchError``), the
        payload width must match, and every ``Glue`` function must run on this
        record's old values (a glue that raises is a real, reportable failure).
        """
        hplan = self._hydration_plan(rec.cid, ti)
        values = decode_payload(rec.payload)
        persisted = self._persisted_fields.get(rec.cid, [])
        if len(values) != len(persisted):
            raise SchemaMismatchError(
                f"{ti.typename}: record has {len(values)} fields, its type "
                f"dictionary row has {len(persisted)}"
            )
        by_name: dict[str, Any] | None = None
        for spec, index, _factory in hplan:
            if index is None and spec.glue is not None:
                if by_name is None:
                    by_name = dict(zip(persisted, values))
                spec.glue(by_name)

    def _load_oid(self, oid: int, cache: dict[int, StoredRecord] | None = None) -> object:
        self._guard()
        obj = self._registry.get(oid)
        if obj is not None:
            return obj
        return self._materialize_graph(oid, cache)

    def _load_root_holder(self, oid: int) -> _Root:
        # cast: the store's root OID always names the _Root record it created
        return cast("_Root", self._load_oid(oid))

    def _load_oid_deref(
        self, oid: int, cache: dict[int, StoredRecord] | None = None
    ) -> Any:  # noqa: ANN401 - an instance or twin whose fields Lazy and the tests read
        """Load ``oid`` at the R14 deref checkpoint (ADR-008 W3-4): instance, twin or dangling.

        It judges committed labels only (W3 D1): judging a staged ``share()`` would let a principal
        pre-grant itself. Unlike :meth:`_load_oid` (principal-free by design, R11) it serves deref
        surfaces only. A registry hit is checked too, so no principal sees an instance another one
        loaded.
        See docs/design/ADR-008-permissions-contract.md, "R14 deref checkpoint".
        """
        self._guard()
        obj = self._registry.get(oid)
        if obj is not None:
            checked = self._check_registered(obj, oid)
            if checked is not None:
                return checked
        rec = cache.get(oid) if cache else None
        if rec is None:
            rec = self._backend.load_many([oid]).get(oid)
        if rec is None:
            raise DanglingRefError(
                f"no record for oid {oid} in the store — deleted "
                "(v0.x deletes are unchecked, ADR-003) or never committed; "
                "the reference you followed is stale"
            )
        twin = self._twin_if_unreadable(rec, oid)
        if twin is not None:
            return twin
        return self._materialize_graph(oid, cache if cache is not None else {oid: rec})

    def _check_registered(self, obj: object, oid: int) -> object | None:
        """Judge a registry hit at the checkpoint: the instance, a twin, or ``None`` to re-check."""
        ti = type_info(obj)
        if not ti.protected:
            return obj
        p = self.principal
        if is_root(p):
            return obj
        labels = self._index.ensure(ti).committed_labels(oid)
        if labels is None:
            # An inconsistency for a registered CLEAN/DIRTY oid — should not
            # happen. The caller falls through to the record path and lets ITS
            # check decide: fail closed, never open.
            return None
        if can_read_row(p, *labels):
            return obj
        return make_twin(ti, oid)

    def _twin_if_unreadable(self, rec: StoredRecord, oid: int) -> object | None:
        """Return a twin when the principal may not read the protected record, else ``None``."""
        ti = self._ti_for_cid(rec.cid)
        if not ti.protected:
            return None
        p = self.principal
        if is_root(p):
            return None
        owner, groups, read_floor, _write_floor = self._prior_labels(rec)
        if can_read_row(p, owner, groups, read_floor):
            return None
        return make_twin(ti, oid)  # nothing hydrated, registry untouched

    def _deref_cached_protected(self, obj: object, oid: int) -> object:
        """Resolve a cached protected target of a ``Lazy.of`` handle to the instance or a twin.

        This is the cross-principal fence for a handle shared through the live registry. A
        just-``store()``d target has no committed labels, so :meth:`_load_oid_deref` would raise
        ``DanglingRefError``; its live birth labels are judged instead (tranquility), so the owner
        reads its uncommitted work and an outsider still gets a twin.
        """
        if oid in self._new:
            p = self.principal
            # the caller (Lazy.get) checked type_info(obj).protected, so the label columns exist
            labelled = cast("LabelColumns", obj)
            if is_root(p) or can_read_row(
                    p, labelled._dc_owner, list(labelled._dc_groups), labelled._dc_read_floor):
                return obj
            return make_twin(type_info(obj), oid)
        return self._load_oid_deref(oid)

    def _materialize_graph(self, root_oid: int,
                           cache: dict[int, StoredRecord] | None) -> object:
        """Materialize ``root_oid`` and its EAGER reference closure with an explicit work-queue.

        The read-path twin of :meth:`_register_graph`: load depth is bounded by the heap, not the C
        stack (#29); ``Lazy`` refs are not followed (invariant 6). Each shell is registered before
        its fields are filled, so a back-edge finds the in-progress shell. If any fill fails, every
        shell this call registered is discarded.
        See docs/design/DESIGN.md, "Read-path loading and identity".
        """
        registered: list[int] = []
        fill_queue: deque[tuple[object, StoredRecord]] = deque()

        def link(child_oid: int, *, root: bool = False) -> object:
            # Return the live object for child_oid, enqueuing its bare shell if new, unfilled.
            # Read-side backstop for ADR-008 R11: a non-root PROTECTED target is never
            # eager-materialized into a shared parent; it fails closed to an unloaded Lazy, so a
            # later .get() takes the checked deref. _walk_value forbids creating such an edge; this
            # covers other sources (federation, migration).
            existing = self._registry.get(child_oid)
            if existing is not None:
                if not root and type_info(existing).protected:
                    return Lazy._unloaded(child_oid, self)
                return existing
            rec = cache.get(child_oid) if cache else None
            if rec is None:
                rec = self._backend.load_many([child_oid]).get(child_oid)
            if rec is None:
                raise DanglingRefError(
                    f"no record for oid {child_oid} in the store — deleted "
                    "(v0.x deletes are unchecked, ADR-003) or never committed; "
                    "the reference you followed is stale"
                )
            child_ti = self._ti_for_cid(rec.cid)
            if not root and child_ti.protected:
                return Lazy._unloaded(child_oid, self)
            shell = object.__new__(child_ti.cls)
            stamp(shell, child_oid, self, STATE_CLEAN)
            self._registry.add(child_oid, shell)  # before fill: breaks cycles
            registered.append(child_oid)
            fill_queue.append((shell, rec))
            return shell

        try:
            root = link(root_oid, root=True)
            while fill_queue:
                obj, rec = fill_queue.popleft()
                self._fill_entity(obj, rec, link)
        except BaseException:
            for oid in registered:  # roll back EVERY shell this load registered
                self._registry.discard(oid)
                self._fingerprints.pop(oid, None)
            raise
        return root

    def _fill_entity(self, obj: object, rec: StoredRecord,
                     link: Callable[[int], object]) -> None:
        ti = self._ti_for_cid(rec.cid)
        plan = self._hydration_plan(rec.cid, ti)
        values = decode_payload(rec.payload)
        persisted = self._persisted_fields.get(rec.cid, [])
        if len(values) != len(persisted):
            raise SchemaMismatchError(
                f"{ti.typename}: record has {len(values)} fields, its type "
                f"dictionary row has {len(persisted)} — the store is damaged"
            )
        fill = object.__setattr__  # bound once: this loop is the hot path
        by_name: dict[str, Any] | None = None  # built once iff a glue field fires
        for spec, index, factory in plan:
            if index is not None:
                raw = values[index]
            elif spec.glue is not None:  # #26 (b): derive from the old record
                if by_name is None:
                    by_name = dict(zip(persisted, values))
                raw = spec.glue(by_name)
            else:
                raw = factory()
            if raw is None or type(raw) in _SCALAR_TYPES:
                fill(obj, spec.name, raw)  # scalars skip the resolve call
            else:
                fill(obj, spec.name, self._resolve(raw, spec.lazy_refs, obj, link))
        if self._debug:
            # Fingerprint via the same encode path the sweep uses (NOT the
            # stored payload: an old-lineage record re-encodes through the live
            # class shape, which must not read as a mutation). An eager-ref
            # field may hold a not-yet-filled child shell — fine, the encode
            # only needs its OID.
            try:
                self._fingerprints[rec.oid] = _crc(
                    self._fingerprint_payload(obj, ti)
                )
            except BaseException:  # noqa: BLE001 - a fingerprint that cannot be taken is simply not recorded
                self._fingerprints.pop(rec.oid, None)

    def _resolve(self, value: object, lazy: bool, owner: object,
                 link: Callable[[int], object]) -> object:
        if value is None or isinstance(value, _LEAF_TYPES):
            return value  # scalar fast path: nothing to swizzle back
        if isinstance(value, BlobToken):
            # A dc.Blob field hydrates to a lazy BlobHandle, never raw bytes (ADR-007 §3):
            # .size/.hash come free, .bytes() fetches on first touch. ADR-008 W4-3: pass the owner
            # OID only for a protected owner so .bytes() can re-gate the read; an unprotected owner
            # stays byte-identical.
            owner_oid = oid_of(owner) if type_info(owner).protected else None
            return BlobHandle._bind(value.blob_oid, value.size, value.hash, self,
                                    owner_oid=owner_oid)
        if isinstance(value, RefToken):
            if lazy:
                existing = self._registry.get(value.oid)
                # ADR-008 W3-4: a registry hit is pre-loaded into the handle only when its target
                # is UNPROTECTED, as a loaded handle in a shared parent is a cross-principal cache
                # (the landmine Lazy.get() closes). A protected target always builds unloaded, so
                # every .get() re-derives through the checked deref.
                if existing is not None and not type_info(existing).protected:
                    # engine-only Lazy constructors (users use Lazy.of)
                    handle: Lazy[Any] = Lazy[Any]._loaded(
                        existing, value.oid, self
                    )
                    if self._lazyman is not None:
                        self._lazyman.track(handle)
                    return handle
                # engine-only Lazy constructors (users use Lazy.of)
                unloaded: Lazy[Any] = Lazy._unloaded(value.oid, self)
                return unloaded
            return link(value.oid)  # eager: hand off to the iterative driver (#29)
        if isinstance(value, list):
            return PersistentList(
                (self._resolve(item, lazy, owner, link)
                 for item in cast("list[object]", value)),
                owner=owner,
            )
        if isinstance(value, dict):
            return PersistentDict(
                ((k, self._resolve(v, lazy, owner, link))
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
    attribute reads come from the row dict, nothing else exists.
    """

    __slots__ = ("_row",)

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    def __getattr__(self, name: str) -> object:
        try:
            return self._row[name]
        except KeyError:
            raise AttributeError(name) from None


def _ref_target_oid(value: object) -> int | None:
    """Return the OID an entity or Lazy handle points at, else None."""
    if is_entity(value):
        return oid_of(value)
    if isinstance(value, Lazy):
        if value.oid is not None:
            return value.oid
        target = cast("Lazy[Any]", value)._peek_unchecked()
        return oid_of(target) if target is not None else None
    return None


def _blob_hash(value: object) -> bytes | None:
    """Return the sha256 of a blob-field value for upsert equivalence (ADR-007), or None.

    A hydrated ``BlobHandle`` carries it free; raw ``bytes``/``bytearray`` are hashed, since the
    write path accepts both (else an identical ``bytearray`` upsert re-stores). Anything else
    (None, a streamed ``BlobSource``) is not a blob.
    """
    if isinstance(value, BlobHandle):
        return value.hash
    if isinstance(value, (bytes, bytearray)):
        return hashlib.sha256(value).digest()
    return None


def _equivalent(cur: object, new: object) -> bool:
    """Would persisting ``new`` over ``cur`` write the same bytes? Decides
    whether upsert() skips a field. Conservative: references match by
    target OID (a Lazy handle and a direct reference encode identically);
    a type change (e.g. ``1`` → ``True``) re-writes even when ``==`` says
    equal, because msgpack bytes differ; wrapped containers compare to the
    plain ones they came from.
    """
    if cur is new:
        return True
    # Blob fields compare by content hash (a BlobHandle carries the sha256, raw bytes are hashed),
    # so an upsert re-supplying an identical blob is a no-op (ADR-007); a blob-vs-None differs. A
    # dc.BlobSource has no resident hash (reading it would defeat streaming), so _blob_hash is None
    # and an upsert with a streamed source always re-writes: the safe choice.
    if isinstance(cur, (BlobHandle, BlobSource)) or isinstance(new, (BlobHandle, BlobSource)):
        return _blob_hash(cur) == _blob_hash(new) and _blob_hash(cur) is not None
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


def _raw_value(value: object) -> object:
    """Map entity/Lazy predicate values onto the decoded representation
    (RefTokens compare by OID), so residuals evaluate without hydration.
    """
    if is_entity(value):
        oid = oid_of(value)
        if oid is None:
            raise QueryError(
                "cannot match an entity that was never stored — it has no OID"
            )
        return RefToken(oid)
    if isinstance(value, Lazy):
        target = cast("Lazy[Any]", value)._peek_unchecked()  # OID only, loaded handle knows best
        if target is not None:
            return _raw_value(target)
        if value.oid is None:
            raise QueryError("cannot match an unloaded Lazy without an OID")
        return RefToken(value.oid)
    return value


def _order_by_attr(objs: list[Any], field: str, descending: bool) -> list[Any]:
    """Hydrated entities ordered by a field for the residual order_by path (#25):
    NULLs last, stable ascending-OID tiebreak (``objs`` arrive ascending-OID from
    ``get_many``, and a stable sort preserves that within equal values).
    """
    present = [o for o in objs if getattr(o, field) is not None]
    absent = [o for o in objs if getattr(o, field) is None]
    present.sort(key=lambda o: getattr(o, field), reverse=descending)
    return present + absent


def _order_rows(rows: list[tuple[int, dict[str, Any]]], field: str,
                descending: bool) -> list[tuple[int, dict[str, Any]]]:
    """Return decoded ``(oid, row)`` pairs ordered by ``row[field]`` for pluck order_by (#25).

    NULLs last, stable ascending-OID tiebreak (``rows`` arrive ascending-OID from the candidate
    scan).
    """
    present = [r for r in rows if r[1][field] is not None]
    absent = [r for r in rows if r[1][field] is None]
    present.sort(key=lambda r: r[1][field], reverse=descending)
    return present + absent


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


def _publish(value: object) -> object:
    """Turn a decoded payload value into pluck() output.

    Refs become public ``Ref`` tokens (``get_many()`` hydrates them), containers plain lists and
    dicts. A blob field plucks as its inert ``BlobToken`` descriptor: a decode-level scan never
    reads blob bytes (ADR-007).
    """
    if isinstance(value, BlobToken):
        return value
    if isinstance(value, RefToken):
        return Ref(value.oid)
    if isinstance(value, list):
        return [_publish(item) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        return {key: _publish(item)
                for key, item in cast("dict[Any, object]", value).items()}
    return value


def _find_escapee(value: object) -> str | None:
    """Name the first live entity (or handle to one) inside a submit()
    result, or None if the value is plain data (ADR-001: EntityEscapeError).
    """
    if is_entity(value):
        return type(value).__name__
    if isinstance(value, Lazy):
        target = cast("Lazy[Any]", value)._peek_unchecked()
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


def _require_entity(obj: object) -> None:
    if not is_entity(obj):
        raise NotAnEntityError(
            f"{type(obj).__name__} is not an @entity class instance"
        )


def _forbid_redacted(obj: object) -> None:
    if isinstance(obj, Redacted):
        raise ReadDeniedError(
            f"this {type(obj).__name__} is a redacted twin — never "
            "committable (ADR-008 R14)"
        )


def _require_storable(obj: object) -> None:
    """Refuse a redacted twin or a deleted entity before it is registered."""
    if isinstance(obj, Redacted):
        # ADR-008 R14: a twin has no engine state slot (state_of() would raise
        # AttributeError), so catch it before that call, whether it is the top-level
        # argument (store(), root setter, upsert) or was smuggled into another entity's
        # field and enqueued by _walk_value.
        raise ReadDeniedError(
            f"this {type(obj).__name__} is a redacted twin — "
            "never committable (ADR-008 R14)"
        )
    if state_of(obj) == STATE_DELETED:
        raise DeletedEntityError(
            f"this {type(obj).__name__} was deleted via "
            "store.delete() and cannot be stored again — create a "
            "new entity instead (OIDs are never reused)"
        )


def _require_unique(cls: type, ti: TypeInfo, field: str) -> None:
    spec = ti.spec(field)
    if spec is None or not spec.unique:
        raise QueryErrorFor(cls, field)


def _require_persisted_fields(cls: type, ti: TypeInfo, fields: tuple[str, ...]) -> None:
    known = set(ti.field_names)
    for name in fields:
        if name not in known:
            raise QueryError(
                f"{cls.__name__}.{name} is not a persisted field "
                f"(fields: {', '.join(ti.field_names)})"
            )


def _projector(fields: tuple[str, ...]) -> Callable[[dict[str, Any]], object]:
    """Build pluck's row-to-output function: a bare value for one field, else a tuple."""
    if len(fields) == 1:
        single = fields[0]
        return lambda row: _publish(row[single])
    return lambda row: tuple(_publish(row[name]) for name in fields)


def _wanted_oids(items: list[Any]) -> list[int]:
    """Return the OIDs ``get_many`` must load for ``items``, refusing a non-reference item."""
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
    return wanted


def _natural_key_field(obj: object, ti: TypeInfo, key: str | None) -> str:
    """Return the Unique field an upsert matches on: ``key``, or the class's only one."""
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
        return unique_fields[0]
    if key not in unique_fields:
        raise QueryErrorFor(type(obj), key)
    return key


def _merge_data_fields(existing: object, incoming: object, ti: TypeInfo) -> None:
    """Copy the changed data fields of ``incoming`` onto the surviving ``existing`` entity."""
    # data_field_names: the _dc_ label columns are NEVER merged (W2-3,
    # ADR-008) — a fresh probe instance carries R6 birth defaults, and
    # copying them would reset the survivor's curated labels (and
    # /v1/submit rides upsert). IS field_names for unprotected classes.
    for name in ti.data_field_names:
        new = getattr(incoming, name)
        cur = getattr(existing, name)
        if not _equivalent(cur, new):
            setattr(existing, name, new)  # the one-shot hook buffers it


def _hash_blob_source(source: BlobSource) -> bytes:
    """Return the sha256 of a streamed blob source without holding its bytes whole."""
    hasher = hashlib.sha256()
    total = 0
    for chunk in source.open_chunks():
        hasher.update(chunk)
        total += len(chunk)
    if total != source.size:
        raise ValueError(
            f"a dc.BlobSource declared size {source.size} but its "
            f"bytes total {total} — fix the size or the source"
        )
    return hasher.digest()


def _note_streamed_swaps(
    obj: object,
    ti: TypeInfo,
    values: list[Any],
    blob_positions: frozenset[int],
    payload: bytes,
    blob_swaps: list[tuple[Any, str, int, int, bytes]],
) -> None:
    """Record the streamed-source fields of one encoded entity for the P3 swap.

    Each field's blob descriptor is read straight from the just-encoded payload, so two fields
    sharing ONE BlobSource each pick up their OWN blob OID (id(value) keying would alias both to
    the last-allocated one).
    """
    streamed_positions = [
        i for i in blob_positions if isinstance(values[i], BlobSource)
    ]
    if streamed_positions:
        decoded = decode_payload(payload)
        for i in streamed_positions:
            tok = cast("BlobToken", decoded[i])  # the field's BLOB_EXT descriptor
            blob_swaps.append(
                (obj, ti.field_names[i], tok.blob_oid, tok.size, tok.hash)
            )


def _bind_frozen_containers(obj: object, ti: TypeInfo) -> None:
    """Bind the containers of a frozen entity to it, so post-commit mutation raises.

    A frozen ``__init__`` bypasses the tracked ``__setattr__``, so its containers are still plain
    and an in-place change would silently do nothing.
    """
    for name in ti.field_names:
        value = getattr(obj, name)
        if isinstance(value, (list, dict, tuple)):
            set_field(obj, name, wrap_value(value, obj))


def _deny_write(p: Principal, message: str) -> NoReturn:
    raise WriteDeniedError(f"{message} — as principal uid={p.uid}; the buffer "
                           "is intact: discard() or fix and re-commit "
                           "(ADR-008)")


def _gate_persisted_record(p: Principal, oid: int, obj: LabelColumns, ti: TypeInfo,
                           prior: _Labels) -> _Labels:
    """Gate a write to a persisted protected record and return its prior labels as the baseline."""
    held = authority_towards(p, prior[0], prior[1])
    if held < prior[3]:
        _deny_write(p, f"write to {ti.cls.__name__} oid={oid} denied: the "
                       f"record's write floor is {prior[3]} and your "
                       f"authority towards it is {held} (the floor binds "
                       "everyone, including the owner — the curation "
                       "guarantee)")
    if obj._dc_owner != prior[0]:
        # Ownership is immutable after birth: only root (R9, short-circuited by the caller) may
        # reassign it. Otherwise any writer clearing the write floor could stage itself
        # as owner, gaining a permanent owner-read bypass and minting its personal-best
        # level into the R8 ceiling, which reads the STAGED owner (ADR-008 R8,
        # amendment 2026-07-15).
        _deny_write(p, f"cannot change owner of {ti.cls.__name__} oid={oid} "
                       f"from {prior[0]} to {obj._dc_owner}: ownership is "
                       "immutable after birth — only root may reassign it (R9)")
    return prior


def _gate_label_changes(p: Principal, oid: int, obj: LabelColumns, ti: TypeInfo,
                        base: _Labels) -> None:
    """Gate the staged groups and floors against the baseline and the principal's authority (R8)."""
    staged_groups = list(obj._dc_groups)
    ceiling = authority_towards(p, obj._dc_owner, staged_groups)
    base_groups: set[int] = set(base[1])
    for g in set(staged_groups) - base_groups:
        if level(p, g) == NO_STANDING:
            _deny_write(p, f"cannot share {ti.cls.__name__} oid={oid} into group "
                           f"{g}: you hold no standing there (R8 — cross-group "
                           "handoff goes through someone who holds both groups)")
    for label_name, staged, was in (
        ("read", obj._dc_read_floor, base[2]),
        ("write", obj._dc_write_floor, base[3]),
    ):
        if staged != was and staged > ceiling:
            _deny_write(p, f"cannot set {ti.cls.__name__} oid={oid} {label_name} "
                           f"floor to {staged}: your own authority towards the "
                           f"record is {ceiling} (R8 ceiling — a label write is "
                           "a write, bounded by what you hold)")


def _gate_actor_memberships(p: Principal, actor: Actor, prior_m: dict[int, int]) -> None:
    """Gate every CHANGED membership of a staged Actor against the level the committer holds.

    F1 (ADR-008 R8): an Actor's `memberships` is authority, since acting_as(uid) turns a stored
    level into a live Principal; without this a writer could mint
    Actor(memberships={WORLD: EXECUTIVE}) and act as root. Root returned earlier; lowering or
    removing never blocks.
    """
    for g, lvl in actor.memberships.items():
        if lvl != prior_m.get(g) and lvl > level(p, g):
            _deny_write(p, f"cannot grant Actor uid={actor.uid} level {lvl} in "
                           f"group {g}: your own level there is {level(p, g)} "
                           "— an Actor's memberships are authority, capped by "
                           "what you hold (R8 ceiling on authority-bearing "
                           "grants; only root may mint root)")


def _gate_delete(p: Principal, oid: int, ti: TypeInfo, labels: _Labels) -> None:
    """Gate a delete of a protected record: it is a write, so the write floor binds."""
    owner, groups, _read, write_floor = labels
    held = authority_towards(p, owner, groups)
    if held < write_floor:
        _deny_write(p, f"delete of {ti.cls.__name__} oid={oid} denied: its write "
                       f"floor is {write_floor} and your authority towards it "
                       f"is {held} (a delete is a write)")


def QueryErrorFor(cls: type, field: str) -> Exception:
    from datacrystal._errors import QueryError

    return QueryError(
        f"{cls.__name__}.{field} is not a Unique field; "
        "get() looks up unique secondary keys only — use query() for the rest"
    )
