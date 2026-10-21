"""Explicit lazy references (ROADMAP item 1) and their timeout manager.

``Lazy[T]`` is the only deferred-loading mechanism in v0.x; the engine hydrates unloaded handles.
:class:`LazyReferenceManager` (KICKOFF M2) demotes idle handles; timeout-only (KICKOFF open question
5).
Daemon principle (ADR-001 bound decision 3): the manager never touches the graph from a foreign
thread.
"""

from __future__ import annotations

import threading
import time
import weakref
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, cast

from datacrystal._errors import StoreClosedError


class Lazy[T]:
    """A typed, explicitly-lazy reference to an entity.

    ``Lazy.of(entity)`` wraps a live entity, ``get()`` loads the target through the store
    (owner-thread contract, ADR-001), and ``peek()`` returns it only if loaded.
    For a ``protected=True`` target ``get()`` is the ADR-008 R14 checkpoint and re-checks the
    current principal on every call.
    The manager may demote an engine handle after ``lazy_timeout``; the next ``get()`` reloads.
    """

    __slots__ = ("__weakref__", "_atime", "_clock", "_obj", "_oid", "_storeref")

    # Slot attribute types declared at class level (annotation-only, no value —
    # compatible with __slots__). Pins ``_obj`` to ``T | None`` so an engine
    # assignment from a loosely-typed store cannot poison it to Unknown.
    _obj: T | None
    _oid: int | None
    _storeref: weakref.ref[Any] | None
    _atime: float
    _clock: Callable[[], float] | None

    def __init__(self) -> None:
        """Refuse direct construction; use :meth:`of`."""
        raise TypeError("use Lazy.of(entity) to create a lazy reference")

    @classmethod
    def of(cls, obj: T) -> Lazy[T]:
        """Wrap a live entity in a loaded handle."""
        self = object.__new__(cls)
        self._obj = obj
        self._oid = None
        self._storeref = None
        self._atime = 0.0
        self._clock = None
        return self

    @classmethod
    def _loaded(cls, obj: T, oid: int, store: Any) -> Lazy[T]:
        """Engine path: a hydrated handle whose target is already live
        (registry hit) — demotable, unlike a user-made ``Lazy.of``.
        """
        self = cls.of(obj)
        self._oid = oid
        self._storeref = weakref.ref(store)
        return self

    @classmethod
    def _unloaded(cls, oid: int, store: Any) -> Lazy[Any]:
        self = object.__new__(cls)
        self._obj = None
        self._oid = oid
        self._storeref = weakref.ref(store)
        self._atime = 0.0
        self._clock = None
        return self

    @property
    def loaded(self) -> bool:
        """Whether the target is currently held in memory."""
        return self._obj is not None

    @property
    def oid(self) -> int | None:
        """The target OID, or ``None`` for a handle never stored."""
        return self._oid

    def get(self) -> T:
        # local import: _entity imports _lazy at module level, so a module-level
        # import here would cycle (the _state.py/_permissions.py precedent for
        # engine-lazy imports).
        """Return the target, loading it from the store if needed.

        A protected target is re-checked on every call: ADR-008, "R14 deref checkpoint".
        """
        from datacrystal._entity import oid_of, type_info

        obj = self._obj
        if obj is None:
            storeref = self._storeref
            store = storeref() if storeref is not None else None
            if store is None:
                raise StoreClosedError(
                    "lazy reference cannot load: its store is closed or gone"
                )
            # The R14 checkpoint (ADR-008): real instance, dc.Redacted twin or DanglingRefError.
            obj = cast(T, store._load_oid_deref(self._oid))
            # Never cache a protected target: it would pin the first principal's result across later
            # acting_as() scopes.
            if not type_info(obj).protected:
                self._obj = obj
                manager = store._lazyman
                if manager is not None:
                    manager.track(self)
            return obj
        # A cached protected target (a user Lazy.of handle) must still pass the checkpoint, so
        # re-derive it
        # through the checked deref from the target's own store and oid.
        # An unbound target (never stored) belongs to its creating principal and returns as-is.
        if type_info(obj).protected:
            oid = oid_of(obj)
            if oid is not None:
                try:
                    storeref = object.__getattribute__(obj, "__dc_store__")
                except AttributeError:
                    storeref = None
                store = storeref() if storeref is not None else None
                if store is not None:
                    # Also covers the pre-commit window; see Store._deref_cached_protected.
                    return cast(T, store._deref_cached_protected(obj, oid))
            return obj
        if self._clock is not None:
            self._atime = self._clock()  # refresh idle time for the manager
        return obj

    def peek(self) -> T | None:
        """Return the target only if already loaded, else ``None``.

        A protected target is never exposed here: ``peek`` has no principal, so use :meth:`get`.
        Engine write plumbing that needs only the OID uses :meth:`_peek_unchecked`.
        """
        obj = self._obj
        if obj is not None:
            from datacrystal._entity import type_info
            if type_info(obj).protected:
                return None
        return obj

    def _peek_unchecked(self) -> T | None:
        """Return the raw cached target with no read fence, for engine write plumbing that needs
        only its OID or typename.

        Never call from a reader-facing path; use :meth:`get`.
        """
        return self._obj

    def __repr__(self) -> str:
        """Render the handle without exposing a protected target."""
        obj = self._obj
        if obj is not None:
            from datacrystal._entity import oid_of, type_info
            if type_info(obj).protected:
                # Never render a protected target's fields (they include the
                # _dc_* label columns): repr flows into logs and error
                # messages, an implicit read surface (ADR-008 R14).
                return f"Lazy(<protected oid={oid_of(obj)}>)"
            return f"Lazy({obj!r})"
        return f"Lazy(<unloaded oid={self._oid}>)"


class BlobHandle:
    """A lazy handle to an out-of-line raw-bytes field (ADR-007, #83).

    A ``dc.Blob`` field hydrates to one; ``size`` and ``hash`` need no fetch, ``bytes()`` caches.
    The manager can demote the cache, and blobs are immutable, so a cache is never stale.
    Owner-confined; for a cross-thread read use ``store.open_blob`` or ``snapshot.open_blob``.
    A plain ``bytes`` assigned to the field stays ``bytes`` until commit, then reads back as a
    handle.
    """

    __slots__ = (
        "__weakref__",
        "_atime",
        "_clock",
        "_hash",
        "_obj",
        "_oid",
        "_owner_oid",
        "_size",
        "_storeref",
    )

    # Mirror Lazy's slot annotations so the manager's duck-typed sweep
    # (_obj / _oid / _atime / _clock) operates on a Blob with no special-casing.
    _obj: bytes | None
    _oid: int
    _size: int
    _hash: bytes
    _storeref: weakref.ref[Any] | None
    # The OID of the record that OWNS this blob, but ONLY when that record is
    # protected (ADR-008 W4-3): a blob is readable iff its owning record is, so
    # .bytes() re-checks the owner under the CURRENT principal. None for an
    # unprotected owner — the gate is then structurally skipped (zero cost).
    _owner_oid: int | None
    _atime: float
    _clock: Callable[[], float] | None

    def __init__(self) -> None:
        raise TypeError("dc.Blob handles are created by the engine on hydration")

    @classmethod
    def _bind(cls, blob_oid: int, size: int, hash: bytes, store: Any,
              owner_oid: int | None = None) -> BlobHandle:
        """Engine path: a handle for a decoded :class:`BlobToken`, bound to its
        store but with the bytes still on disk (fetched on first ``.bytes()``).
        ``owner_oid`` is set only for a PROTECTED owning record (ADR-008 W4-3);
        None leaves the read gate off (unprotected owner, zero cost).
        """
        self = object.__new__(cls)
        self._obj = None
        self._oid = blob_oid
        self._size = size
        self._hash = hash
        self._storeref = weakref.ref(store)
        self._owner_oid = owner_oid
        self._atime = 0.0
        self._clock = None
        return self

    @property
    def size(self) -> int:
        """The blob's byte length — from the descriptor, no fetch."""
        return self._size

    @property
    def hash(self) -> bytes:
        """The blob's sha256 digest (32 bytes) — from the descriptor, no fetch."""
        return self._hash

    @property
    def blob_oid(self) -> int:
        """The blob's OID (its row in the ``blobs`` table).

        The encode path re-emits an unchanged blob's descriptor with it, so an immutable blob is
        never re-stored.
        """
        return self._oid

    @property
    def loaded(self) -> bool:
        """Whether the bytes are currently cached in memory."""
        return self._obj is not None

    def bytes(self) -> bytes:
        """Return the whole blob value, reading it from the store on the first call.

        Later calls return the cached bytes until the manager demotes the handle.
        """
        # ADR-008 W4-3: the owner-read gate runs on every call, before the cache check,
        # so cached bytes are never served to a later unreadable principal.
        if self._owner_oid is not None:
            storeref = self._storeref
            store = storeref() if storeref is not None else None
            if store is None:
                raise StoreClosedError(
                    "blob handle cannot load: its store is closed or gone"
                )
            store._blob_read_gate(self._owner_oid)
        obj = self._obj
        if obj is None:
            storeref = self._storeref
            store = storeref() if storeref is not None else None
            if store is None:
                raise StoreClosedError(
                    "blob handle cannot load: its store is closed or gone"
                )
            obj = store._load_blob_bytes(self._oid)
            self._obj = obj
            manager = store._lazyman
            if manager is not None:
                manager.track(self)
        elif self._clock is not None:
            self._atime = self._clock()  # refresh idle time for the manager
        return obj

    def __repr__(self) -> str:
        state = "cached" if self._obj is not None else "on-disk"
        return f"BlobHandle(oid={self._oid}, size={self._size}, {state})"


class BlobSource:
    """A sized, re-readable source for a streamed blob write (ADR-007 section 4).

    The size must be known up front, because SQLite pre-allocates the cell.
    ``open_chunks`` must return a fresh iterable on each call, because the engine reads the source
    twice, before the TID (to hash and check the length gaplessly) and inside the commit.
    After the commit the field reads back as a :class:`BlobHandle`; use :func:`blob_from_path` for a
    file.
    """

    __slots__ = ("open_chunks", "size")

    size: int
    open_chunks: Callable[[], Iterable[bytes]]

    def __init__(self, size: int, open_chunks: Callable[[], Iterable[bytes]]) -> None:
        if size < 0:
            raise ValueError(f"a blob size must be >= 0, got {size!r}")
        self.size = size
        self.open_chunks = open_chunks

    def __repr__(self) -> str:
        return f"BlobSource(size={self.size})"


def blob_from_path(path: str | Path, *, chunk_size: int = 1 << 20) -> BlobSource:
    """Return a :class:`BlobSource` reading a file in ``chunk_size`` blocks (default 1 MiB).

    This is the PDF-archival recipe (ADR-007 section 4).
    The size is taken at call time, and the file is re-opened for each of the two read passes, so it
    must stay unchanged across the commit.
    """
    p = Path(path)
    size = p.stat().st_size

    def open_chunks() -> Iterator[bytes]:
        with p.open("rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    return
                yield chunk

    return BlobSource(size, open_chunks)


class LazyReferenceManager:
    """Demotes idle loaded ``Lazy`` handles back to unloaded (timeout-only).

    Takes an injectable ``clock`` so tests never sleep, and tracks only handles that can reload
    themselves.
    A protected target is never tracked, because :meth:`Lazy.get` never caches one (ADR-008 W3-4).
    """

    def __init__(self, timeout: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if timeout <= 0:
            raise ValueError(f"lazy_timeout must be positive, got {timeout!r}")
        self._timeout = timeout
        self._clock = clock
        self._handles: weakref.WeakSet[Any] = weakref.WeakSet()
        self._last_sweep = clock()
        # Conformance hooks (fitness #4, daemon principle): which thread
        # demoted last, and how many handles ever.
        self.last_demotion_thread: int | None = None
        self.demoted_total = 0

    @property
    def sweep_interval(self) -> float:
        """Seconds between piggybacked sweeps, a quarter of the timeout."""
        return max(self._timeout / 4.0, 0.01)

    def track(self, handle: Any) -> None:
        """Register a (re)loaded handle and stamp its access time."""
        handle._clock = self._clock
        handle._atime = self._clock()
        self._handles.add(handle)

    def maybe_sweep(self) -> int:
        """Sweep if an interval has passed — the sync owner's piggyback."""
        if self._clock() - self._last_sweep < self.sweep_interval:
            return 0
        return self.sweep()

    def sweep(self) -> int:
        """Demote every tracked handle idle past the timeout and return the count.

        Callers are the owner by construction; the acting thread is recorded for the conformance
        suite.
        """
        now = self._clock()
        self._last_sweep = now
        demoted = 0
        for handle in list(self._handles):
            if handle._obj is not None and handle._oid is not None \
                    and now - handle._atime > self._timeout:
                handle._obj = None  # the next get() reloads through the store
                demoted += 1
        if demoted:
            self.last_demotion_thread = threading.get_ident()
            self.demoted_total += demoted
        return demoted
