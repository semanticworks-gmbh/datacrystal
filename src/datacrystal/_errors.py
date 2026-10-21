"""Error taxonomy for datacrystal.

Every exception raised by datacrystal derives from :class:`DataCrystalError`,
so callers can catch the whole family with one clause. The taxonomy mirrors
the contracts in docs/design/ADR-001-concurrency-contract.md and ROADMAP.md.
"""

from __future__ import annotations


class DataCrystalError(Exception):
    """Base class for all datacrystal errors."""


# FEDERATION-WIRE-v1 §"Conflict envelope": the ``error`` discriminator strings a
# ``/v1/submit`` 409 carries. The follower dispatches on these to raise the
# faithful typed error, so they are load-bearing wire values — defined ONCE here
# as the single source of truth for the encode (``web/_federation``) / decode
# (``_follower``) pair (a rename then cannot drift the two halves apart).
ERROR_CONFLICT = "conflict"
ERROR_SCHEMA_SKEW = "schema-skew"
ERROR_DANGLING_REF = "dangling-ref"


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
    owner boundary (ADR-001) — return plain data from submitted closures.
    """


class FrozenEntityError(DataCrystalError):
    """An ``@entity(frozen=True)`` instance was mutated after construction."""


class NotAnEntityError(DataCrystalError):
    """An object that is not an ``@entity`` class instance was passed where
    an entity is required.
    """


class UniqueViolationError(DataCrystalError):
    """A commit would create two entities with the same unique-key value."""


class SchemaMismatchError(DataCrystalError):
    """A persisted record cannot be mapped onto the live class.

    Additive evolution (new fields with a default, removed fields) is automatic.
    This error means more: a new field without a default, a Unique field added with a non-None
    default, or a damaged type dictionary.
    A rename is remove plus add; explicit data migrations are post-v0.1.
    """


class SchemaSkewError(DataCrystalError):
    """A federated contribution carries a field the coordinator's class lacks.

    The ``datacrystal[web]`` ``/v1/submit`` cid-lineage guard (ROADMAP item 21,
    [FEDERATION-WIRE-v1]): a follower's ``@entity`` shape forked from the
    coordinator's (a field added on the follower, or the two never in sync). The
    whole submission is rejected (fail-closed, HTTP 409) rather than silently
    dropping the unknown field — roll out coordinator-first.
    """


class ConflictError(DataCrystalError):
    """A federated contribution conflicts with the coordinator's current state (FEDERATION-WIRE-v1).

    The ``/v1/submit`` OCC guard rejects the whole submission (HTTP 409) when the base token is
    stale; re-read and retry.
    Detect-and-reject, never last-writer-wins.
    Carries ``key``, ``expected_base`` and ``actual_base`` (``None`` means the key is absent).
    """

    def __init__(
        self,
        message: str,
        *,
        key: object = None,
        expected_base: str | None = None,
        actual_base: str | None = None,
    ) -> None:
        super().__init__(message)
        self.key = key
        self.expected_base = expected_base
        self.actual_base = actual_base


class UnregisteredTypeError(DataCrystalError):
    """The store contains records of a type whose ``@entity`` class has not
    been imported/defined in this process.
    """


class NewerStoreError(DataCrystalError):
    """The store was written by a newer format version than this library
    supports (DESIGN.md amendment 7: refuse loudly, never misread).
    """


class CorruptRecordError(DataCrystalError):
    """A stored record failed its checksum — the store file is damaged."""


class MixedTemporalIndexError(DataCrystalError):
    """A SortedIndex datetime field holds both timezone-naive and timezone-aware values (#106,
    ADR-004 section 4).

    Python cannot compare the two, so the mix is rejected at insert or build instead of raising a
    bare ``TypeError`` in ``bisect``.
    Pick one convention per field; storing every timestamp aware is recommended.
    """


class QueryError(DataCrystalError):
    """A condition is malformed — e.g. it mixes fields of two entity classes
    (cross-entity joins are a v1 feature on Arrow mirrors, not v0.x).
    """


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


class UnknownActorError(DataCrystalError):
    """``store.acting_as(uid)`` found no ``dc.Actor`` row with that uid.

    The shipped registry resolves session identities (epic #168); an
    unregistered uid cannot act. Store an ``Actor`` row first — or pass a
    ``dc.Principal`` directly when identity comes from verified auth claims
    instead of the registry.
    """


class SponsorRequiredError(DataCrystalError):
    """``store.acting_as(uid)`` resolved a non-human actor whose sponsor gate fails.

    Either no sponsor is named, or the named sponsor is not a registered human actor.
    Set ``Actor.sponsor`` to a human's uid (ADR-008, epic #168).
    """


class UncommittedActorError(DataCrystalError):
    """``store.acting_as(uid)`` resolved an ``Actor`` row with uncommitted
    changes (buffered NEW or DIRTY state).

    Identity must be durable before it acts: a membership or sponsor edit
    that never commits would otherwise authorize stamps the replayed
    registry history cannot explain. Commit the registry change first.
    """


class WriteDeniedError(DataCrystalError):
    """A write to a protected record was denied (ADR-008 R10).

    Raised in commit P1 before the TID is allocated, so the sequence stays gapless and buffered
    changes stay intact: ``discard()`` or fix and re-commit.
    Covers a write below the write floor, a floor above the setter's own authority (R8), and
    creating a protected record anonymously (R6).
    Denial is deterministic, so ``committing()`` never retries it (unlike :class:`ConflictError`).
    """


class ReadDeniedError(DataCrystalError):
    """Redacted data was used (ADR-008 R14): a field read on a ``dc.Redacted`` twin, or an attempt
    to mutate or commit one.

    Traversal never raises this; only a data-field or ``dc_permissions`` read, or a write
    entrypoint, does.
    The message carries only the typename, since a title or value would itself leak.
    Unlike :class:`DanglingRefError`, the record exists but is denied.
    """


class ConsumerDetachedWarning(UserWarning):
    """An attached delta consumer raised during delivery and was detached.

    The commit is durable and the store healthy; sidecars are rebuildable (invariant 11).
    Its watermark now lags, and ``attach()`` refuses it until it rebuilds, because deltas are not
    retained (COMMIT-DELTA-v1 section 5).
    """


class UnseenTypeWarning(UserWarning):
    """``query()``/``count()``/``pluck()`` ran against an entity class the
    store has no committed records of — the result is trivially empty.

    Legitimate on a first run (nothing committed yet); a footgun when you
    meant to open a different store file or forgot to ``commit()`` before
    reading back. ``get()`` deliberately stays silent — ``None`` is the
    expected miss in the get-or-create idiom.
    """


class UntrackedMutationWarning(UserWarning):
    """``debug=True`` found a CLEAN entity whose re-encoded record differs
    from its last committed/hydrated state: something mutated it without
    going through the dirty-tracking hook (e.g. ``object.__setattr__`` or a
    mutable non-container object like a ``bytearray``). The safety net
    commits the entity anyway — fix the write path it names (KICKOFF risk 1:
    silent lost writes were the #1 DX killer in both ancestor systems).
    """


class DanglingDeleteWarning(UserWarning):
    """``debug=True`` committed a delete that left a surviving record pointing at the deleted OID.

    ADR-003 only raises at dereference; this names the referrers found via ``incoming(dead)`` (#110)
    at the delete.
    The commit proceeds, and ``strict_deletes=True`` raises ``DanglingRefError`` instead.
    A dev-time bridge until checked deletes land, not referential integrity.
    """
