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
    (or asyncio event loop) that opened them. Read from other threads via
    ``store.snapshot()`` (v0.x), or send work to the owner via
    ``store.submit(fn)`` (M2).
    """


class EntityEscapeError(DataCrystalError):
    """A live entity was returned across the owner-thread boundary (ADR-001)."""


class FrozenEntityError(DataCrystalError):
    """An ``@entity(frozen=True)`` instance was mutated after construction."""


class NotAnEntityError(DataCrystalError):
    """An object that is not an ``@entity`` class instance was passed where
    an entity is required."""


class UniqueViolationError(DataCrystalError):
    """A commit would create two entities with the same unique-key value."""


class SchemaMismatchError(DataCrystalError):
    """The persisted field schema of a type differs from the live class.

    Schema evolution (renames, adds, deletes) is post-v0.1 work; until then
    datacrystal refuses loudly instead of guessing.
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
