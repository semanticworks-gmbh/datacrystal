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
from pathlib import Path

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from tests.conftest import Locality, Mineral

# ``store_factory`` (conftest) opens with the defaults; this story needs the
# dev-mode flags, so build a parametrized factory that threads them through.
StoreOpener = Callable[..., dc.Store]


@pytest.fixture(params=["memory", "sqlite"])
def dev_store_factory(request: pytest.FixtureRequest, tmp_path: Path) -> StoreOpener:
    """Return an opener threading the dev-mode flags `strict_deletes` and `debug` through."""
    if request.param == "memory":
        backend = MemoryBackend()

        def open_store(*, strict_deletes: bool = False, debug: bool = False) -> dc.Store:
            return dc.Store._from_backend(backend, strict_deletes=strict_deletes, debug=debug)
    else:

        def open_store(*, strict_deletes: bool = False, debug: bool = False) -> dc.Store:
            return dc.Store.open(
                tmp_path / "store", lock_ttl=0.5, strict_deletes=strict_deletes, debug=debug
            )

    return open_store


def _quartz() -> Mineral:
    return Mineral(qid="Q43010", name="quartz", crystal_system="trigonal", mohs=7.0)


def _tsumeb() -> Locality:
    return Locality(qid="Q571997", name="Tsumeb Mine")


def _azurite_at(locality: Locality) -> Mineral:
    return Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(locality))


# -- strict_deletes raises, naming the dangling referrer ----------------------


def test_strict_commit_names_a_referrer_from_a_prior_commit(dev_store_factory: StoreOpener) -> None:
    """Given azurite pointing at Tsumeb Mine, committed earlier, in a strict_deletes store
    When Tsumeb Mine is deleted and the store commits
    Then DanglingRefError names Mineral
    """
    store = dev_store_factory(strict_deletes=True)
    tsumeb = _tsumeb()
    store.store(_azurite_at(tsumeb))
    store.commit()
    store.delete(tsumeb)

    with pytest.raises(dc.DanglingRefError, match="Mineral"):
        store.commit()

    store.close()


def test_warn_mode_commit_goes_through(dev_store_factory: StoreOpener) -> None:
    """Given azurite pointing at Tsumeb Mine, committed earlier, in a debug store
    When Tsumeb Mine is deleted and the store commits
    Then DanglingDeleteWarning names Mineral
    And a TID is returned, the bulk re-import is not bricked
    """
    store = dev_store_factory(debug=True)
    tsumeb = _tsumeb()
    store.store(_azurite_at(tsumeb))
    store.commit()
    store.delete(tsumeb)

    with pytest.warns(dc.DanglingDeleteWarning, match="Mineral"):
        tid = store.commit()

    assert tid is not None
    store.close()


def test_warn_mode_dangle_is_real(dev_store_factory: StoreOpener) -> None:
    """Given azurite pointing at Tsumeb Mine, committed earlier, in a debug store
    When Tsumeb Mine is deleted and the store commits with a warning
    Then Tsumeb Mine is gone from the store
    """
    store = dev_store_factory(debug=True)
    tsumeb = _tsumeb()
    store.store(_azurite_at(tsumeb))
    store.commit()
    store.delete(tsumeb)
    with pytest.warns(dc.DanglingDeleteWarning):
        store.commit()

    found = store.get(Locality, qid="Q571997")

    assert found is None
    store.close()


def test_strict_flags_a_referrer_new_in_the_same_commit(dev_store_factory: StoreOpener) -> None:
    """A referrer NEW in the same commit as the delete is not yet in the P1-stale reverse map.

    The check must union this commit's harvested refs: AC1 says the flagged set is
    incoming(dead) AFTER the folds.

    Given a committed Tsumeb Mine in a strict_deletes store
    And a new azurite pointing at it and Tsumeb Mine deleted, in one pending commit
    When the store commits
    Then DanglingRefError names Mineral
    """
    store = dev_store_factory(strict_deletes=True)
    tsumeb = _tsumeb()
    store.store(tsumeb)
    store.commit()
    store.store(_azurite_at(tsumeb))
    store.delete(tsumeb)

    with pytest.raises(dc.DanglingRefError, match="Mineral"):
        store.commit()

    store.close()


def test_strict_rejection_consumes_no_tid(dev_store_factory: StoreOpener) -> None:
    """The check raises in P1 before the TID is allocated (invariant 5).

    Given azurite pointing at Tsumeb Mine committed as the first TID, in a strict_deletes store
    And Tsumeb Mine deleted, with the commit rejected
    When azurite is deleted too and the store commits
    Then the TID is the first plus one
    """
    store = dev_store_factory(strict_deletes=True)
    tsumeb = _tsumeb()
    azurite = _azurite_at(tsumeb)
    store.store(azurite)
    create_tid = store.commit()
    assert create_tid is not None
    store.delete(tsumeb)
    with pytest.raises(dc.DanglingRefError):
        store.commit()
    store.delete(azurite)

    retry_tid = store.commit()

    assert retry_tid == create_tid + 1
    store.close()


def test_strict_rejection_keeps_the_delete_buffered(dev_store_factory: StoreOpener) -> None:
    """Given azurite pointing at Tsumeb Mine, committed, in a strict_deletes store
    And Tsumeb Mine deleted, with the commit rejected
    When azurite is deleted too and the store commits
    Then Tsumeb Mine is gone
    And azurite is gone
    """
    store = dev_store_factory(strict_deletes=True)
    tsumeb = _tsumeb()
    azurite = _azurite_at(tsumeb)
    store.store(azurite)
    store.commit()
    store.delete(tsumeb)
    with pytest.raises(dc.DanglingRefError):
        store.commit()
    store.delete(azurite)

    store.commit()

    assert store.get(Locality, qid="Q571997") is None
    assert store.get(Mineral, qid="Q193563") is None
    store.close()


def test_strict_flags_a_referrer_dirtied_in_the_same_commit(dev_store_factory: StoreOpener) -> None:
    """A referrer that gains the ref by being mutated in the delete's commit is folded in by P3.

    Given committed Tsumeb Mine and azurite without a ref, in a strict_deletes store
    And azurite pointed at Tsumeb Mine and Tsumeb Mine deleted, in one pending commit
    When the store commits
    Then DanglingRefError names Mineral
    """
    store = dev_store_factory(strict_deletes=True)
    tsumeb = _tsumeb()
    azurite = Mineral(qid="Q193563", name="azurite")
    store.store(tsumeb)
    store.store(azurite)
    store.commit()
    azurite.type_locality = dc.Lazy.of(tsumeb)
    store.delete(tsumeb)

    with pytest.raises(dc.DanglingRefError, match="Mineral"):
        store.commit()

    store.close()


# -- the co-deleted / cancelled-new exemptions (AC3) --------------------------


def test_co_deleted_referrer_is_silent(dev_store_factory: StoreOpener) -> None:
    """A referrer also deleted in the same commit is not a dangle: its outgoing edges vanish.

    Given azurite pointing at Tsumeb Mine, committed, in a strict_deletes store
    When both are deleted and the store commits with all warnings turned into errors
    Then a TID is returned
    """
    store = dev_store_factory(strict_deletes=True)
    tsumeb = _tsumeb()
    azurite = _azurite_at(tsumeb)
    store.store(azurite)
    store.commit()
    store.delete(azurite)
    store.delete(tsumeb)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        tid = store.commit()

    assert tid is not None
    store.close()


def test_cancelled_new_referrer_is_never_flagged(dev_store_factory: StoreOpener) -> None:
    """Deleting a never-committed NEW entity cancels its pending insert (ADR-003 rule 1).

    It is gone from both buffers, so it can never be flagged as a surviving referrer.

    Given a committed Tsumeb Mine, in a strict_deletes store
    And a new azurite pointing at it, stored and then deleted, with Tsumeb Mine deleted
    When the store commits with all warnings turned into errors
    Then a TID is returned
    """
    store = dev_store_factory(strict_deletes=True)
    tsumeb = _tsumeb()
    store.store(tsumeb)
    store.commit()
    azurite = _azurite_at(tsumeb)
    store.store(azurite)
    store.delete(azurite)
    store.delete(tsumeb)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        tid = store.commit()

    assert tid is not None
    store.close()


def test_delete_with_no_surviving_referrer_is_silent(dev_store_factory: StoreOpener) -> None:
    """Given a committed quartz nothing points at, in a strict_deletes store
    When quartz is deleted and the store commits with all warnings turned into errors
    Then a TID is returned
    """
    store = dev_store_factory(strict_deletes=True)
    quartz = _quartz()
    store.store(quartz)
    store.commit()
    store.delete(quartz)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        tid = store.commit()

    assert tid is not None
    store.close()


# -- unarmed: the commit path is unchanged and pays nothing -------------------


def test_unarmed_store_commits_a_dangling_delete_silently(dev_store_factory: StoreOpener) -> None:
    """AC2: without dev mode the dangle stays deferred to dereference: no warning, no raise.

    Given azurite pointing at Tsumeb Mine, committed, in a default (non-dev) store
    When Tsumeb Mine is deleted and the store commits with all warnings turned into errors
    Then a TID is returned
    """
    store = dev_store_factory()
    tsumeb = _tsumeb()
    store.store(_azurite_at(tsumeb))
    store.commit()
    store.delete(tsumeb)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        tid = store.commit()

    assert tid is not None
    store.close()


def test_unarmed_store_leaves_reverse_index_unbuilt(dev_store_factory: StoreOpener) -> None:
    """The eager check never forced ensure_reverse(): the unwatched store pays nothing.

    Given azurite pointing at Tsumeb Mine, committed, in a default (non-dev) store
    When Tsumeb Mine is deleted and the store commits
    Then the reverse index is still unbuilt (invariant 11, spec section 5)
    """
    store = dev_store_factory()
    tsumeb = _tsumeb()
    store.store(_azurite_at(tsumeb))
    store.commit()
    store.delete(tsumeb)

    store.commit()

    assert store._index.reverse_built is False
    store.close()
