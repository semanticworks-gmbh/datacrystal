"""datacrystal — your live objects, crystallized.

An embedded object-graph database for Python, inspired by EclipseStore:
typed Python objects **are** the database — pickle-free, crash-safe, with
bitmap-indexed queries built in.

Quickstart::

    from typing import Annotated
    import datacrystal as dc

    @dc.entity
    class Mineral:
        qid: Annotated[str, dc.Unique]
        name: str
        crystal_system: Annotated[str | None, dc.Index] = None

    store = dc.Store.open("cabinet.store")
    if store.root is None:
        store.root = [Mineral(qid="Q43010", name="quartz", crystal_system="trigonal")]
    store.commit()
    hits = store.query(Mineral.crystal_system == "trigonal")
    store.close()

Design docs: docs/design/ in the repository (DESIGN.md, ROADMAP.md, ADR-001).
"""

from datacrystal._conditions import fields
from datacrystal._containers import PersistentDict, PersistentList
from datacrystal._entity import FullText, Index, Unique, entity
from datacrystal._errors import (
    CorruptRecordError,
    DataCrystalError,
    EntityEscapeError,
    FrozenEntityError,
    LeaseLostError,
    NewerStoreError,
    NotAnEntityError,
    QueryError,
    SchemaMismatchError,
    StoreClosedError,
    StoreLockedError,
    UniqueViolationError,
    UnregisteredTypeError,
    WrongThreadError,
)
from datacrystal._lazy import Lazy
from datacrystal._store import Store

__version__ = "0.1.0.dev0"

__all__ = [
    "Store",
    "entity",
    "fields",
    "Lazy",
    "Index",
    "Unique",
    "FullText",
    "PersistentList",
    "PersistentDict",
    "DataCrystalError",
    "StoreClosedError",
    "StoreLockedError",
    "LeaseLostError",
    "WrongThreadError",
    "EntityEscapeError",
    "FrozenEntityError",
    "NotAnEntityError",
    "UniqueViolationError",
    "SchemaMismatchError",
    "UnregisteredTypeError",
    "NewerStoreError",
    "CorruptRecordError",
    "QueryError",
    "__version__",
]
