"""Shared fixtures: one mineral-cabinet model, two storage backends.

Every engine test runs parametrized over (memory, sqlite) — the protocol is
the seam, both sides must behave identically (KICKOFF test strategy).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import field
from pathlib import Path
from typing import Annotated, Protocol

import pytest

import datacrystal as dc
from datacrystal import Principal
from datacrystal._storage.memory import MemoryBackend


@dc.entity
class Locality:
    """Where a mineral was first found."""

    qid: Annotated[str, dc.Unique]
    name: str
    country: Annotated[str | None, dc.Index] = None


@dc.entity
class Mineral:
    """A mineral of the cabinet, with indexed and unique fields."""

    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None
    tags: list = field(default_factory=list)


@dc.entity(frozen=True)
class LogEntry:
    """A frozen note about the cabinet."""

    note: str
    kind: Annotated[str, dc.Index] = "misc"


@pytest.fixture(params=["memory", "sqlite"])
def store_factory(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[], dc.Store]:
    """Return a zero-arg callable that opens the *same* store each call."""
    if request.param == "memory":
        backend = MemoryBackend()

        def open_store() -> dc.Store:
            return dc.Store._from_backend(backend)
    else:

        def open_store() -> dc.Store:
            return dc.Store.open(tmp_path / "store", lock_ttl=0.5)

    return open_store


class DevStoreOpener(Protocol):
    """Opens the same store each call, with the dev flags of :meth:`dc.Store.open`."""

    def __call__(
        self,
        *,
        debug: bool = False,
        strict_deletes: bool = False,
        principal: Principal | None = None,
    ) -> dc.Store:
        """Open the store with these flags."""


@pytest.fixture(params=["memory", "sqlite"])
def dev_store_factory(request: pytest.FixtureRequest, tmp_path: Path) -> DevStoreOpener:
    """Return an opener of the *same* store on each backend; the dev flags are chosen per call."""
    if request.param == "memory":
        backend = MemoryBackend()

        def open_store(
            *, debug: bool = False, strict_deletes: bool = False, principal: Principal | None = None
        ) -> dc.Store:
            return dc.Store._from_backend(
                backend, debug=debug, strict_deletes=strict_deletes, principal=principal
            )
    else:

        def open_store(
            *, debug: bool = False, strict_deletes: bool = False, principal: Principal | None = None
        ) -> dc.Store:
            return dc.Store.open(
                tmp_path / "store",
                lock_ttl=0.5,
                debug=debug,
                strict_deletes=strict_deletes,
                principal=principal,
            )

    return open_store


@pytest.fixture
def store(store_factory: Callable[[], dc.Store]) -> Iterator[dc.Store]:
    """Yield one open store per test and close it afterwards."""
    s = store_factory()
    yield s
    s.close()
