"""debug=True — the msgspec-fingerprint safety net (KICKOFF M2, risk 1).

A mutation that slips past the hooks (a bypass write, or an in-place
mutation of a mutable non-container object such as a bytearray) is the
silent-lost-write class that killed DX in both ancestor systems. With
debug=True every commit re-encodes the live CLEAN entities, warns with
UntrackedMutationWarning, and commits the change anyway — detection plus
rescue, never silent loss.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from tests.conftest import Mineral


StoreOpener = Callable[..., dc.Store]


@pytest.fixture(params=["memory", "sqlite"])
def debug_store_factory(request: pytest.FixtureRequest, tmp_path: Path) -> StoreOpener:
    """Return an opener of the *same* store with ``debug`` chosen per call."""
    if request.param == "memory":
        backend = MemoryBackend()

        def open_store(*, debug: bool) -> dc.Store:
            return dc.Store._from_backend(backend, debug=debug)
    else:

        def open_store(*, debug: bool) -> dc.Store:
            return dc.Store.open(tmp_path / "store", lock_ttl=0.5, debug=debug)

    return open_store


@dc.entity
class Scan:
    """A specimen scan whose raw sensor buffer is a bytearray.

    A bytearray is mutable and not a container the engine wraps, hence the honest untracked vector.
    """

    name: str
    raw: bytearray


def test_bypass_write_is_detected(debug_store_factory: StoreOpener) -> None:
    """Given a committed mineral renamed through object.__setattr__, past the hook
    When the debug store commits
    Then UntrackedMutationWarning names Mineral
    """
    with debug_store_factory(debug=True) as store:
        quartz = Mineral(qid="Q1", name="quartz")
        store.store(quartz)
        store.commit()
        object.__setattr__(quartz, "name", "smoky quartz")

        with pytest.warns(dc.UntrackedMutationWarning, match="Mineral"):
            store.commit()


def test_bypass_write_is_rescued_not_lost(debug_store_factory: StoreOpener) -> None:
    """Given a committed mineral renamed through object.__setattr__, past the hook
    When the debug store commits and the backend is reopened
    Then the mineral is named "smoky quartz"
    """
    with debug_store_factory(debug=True) as store:
        quartz = Mineral(qid="Q1", name="quartz")
        store.store(quartz)
        store.commit()
        object.__setattr__(quartz, "name", "smoky quartz")
        with pytest.warns(dc.UntrackedMutationWarning):
            store.commit()

    reopened = debug_store_factory(debug=False)
    found = reopened.get(Mineral, qid="Q1")
    assert found is not None
    assert found.name == "smoky quartz"
    reopened.close()


def test_bytearray_mutation_on_a_live_entity_is_detected(debug_store_factory: StoreOpener) -> None:
    """Given a committed scan whose bytearray buffer is appended to in place
    When the debug store commits
    Then UntrackedMutationWarning names Scan
    """
    with debug_store_factory(debug=True) as store:
        scan = Scan(name="alpha", raw=bytearray(b"\x00\x01"))
        store.store(scan)
        store.commit()
        scan.raw.append(0x02)

        with pytest.warns(dc.UntrackedMutationWarning, match="Scan"):
            store.commit()


def test_bytearray_round_trips_as_bytes_after_hydration(debug_store_factory: StoreOpener) -> None:
    """Known v0.1 mapping: bytearray round-trips as bytes (msgpack bin).

    Given a scan with a bytearray buffer, appended to and committed with a warning
    When a new debug store hydrates it
    Then the buffer holds the bytes 0, 1, 2
    """
    with debug_store_factory(debug=True) as store:
        scan = Scan(name="alpha", raw=bytearray(b"\x00\x01"))
        oid = store.store(scan)
        store.commit()
        scan.raw.append(0x02)
        with pytest.warns(dc.UntrackedMutationWarning):
            store.commit()
        del scan

    with debug_store_factory(debug=True) as store:
        hydrated = store.get_many([oid])[0]

        assert bytes(hydrated.raw) == b"\x00\x01\x02"


def test_bypass_write_on_a_hydrated_entity_is_detected(debug_store_factory: StoreOpener) -> None:
    """The live-object bytearray vector cannot recur after hydration, so a bypass write proves it.

    Given a scan hydrated by a new debug store and renamed through object.__setattr__
    When the debug store commits
    Then UntrackedMutationWarning names Scan
    """
    with debug_store_factory(debug=True) as store:
        oid = store.store(Scan(name="alpha", raw=bytearray(b"\x00\x01")))
        store.commit()

    with debug_store_factory(debug=True) as store:
        hydrated = store.get_many([oid])[0]
        object.__setattr__(hydrated, "name", "beta")

        with pytest.warns(dc.UntrackedMutationWarning, match="Scan"):
            store.commit()


def test_tracked_changes_and_rereads_raise_no_warning(debug_store_factory: StoreOpener) -> None:
    """Given a committed mineral changed through the hook and a tracked container
    When the debug store commits with UntrackedMutationWarning turned into an error
    And commits a second time with nothing pending
    Then no warning is raised
    And the second commit finds nothing to commit, the dirty entity's fingerprint having refreshed
    """
    with debug_store_factory(debug=True) as store:
        quartz = Mineral(qid="Q1", name="quartz", tags=["smoky", "rutile"])
        store.store(quartz)
        store.commit()
        quartz.mohs = 7.0
        quartz.tags.append("phantom")

        with warnings.catch_warnings():
            warnings.simplefilter("error", dc.UntrackedMutationWarning)
            tid = store.commit()
            second_tid = store.commit()

        assert tid is not None
        assert second_tid is None


def test_commit_with_nothing_pending_raises_no_warning(debug_store_factory: StoreOpener) -> None:
    """Given a committed mineral re-read with all fingerprints current
    When the debug store commits with nothing pending, UntrackedMutationWarning turned into an error
    Then nothing is committed
    """
    with debug_store_factory(debug=True) as store:
        quartz = Mineral(qid="Q1", name="quartz")
        store.store(quartz)
        store.commit()
        reread = store.get(Mineral, qid="Q1")
        assert reread is quartz

        with warnings.catch_warnings():
            warnings.simplefilter("error", dc.UntrackedMutationWarning)
            tid = store.commit()

        assert tid is None


def test_without_debug_a_bypass_write_raises_no_warning(debug_store_factory: StoreOpener) -> None:
    """Detection costs O(live set), so it is opt-in (use debug in dev).

    Given a committed mineral renamed through object.__setattr__ in a store without debug
    When the store commits with UntrackedMutationWarning turned into an error
    Then the commit goes through without a warning
    """
    with debug_store_factory(debug=False) as store:
        quartz = Mineral(qid="Q1", name="quartz")
        store.store(quartz)
        store.commit()
        object.__setattr__(quartz, "name", "smoky quartz")

        with warnings.catch_warnings():
            warnings.simplefilter("error", dc.UntrackedMutationWarning)
            tid = store.commit()

        assert tid is None


def test_without_debug_the_bypass_write_is_silently_lost(debug_store_factory: StoreOpener) -> None:
    """Without debug this class of write IS lost, the documented default.

    Given a committed mineral renamed through object.__setattr__ in a store without debug
    When the store commits and the backend is reopened
    Then the mineral is still named "quartz"
    """
    with debug_store_factory(debug=False) as store:
        quartz = Mineral(qid="Q1", name="quartz")
        store.store(quartz)
        store.commit()
        object.__setattr__(quartz, "name", "smoky quartz")
        store.commit()

    reopened = debug_store_factory(debug=False)
    found = reopened.get(Mineral, qid="Q1")
    assert found is not None
    assert found.name == "quartz"
    reopened.close()
