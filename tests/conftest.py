"""Shared fixtures: one mineral-cabinet model, two storage backends.

Every engine test runs parametrized over (memory, sqlite) — the protocol is
the seam, both sides must behave identically (KICKOFF test strategy).
"""

from __future__ import annotations

from dataclasses import field
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend


@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str
    country: Annotated[str | None, dc.Index] = None


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None
    tags: list = field(default_factory=list)


@dc.entity(frozen=True)
class LogEntry:
    note: str
    kind: Annotated[str, dc.Index] = "misc"


@pytest.fixture(params=["memory", "sqlite"])
def store_factory(request, tmp_path):
    """Returns a zero-arg callable opening the *same* store each call."""
    if request.param == "memory":
        backend = MemoryBackend()

        def open_store() -> dc.Store:
            return dc.Store._from_backend(backend)
    else:

        def open_store() -> dc.Store:
            return dc.Store.open(tmp_path / "store", lock_ttl=0.5)

    return open_store


@pytest.fixture
def store(store_factory):
    s = store_factory()
    yield s
    s.close()
