"""Error taxonomy for datacrystal.

Every exception raised by datacrystal derives from :class:`DataCrystalError`,
so callers can catch the whole family with one clause. The taxonomy mirrors
the contracts in docs/design/ADR-001-concurrency-contract.md and ROADMAP.md.
"""

from __future__ import annotations


class DataCrystalError(Exception):
    """Base class for all datacrystal errors."""


class StoreClosedError(DataCrystalError):
    """The store has been closed; no further operations are possible."""


class StoreLockedError(DataCrystalError):
    """Another live process holds the store's single-writer lease lock."""


class LeaseLostError(DataCrystalError):
    """This process lost the single-writer lease (e.g. after a long pause).

    Another process may have taken over the store; writing further would risk
    two-writer corruption, so the store refuses to commit.
    """


class WrongThreadError(DataCrystalError):
    """A live entity or store was touched from a thread that does not own it.

    Per ADR-001, a store and its live object graph are confined to the thread
    (or asyncio event loop) that opened them. Send work to the owner via
    ``store.submit(fn)``; read from other threads via ``store.snapshot()``.
    """


class EntityEscapeError(DataCrystalError):
    """A ``submit()`` result would have carried a live entity across the
    owner boundary (ADR-001) — return plain data from submitted closures."""


class FrozenEntityError(DataCrystalError):
    """An ``@entity(frozen=True)`` instance was mutated after construction."""


class NotAnEntityError(DataCrystalError):
    """An object that is not an ``@entity`` class instance was passed where
    an entity is required."""


class UniqueViolationError(DataCrystalError):
    """A commit would create two entities with the same unique-key value."""


class SchemaMismatchError(DataCrystalError):
    """A persisted record cannot be mapped onto the live class.

    Additive schema evolution is automatic — fields added *with a default*
    are filled on load, removed fields are ignored. This error means
    something beyond that: a new field without a default, a Unique field
    added with a non-None default, or a damaged type dictionary. A rename is
    remove+add (the old values are dropped); explicit data migrations are
    post-v0.1 work.
    """


class UnregisteredTypeError(DataCrystalError):
    """The store contains records of a type whose ``@entity`` class has not
    been imported/defined in this process."""


class NewerStoreError(DataCrystalError):
    """The store was written by a newer format version than this library
    supports (DESIGN.md amendment 7: refuse loudly, never misread)."""


class CorruptRecordError(DataCrystalError):
    """A stored record failed its checksum — the store file is damaged."""


class QueryError(DataCrystalError):
    """A condition is malformed — e.g. it mixes fields of two entity classes
    (cross-entity joins are a v1 feature on Arrow mirrors, not v0.x)."""


class ConsumerDetachedWarning(UserWarning):
    """An attached delta consumer raised during delivery and was detached.

    The commit it choked on is durable and the store is healthy — sidecars
    are rebuildable derived data (invariant 11), so a broken consumer never
    holds writes hostage. Its watermark now lags the store; ``attach()``
    refuses it until it rebuilds (e.g. from ``store.snapshot()``). See
    COMMIT-DELTA-v1 §5: deltas are not retained, missed history cannot be
    re-fetched from the engine."""


class UntrackedMutationWarning(UserWarning):
    """``debug=True`` found a CLEAN entity whose re-encoded record differs
    from its last committed/hydrated state: something mutated it without
    going through the dirty-tracking hook (e.g. ``object.__setattr__`` or a
    mutable non-container object like a ``bytearray``). The safety net
    commits the entity anyway — fix the write path it names (KICKOFF risk 1:
    silent lost writes were the #1 DX killer in both ancestor systems)."""
