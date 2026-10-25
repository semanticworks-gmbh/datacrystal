"""Embed an object-graph database in Python: datacrystal crystallizes your live objects.

Inspired by EclipseStore.
Typed Python objects are the database: pickle-free, crash-safe, with bitmap-indexed queries built
in.
Quickstart: README.md; design docs: docs/design/ (DESIGN.md, ROADMAP.md, ADR-001).
"""

import logging
from typing import TYPE_CHECKING

from datacrystal._actors import (
    ADMIN,
    AGENT,
    AUTOMATION,
    CURATOR,
    EXECUTIVE,
    NO_STANDING,
    STAFF,
    VIEWER,
    WORLD,
    Actor,
    Principal,
    root_principal,
)
from datacrystal._conditions import Condition, fields
from datacrystal._containers import PersistentDict, PersistentList
from datacrystal._entity import (
    Blob,
    FullText,
    Glue,
    Index,
    RenamedFrom,
    SortedIndex,
    Unique,
    entity,
)
from datacrystal._errors import (
    ConflictError,
    ConsumerDetachedWarning,
    CorruptRecordError,
    DanglingDeleteWarning,
    DanglingRefError,
    DataCrystalError,
    DeletedEntityError,
    EntityEscapeError,
    FrozenEntityError,
    LeaseLostError,
    MixedTemporalIndexError,
    NewerStoreError,
    NotAnEntityError,
    QueryError,
    ReadDeniedError,
    SchemaMismatchError,
    SchemaSkewError,
    SponsorRequiredError,
    StoreClosedError,
    StoreLockedError,
    UncommittedActorError,
    UniqueViolationError,
    UnknownActorError,
    UnregisteredTypeError,
    UnseenTypeWarning,
    UntrackedMutationWarning,
    WriteDeniedError,
    WrongThreadError,
)
from datacrystal._follower import HttpClient, open_follower
from datacrystal._indexes import QueryPlan
from datacrystal._lazy import BlobHandle, BlobSource, Lazy, blob_from_path
from datacrystal._permissions import Permissions, protect, share, unshare
from datacrystal._pipeline import DeltaConsumer
from datacrystal._redacted import Redacted
from datacrystal._snapshot import EntityView, Ref, Snapshot, SnapshotIndexes
from datacrystal._store import Store

if TYPE_CHECKING:  # the real import stays lazy — see __getattr__ below
    from datacrystal._async import AsyncStore, aopen

__version__ = "0.10.0"

logging.getLogger("datacrystal").addHandler(logging.NullHandler())


def __getattr__(name: str) -> object:  # PEP 562
    """Load the asyncio facade on first use: plain ``import datacrystal``
    must not pay the ``asyncio`` import (fitness #12, import-time budget).
    """
    if name in ("aopen", "AsyncStore"):
        from datacrystal import _async

        return getattr(_async, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "ADMIN",
    "AGENT",
    "AUTOMATION",
    "CURATOR",
    "EXECUTIVE",
    "NO_STANDING",
    "STAFF",
    "VIEWER",
    "WORLD",
    "Actor",
    "AsyncStore",
    "Blob",
    "BlobHandle",
    "BlobSource",
    "Condition",
    "ConflictError",
    "ConsumerDetachedWarning",
    "CorruptRecordError",
    "DanglingDeleteWarning",
    "DanglingRefError",
    "DataCrystalError",
    "DeletedEntityError",
    "DeltaConsumer",
    "EntityEscapeError",
    "EntityView",
    "FrozenEntityError",
    "FullText",
    "Glue",
    "HttpClient",
    "Index",
    "Lazy",
    "LeaseLostError",
    "MixedTemporalIndexError",
    "NewerStoreError",
    "NotAnEntityError",
    "Permissions",
    "PersistentDict",
    "PersistentList",
    "Principal",
    "QueryError",
    "QueryPlan",
    "ReadDeniedError",
    "Redacted",
    "Ref",
    "RenamedFrom",
    "SchemaMismatchError",
    "SchemaSkewError",
    "Snapshot",
    "SnapshotIndexes",
    "SortedIndex",
    "SponsorRequiredError",
    "Store",
    "StoreClosedError",
    "StoreLockedError",
    "UncommittedActorError",
    "Unique",
    "UniqueViolationError",
    "UnknownActorError",
    "UnregisteredTypeError",
    "UnseenTypeWarning",
    "UntrackedMutationWarning",
    "WriteDeniedError",
    "WrongThreadError",
    "__version__",
    "aopen",
    "blob_from_path",
    "entity",
    "fields",
    "open_follower",
    "protect",
    "root_principal",
    "share",
    "unshare",
]
