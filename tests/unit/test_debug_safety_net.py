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

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from tests.conftest import Mineral


@dc.entity
class Scan:
    """A specimen scan: the raw sensor buffer is a bytearray — mutable, not
    a container the engine wraps, hence the honest untracked vector."""

    name: str
    raw: bytearray


def test_bypass_write_is_detected_and_rescued():
    backend = MemoryBackend()
    with dc.Store._from_backend(backend, debug=True) as store:
        m = Mineral(qid="Q1", name="quartz")
        store.store(m)
        store.commit()
        object.__setattr__(m, "name", "smoky quartz")  # slips past the hook
        with pytest.warns(dc.UntrackedMutationWarning, match="Mineral"):
            store.commit()
    reopened = dc.Store._from_backend(backend)
    found = reopened.get(Mineral, qid="Q1")
    assert found is not None and found.name == "smoky quartz"  # rescued, not lost
    reopened.close()


def test_bytearray_mutation_is_detected_on_live_and_hydrated_entities():
    backend = MemoryBackend()
    with dc.Store._from_backend(backend, debug=True) as store:
        scan = Scan(name="alpha", raw=bytearray(b"\x00\x01"))
        oid = store.store(scan)
        store.commit()
        scan.raw.append(0x02)  # in-place: no hook anywhere on this path
        with pytest.warns(dc.UntrackedMutationWarning, match="Scan"):
            store.commit()
        del scan

    with dc.Store._from_backend(backend, debug=True) as store:
        hydrated = store.get_many([oid])[0]
        # known v0.1 mapping: bytearray round-trips as bytes (msgpack bin),
        # so the live-object vector above cannot recur after hydration —
        # bypass-write instead to prove hydrated entities are fingerprinted
        assert bytes(hydrated.raw) == b"\x00\x01\x02"
        object.__setattr__(hydrated, "name", "beta")
        with pytest.warns(dc.UntrackedMutationWarning, match="Scan"):
            store.commit()


def test_clean_entities_do_not_false_positive():
    backend = MemoryBackend()
    with dc.Store._from_backend(backend, debug=True) as store:
        m = Mineral(qid="Q1", name="quartz", tags=["smoky", "rutile"])
        store.store(m)
        store.commit()
        # tracked changes through hook and container, plus a re-read
        m.mohs = 7.0
        m.tags.append("phantom")
        with warnings.catch_warnings():
            warnings.simplefilter("error", dc.UntrackedMutationWarning)
            store.commit()
            assert store.get(Mineral, qid="Q1") is m
            store.commit()  # nothing pending, fingerprints all current


def test_without_debug_the_bypass_write_is_silently_lost():
    """Pins the documented default: detection costs O(live set), so it is
    opt-in — without it, this class of write IS lost (use debug in dev)."""
    backend = MemoryBackend()
    with dc.Store._from_backend(backend) as store:
        m = Mineral(qid="Q1", name="quartz")
        store.store(m)
        store.commit()
        object.__setattr__(m, "name", "smoky quartz")
        with warnings.catch_warnings():
            warnings.simplefilter("error", dc.UntrackedMutationWarning)
            store.commit()
    reopened = dc.Store._from_backend(backend)
    found = reopened.get(Mineral, qid="Q1")
    assert found is not None and found.name == "quartz"  # the honest, sad truth
    reopened.close()
