"""#110 — opt-in eager dangling-ref check at commit (the ADR-003 dev bridge).

v0.x deletes are *unchecked* (ADR-003): deleting a still-referenced entity is
fine until something *later* follows the stale ref and raises DanglingRefError,
far from the delete that caused it. ``strict_deletes=True`` raises at the
offending commit naming the referrers; ``debug=True`` alone runs the same check
but only warns (DanglingDeleteWarning) so a bulk re-import is never bricked. The
flagged set equals ``incoming(dead)`` AFTER the P3 folds — prior-commit AND
same-commit-new referrers, never co-deleted ones. The seam is ``incoming(dead)``
(ADR-003, line 108). Unarmed, the commit path is unchanged and pays nothing.

Parametrized over both backends (memory + sqlite) — the seam is the protocol,
both sides must behave identically.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from tests.conftest import Locality, Mineral

# ``store_factory`` (conftest) opens with the defaults; this story needs the
# dev-mode flags, so build a parametrized factory that threads them through.
StoreOpener = Callable[..., dc.Store]


@pytest.fixture(params=["memory", "sqlite"])
def dev_store_factory(request: pytest.FixtureRequest, tmp_path) -> StoreOpener:  # type: ignore[no-untyped-def]
    if request.param == "memory":
        backend = MemoryBackend()

        def open_store(**kw: object) -> dc.Store:
            return dc.Store._from_backend(backend, **kw)  # type: ignore[arg-type]
    else:

        def open_store(**kw: object) -> dc.Store:
            return dc.Store.open(tmp_path / "store", lock_ttl=0.5, **kw)  # type: ignore[arg-type]

    return open_store


def _quartz() -> Mineral:
    return Mineral(qid="Q43010", name="quartz", crystal_system="trigonal", mohs=7.0)


# -- strict_deletes raises, naming the dangling referrer ----------------------


def test_strict_raises_on_prior_commit_referrer(dev_store_factory: StoreOpener) -> None:
    store = dev_store_factory(strict_deletes=True)
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    azurite = Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(tsumeb))
    store.store(azurite)
    store.commit()  # azurite -> tsumeb persisted in a PRIOR commit

    store.delete(tsumeb)  # the Mineral survives, still pointing at the Locality
    with pytest.raises(dc.DanglingRefError, match="Mineral"):
        store.commit()
    store.close()


def test_warn_mode_does_not_brick_the_commit(dev_store_factory: StoreOpener) -> None:
    store = dev_store_factory(debug=True)  # debug => warn-only, never raise
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    azurite = Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(tsumeb))
    store.store(azurite)
    store.commit()

    store.delete(tsumeb)
    with pytest.warns(dc.DanglingDeleteWarning, match="Mineral"):
        tid = store.commit()
    assert tid is not None  # the bulk re-import is NOT bricked — it commits
    # The dangle is real and survives: the Locality is gone from the store.
    assert store.get(Locality, qid="Q571997") is None
    store.close()


def test_strict_flags_same_commit_new_referrer(dev_store_factory: StoreOpener) -> None:
    """The corrected concern 3: a referrer that is NEW in the SAME commit as the
    delete is not yet in the P1-stale reverse map; the check must union this
    commit's harvested refs. AC1 says the flagged set is incoming(dead) AFTER
    the folds — so the new referrer must be named."""
    store = dev_store_factory(strict_deletes=True)
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.store(tsumeb)
    store.commit()

    # Same commit: create a NEW Mineral pointing at tsumeb AND delete tsumeb.
    azurite = Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(tsumeb))
    store.store(azurite)
    store.delete(tsumeb)
    with pytest.raises(dc.DanglingRefError, match="Mineral"):
        store.commit()
    store.close()


def test_strict_raise_leaves_the_commit_retryable(dev_store_factory: StoreOpener) -> None:
    """The check raises in P1 *before* the TID is allocated (invariant 5), so a
    rejected commit leaves the delete buffer intact — resolve the dangle (delete
    the referrer too) and re-commit, gaplessly."""
    store = dev_store_factory(strict_deletes=True)
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    azurite = Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(tsumeb))
    store.store(azurite)
    create_tid = store.commit()
    assert create_tid is not None

    store.delete(tsumeb)
    with pytest.raises(dc.DanglingRefError):
        store.commit()  # rejected — no TID consumed, the delete stays buffered
    store.delete(azurite)  # resolve the dangle: the referrer goes too
    retry_tid = store.commit()
    assert retry_tid == create_tid + 1  # gapless: the rejected commit took no TID
    assert store.get(Locality, qid="Q571997") is None
    assert store.get(Mineral, qid="Q193563") is None
    store.close()


def test_strict_flags_same_commit_dirty_referrer(dev_store_factory: StoreOpener) -> None:
    """A referrer that gains the ref by being mutated (DIRTY) in the same commit
    as the delete is also folded in by P3 — and so must be flagged."""
    store = dev_store_factory(strict_deletes=True)
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    azurite = Mineral(qid="Q193563", name="azurite")  # no ref yet
    store.store(tsumeb)
    store.store(azurite)
    store.commit()

    azurite.type_locality = dc.Lazy.of(tsumeb)  # DIRTY: gains the ref this commit
    store.delete(tsumeb)
    with pytest.raises(dc.DanglingRefError, match="Mineral"):
        store.commit()
    store.close()


# -- the co-deleted / cancelled-new exemptions (AC3) --------------------------


def test_co_deleted_referrer_is_silent(dev_store_factory: StoreOpener) -> None:
    """A referrer also deleted in the same commit is NOT a dangle — its outgoing
    edges vanish (ADR-003). Deleting both the Mineral and its Locality together
    must be silent."""
    store = dev_store_factory(strict_deletes=True)
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    azurite = Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(tsumeb))
    store.store(azurite)
    store.commit()

    store.delete(azurite)  # the referrer goes too
    store.delete(tsumeb)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any DanglingDeleteWarning would fail here
        tid = store.commit()  # raises nothing, warns nothing
    assert tid is not None
    store.close()


def test_cancelled_new_referrer_is_never_flagged(dev_store_factory: StoreOpener) -> None:
    """Deleting a never-committed NEW entity cancels its pending insert (ADR-003
    rule 1); it is gone from both buffers, so it can never be flagged as a
    surviving referrer."""
    store = dev_store_factory(strict_deletes=True)
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.store(tsumeb)
    store.commit()

    # A NEW Mineral pointing at tsumeb, then both the Mineral (cancel) and the
    # Locality (real) are deleted in one commit — the Mineral never existed.
    azurite = Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(tsumeb))
    store.store(azurite)
    store.delete(azurite)  # cancels the never-committed insert
    store.delete(tsumeb)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        tid = store.commit()
    assert tid is not None
    store.close()


def test_delete_with_no_surviving_referrer_is_silent(dev_store_factory: StoreOpener) -> None:
    store = dev_store_factory(strict_deletes=True)
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    store.delete(quartz)  # nothing points at it
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert store.commit() is not None
    store.close()


# -- unarmed: the commit path is unchanged and pays nothing -------------------


def test_unarmed_store_leaves_reverse_index_unbuilt(dev_store_factory: StoreOpener) -> None:
    """AC2: without dev mode the commit path is byte-for-byte unchanged and pays
    nothing — the reverse index stays unbuilt (spec §5), no warning, no raise,
    the dangle stays deferred to dereference."""
    store = dev_store_factory()  # neither debug nor strict_deletes
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    azurite = Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(tsumeb))
    store.store(azurite)
    store.commit()

    store.delete(tsumeb)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # NO DanglingDeleteWarning unarmed
        tid = store.commit()
    assert tid is not None
    # The eager check never forced ensure_reverse(): the index stays unbuilt
    # (the unwatched store pays nothing — invariant 11 / spec §5).
    assert store._index.reverse_built is False  # pyright: ignore[reportPrivateUsage]
    store.close()
