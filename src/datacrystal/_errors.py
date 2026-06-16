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


class MixedTemporalIndexError(DataCrystalError):
    """A SortedIndex datetime field holds both timezone-naive and timezone-aware
    values (#106 / ADR-004 §4).

    Aware datetimes order by their UTC instant; naive datetimes carry no offset.
    Python refuses to compare the two, so a sorted run that mixed them would raise
    a bare ``TypeError`` deep in ``bisect``/``insort``. datacrystal rejects the mix
    loudly at insert/build instead — pick one convention per field (store every
    timestamp aware, e.g. ``datetime.now(timezone.utc)``, is the recommendation)."""


class QueryError(DataCrystalError):
    """A condition is malformed — e.g. it mixes fields of two entity classes
    (cross-entity joins are a v1 feature on Arrow mirrors, not v0.x)."""


class DeletedEntityError(DataCrystalError):
    """A ``store.delete()``d entity was written to, re-``store()``d, or is
    still referenced by an uncommitted graph (ADR-003).

    A deleted entity is a detached plain object: field reads keep working,
    everything that would persist it again raises. Create a new entity
    instead — OIDs are never reused.
    """


class DanglingRefError(DataCrystalError):
    """A reference to a deleted (or never-existing) record was followed.

    v0.x deletes are *unchecked* (ADR-003): nothing stops you deleting an
    entity other records still point at — following such a stale reference
    raises this, loudly, instead of returning None. Checked deletes
    (cascade/orphan validation) arrive with the v1 reverse-reference index.
    """


class ConsumerDetachedWarning(UserWarning):
    """An attached delta consumer raised during delivery and was detached.

    The commit it choked on is durable and the store is healthy — sidecars
    are rebuildable derived data (invariant 11), so a broken consumer never
    holds writes hostage. Its watermark now lags the store; ``attach()``
    refuses it until it rebuilds (e.g. from ``store.snapshot()``). See
    COMMIT-DELTA-v1 §5: deltas are not retained, missed history cannot be
    re-fetched from the engine."""


class UnseenTypeWarning(UserWarning):
    """``query()``/``count()``/``pluck()`` ran against an entity class the
    store has no committed records of — the result is trivially empty.

    Legitimate on a first run (nothing committed yet); a footgun when you
    meant to open a different store file or forgot to ``commit()`` before
    reading back. ``get()`` deliberately stays silent — ``None`` is the
    expected miss in the get-or-create idiom."""


class UntrackedMutationWarning(UserWarning):
    """``debug=True`` found a CLEAN entity whose re-encoded record differs
    from its last committed/hydrated state: something mutated it without
    going through the dirty-tracking hook (e.g. ``object.__setattr__`` or a
    mutable non-container object like a ``bytearray``). The safety net
    commits the entity anyway — fix the write path it names (KICKOFF risk 1:
    silent lost writes were the #1 DX killer in both ancestor systems)."""
