"""store.upsert() — insert-or-merge by natural key (KICKOFF M4).

The contract under test: the existing live instance always survives a
match (identity is never broken), only fields that actually changed are
written (an unchanged re-import buffers nothing — the commit is
O(changed), not O(rows)), and one batch may upsert the same key twice.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.sqlite import SqliteBackend
from tests.conftest import Locality, Mineral
from tests.unit.test_three_phase import _FailNTimes


@dc.entity
class DualKeyed:
    """Two natural keys — upsert must demand an explicit choice."""

    qid: Annotated[str, dc.Unique]
    catalog_no: Annotated[str | None, dc.Unique] = None


@dc.entity
class Keyless:
    """An entity without any natural key."""

    label: str


@dc.entity(frozen=True)
class SealedCert:
    """A frozen entity keyed by cert_no."""

    cert_no: Annotated[str, dc.Unique]
    grade: str = "A"


def _tagged_quartz(mohs: float = 7.0) -> Mineral:
    return Mineral(
        qid="Q43010", name="quartz", mohs=mohs, crystal_system="trigonal", tags=["common"]
    )


@pytest.fixture
def committed_quartz(store: dc.Store) -> Mineral:
    """Commit quartz (mohs 7.0) and return the live instance."""
    quartz = Mineral(qid="Q43010", name="quartz", mohs=7.0)
    store.store(quartz)
    store.commit()
    return quartz


@pytest.fixture
def azurite_at_tsumeb(store: dc.Store) -> Locality:
    """Commit azurite lazily pointing at Tsumeb Mine and return the locality."""
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.store(Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(tsumeb)))
    store.commit()
    return tsumeb


# -- insert and merge --------------------------------------------------------


def test_upsert_of_an_unseen_key_returns_the_given_instance(store: dc.Store) -> None:
    """Given a store without quartz
    When quartz is upserted
    Then the given instance comes back
    """
    quartz = Mineral(qid="Q43010", name="quartz")

    returned = store.upsert(quartz)

    assert returned is quartz


def test_upsert_of_an_unseen_key_inserts_it(store: dc.Store) -> None:
    """Given a store without quartz and quartz upserted
    When the store commits
    Then quartz is found by its qid as the identical instance
    """
    quartz = Mineral(qid="Q43010", name="quartz")
    store.upsert(quartz)
    store.commit()

    found = store.get(Mineral, qid="Q43010")

    assert found is quartz


def test_upsert_of_a_known_key_returns_the_existing_instance(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Identity is never broken.

    Given committed quartz
    When a new Mineral with quartz's qid and the name "rock crystal" is upserted
    Then the committed instance comes back
    """
    survivor = store.upsert(Mineral(qid="Q43010", name="rock crystal", mohs=7.0))

    assert survivor is committed_quartz


def test_upsert_of_a_known_key_updates_the_existing_instance(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz
    When a new Mineral with quartz's qid and the name "rock crystal" is upserted
    Then the committed instance is named "rock crystal"
    """
    store.upsert(Mineral(qid="Q43010", name="rock crystal", mohs=7.0))

    assert committed_quartz.name == "rock crystal"


def test_merged_upsert_persists_the_new_value(store: dc.Store, committed_quartz: Mineral) -> None:
    """Given committed quartz upserted with the name "rock crystal"
    When the store commits
    Then the only name in the store is "rock crystal"
    """
    store.upsert(Mineral(qid="Q43010", name="rock crystal", mohs=7.0))
    store.commit()

    names = store.pluck(Mineral, "name")

    assert names == ["rock crystal"]


def test_unchanged_reimport_buffers_nothing(store: dc.Store) -> None:
    """The bulk-refresh property: re-upserting identical rows is a no-op, no TID consumed.

    Given committed quartz
    When identical quartz is upserted and the store commits
    Then nothing is committed
    """
    store.store(_tagged_quartz())
    store.commit()
    store.upsert(_tagged_quartz())

    tid = store.commit()

    assert tid is None


def test_partial_change_updates_the_survivor(store: dc.Store, committed_quartz: Mineral) -> None:
    """Given committed quartz with mohs 7.0
    When quartz with mohs 7.5 is upserted
    Then the survivor has mohs 7.5
    """
    survivor = store.upsert(Mineral(qid="Q43010", name="quartz", mohs=7.5))

    assert survivor.mohs == 7.5


def test_partial_change_is_committed(store: dc.Store, committed_quartz: Mineral) -> None:
    """Given committed quartz upserted with mohs 7.5
    When the store commits
    Then a TID is returned
    """
    store.upsert(Mineral(qid="Q43010", name="quartz", mohs=7.5))

    tid = store.commit()

    assert tid is not None


def test_upserting_the_canonical_instance_returns_it(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz
    When that very instance is upserted
    Then the same instance comes back
    """
    returned = store.upsert(committed_quartz)

    assert returned is committed_quartz


def test_upserting_the_canonical_instance_buffers_nothing(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given committed quartz that was upserted as itself
    When the store commits
    Then nothing is committed
    """
    store.upsert(committed_quartz)

    tid = store.commit()

    assert tid is None


def test_same_batch_upserts_of_one_key_return_one_instance(store: dc.Store) -> None:
    """Given quartz upserted once in a batch
    When a second Mineral with the same qid is upserted
    Then the first instance comes back
    """
    first = store.upsert(Mineral(qid="Q43010", name="quartz"))

    second = store.upsert(Mineral(qid="Q43010", name="rock crystal", mohs=7.0))

    assert second is first


def test_same_batch_upserts_of_one_key_merge_their_fields(store: dc.Store) -> None:
    """Given quartz upserted once in a batch
    When a second Mineral with the same qid, name "rock crystal" and mohs 7.0 is upserted
    Then the first instance is named "rock crystal"
    And it has mohs 7.0
    """
    first = store.upsert(Mineral(qid="Q43010", name="quartz"))

    store.upsert(Mineral(qid="Q43010", name="rock crystal", mohs=7.0))

    assert first.name == "rock crystal"
    assert first.mohs == 7.0


def test_same_batch_upserts_of_one_key_commit_one_row(store: dc.Store) -> None:
    """Given quartz upserted twice in one batch
    When the store commits
    Then Mineral counts 1
    """
    store.upsert(Mineral(qid="Q43010", name="quartz"))
    store.upsert(Mineral(qid="Q43010", name="rock crystal", mohs=7.0))
    store.commit()

    count = store.count(Mineral)

    assert count == 1


# -- reference-valued fields -------------------------------------------------


def test_same_reference_target_does_not_rewrite(
    store: dc.Store, azurite_at_tsumeb: Locality
) -> None:
    """A fresh Lazy handle to the same target encodes to the same bytes.

    Given committed azurite pointing at Tsumeb Mine
    When azurite with a fresh Lazy to Tsumeb Mine is upserted and the store commits
    Then nothing is committed
    """
    store.upsert(
        Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(azurite_at_tsumeb))
    )

    tid = store.commit()

    assert tid is None


@pytest.fixture
def azurite_moved_to_chessy(store: dc.Store, azurite_at_tsumeb: Locality) -> Mineral:
    """Upsert azurite pointing at Chessy-les-Mines instead of Tsumeb Mine; return the survivor."""
    chessy = Locality(qid="Q1075580", name="Chessy-les-Mines")
    return store.upsert(
        Mineral(qid="Q193563", name="azurite", type_locality=dc.Lazy.of(chessy))
    )


def test_changed_reference_target_is_committed(
    store: dc.Store, azurite_moved_to_chessy: Mineral
) -> None:
    """Given committed azurite at Tsumeb Mine, upserted with a Lazy to Chessy-les-Mines
    When the store commits
    Then a TID is returned
    """
    tid = store.commit()

    assert tid is not None


def test_changed_reference_target_is_followed_by_the_survivor(
    azurite_moved_to_chessy: Mineral,
) -> None:
    """Given committed azurite at Tsumeb Mine, upserted with a Lazy to Chessy-les-Mines
    When the survivor's type_locality is fetched
    Then the locality is named "Chessy-les-Mines"
    """
    handle = azurite_moved_to_chessy.type_locality
    assert handle is not None

    locality = handle.get()

    assert locality.name == "Chessy-les-Mines"


# -- key selection and validation ---------------------------------------------


def test_upsert_of_two_unique_fields_demands_an_explicit_key(store: dc.Store) -> None:
    """Given an entity with two Unique fields
    When it is upserted without a key
    Then TypeError reports 2 Unique fields
    """
    with pytest.raises(TypeError, match="2 Unique fields"):
        store.upsert(DualKeyed(qid="Q1", catalog_no="C-1"))


def test_upsert_of_an_entity_without_unique_field_is_rejected(store: dc.Store) -> None:
    """Given an entity without a Unique field
    When it is upserted
    Then TypeError reports no Unique field
    """
    with pytest.raises(TypeError, match="no Unique field"):
        store.upsert(Keyless(label="x"))


def test_upsert_with_an_explicit_key_resolves_two_unique_fields(store: dc.Store) -> None:
    """Given an entity with two Unique fields
    When it is upserted with key="qid"
    Then the given instance comes back
    """
    row = DualKeyed(qid="Q1", catalog_no="C-1")

    returned = store.upsert(row, key="qid")

    assert returned is row


def test_explicit_key_must_name_a_unique_field(store: dc.Store) -> None:
    """Given a Mineral whose name is not Unique
    When it is upserted with key="name"
    Then QueryError says it is not a Unique field
    """
    with pytest.raises(dc.QueryError, match="not a Unique field"):
        store.upsert(Mineral(qid="Q1", name="quartz"), key="name")


def test_none_key_value_is_rejected(store: dc.Store) -> None:
    """Given an entity whose catalog_no is None
    When it is upserted with key="catalog_no"
    Then QueryError says a natural key must have a value
    """
    with pytest.raises(dc.QueryError, match="natural key must have a value"):
        store.upsert(DualKeyed(qid="Q1"), key="catalog_no")


def test_upsert_rejects_non_entities(store: dc.Store) -> None:
    """Given a store
    When a plain dict is upserted
    Then NotAnEntityError is raised
    """
    with pytest.raises(dc.NotAnEntityError):
        store.upsert({"qid": "Q1"})


def test_upsert_on_an_unseen_type_stays_silent(store: dc.Store) -> None:
    """The get-or-create idiom must not warn.

    Given a store with no committed Mineral
    When quartz is upserted with all warnings turned into errors
    Then the upsert returns quartz
    """
    quartz = Mineral(qid="Q1", name="quartz")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        returned = store.upsert(quartz)

    assert returned is quartz


def test_a_second_registered_instance_is_a_loud_conflict(store: dc.Store) -> None:
    """Given committed quartz and an impostor with the same qid stored on its own
    When the impostor is upserted
    Then UniqueViolationError says it is itself registered
    """
    store.store(Mineral(qid="Q43010", name="quartz"))
    store.commit()
    rival = Mineral(qid="Q43010", name="impostor")
    store.store(rival)

    with pytest.raises(dc.UniqueViolationError, match="itself registered"):
        store.upsert(rival)


def test_plain_store_duplicates_still_raise_at_commit(store: dc.Store) -> None:
    """upsert() matches committed state and earlier upserts, not entities buffered by store().

    Given quartz buffered with store() and a same-qid Mineral upserted
    When the store commits
    Then UniqueViolationError is raised
    """
    store.store(Mineral(qid="Q43010", name="quartz"))
    store.upsert(Mineral(qid="Q43010", name="rock crystal"))

    with pytest.raises(dc.UniqueViolationError):
        store.commit()


# -- interaction with delete (ADR-003) ----------------------------------------


@pytest.fixture
def requartzed(store: dc.Store, committed_quartz: Mineral) -> Mineral:
    """Delete committed quartz, then upsert "quartz v2" under the freed key; return the result."""
    store.delete(committed_quartz)
    return store.upsert(Mineral(qid="Q43010", name="quartz v2"))


def test_upsert_of_a_key_freed_by_a_buffered_delete_inserts_a_new_instance(
    requartzed: Mineral, committed_quartz: Mineral
) -> None:
    """The freed key inserts, never merges.

    Given committed quartz deleted but not yet committed
    When a Mineral with quartz's qid is upserted
    Then a different instance than the deleted one comes back
    """
    assert requartzed is not committed_quartz


def test_upsert_of_a_freed_key_commits_one_row(store: dc.Store, requartzed: Mineral) -> None:
    """Given quartz deleted and "quartz v2" upserted under its key
    When the store commits
    Then Mineral counts 1
    """
    store.commit()

    count = store.count(Mineral)

    assert count == 1


def test_upsert_of_a_freed_key_commits_the_new_name(store: dc.Store, requartzed: Mineral) -> None:
    """Given quartz deleted and "quartz v2" upserted under its key
    When the store commits
    Then the mineral under that qid is named "quartz v2"
    """
    store.commit()

    found = store.get(Mineral, qid="Q43010")

    assert found is not None
    assert found.name == "quartz v2"


def test_deleting_a_pending_upsert_frees_its_batch_slot(store: dc.Store) -> None:
    """Given quartz upserted and then deleted (cancelling the pending insert)
    When "quartz v2" with the same qid is upserted
    Then a different instance comes back
    """
    first = store.upsert(Mineral(qid="Q43010", name="quartz"))
    store.delete(first)

    second = store.upsert(Mineral(qid="Q43010", name="quartz v2"))

    assert second is not first


def test_reusing_a_cancelled_upsert_key_commits_the_new_name(store: dc.Store) -> None:
    """Given quartz upserted, deleted, and "quartz v2" upserted under the same qid
    When the store commits
    Then the only name in the store is "quartz v2"
    """
    first = store.upsert(Mineral(qid="Q43010", name="quartz"))
    store.delete(first)
    store.upsert(Mineral(qid="Q43010", name="quartz v2"))
    store.commit()

    names = store.pluck(Mineral, "name")

    assert names == ["quartz v2"]


def test_mutating_a_pending_upserts_key_frees_its_batch_slot(store: dc.Store) -> None:
    """Given a pending upsert of qid "Q1" whose qid was then changed to "Q2"
    When a Mineral with qid "Q1" is upserted
    Then a different instance comes back
    """
    first = store.upsert(Mineral(qid="Q1", name="quartz"))
    first.qid = "Q2"

    second = store.upsert(Mineral(qid="Q1", name="azurite"))

    assert second is not first


def test_both_rekeyed_upserts_are_committed(store: dc.Store) -> None:
    """Given a pending upsert re-keyed from "Q1" to "Q2" and another upsert of "Q1"
    When the store commits
    Then Mineral counts 2
    """
    first = store.upsert(Mineral(qid="Q1", name="quartz"))
    first.qid = "Q2"
    store.upsert(Mineral(qid="Q1", name="azurite"))
    store.commit()

    count = store.count(Mineral)

    assert count == 2


# -- frozen entities -----------------------------------------------------------


@pytest.fixture
def committed_cert(store: dc.Store) -> None:
    """Commit the frozen certificate C-1 with grade A."""
    store.store(SealedCert(cert_no="C-1", grade="A"))
    store.commit()


@pytest.mark.usefixtures("committed_cert")
def test_upsert_of_identical_data_into_a_frozen_entity_buffers_nothing(
    store: dc.Store,
) -> None:
    """The no-op path works on frozen entities too.

    Given a committed frozen certificate C-1 with grade A
    When identical data is upserted and the store commits
    Then nothing is committed
    """
    store.upsert(SealedCert(cert_no="C-1", grade="A"))

    tid = store.commit()

    assert tid is None


@pytest.mark.usefixtures("committed_cert")
def test_upsert_of_changed_data_into_a_frozen_entity_raises(store: dc.Store) -> None:
    """A changed field would need a write: frozen means frozen, loudly.

    Given a committed frozen certificate C-1 with grade A
    When grade B is upserted
    Then FrozenEntityError is raised
    """
    with pytest.raises(dc.FrozenEntityError):
        store.upsert(SealedCert(cert_no="C-1", grade="B"))


# -- the batch memory survives a failed P2 --------------------------------------


@pytest.fixture
def failed_first_commit() -> tuple[dc.Store, Mineral]:
    """Return a store whose first commit of upserted quartz failed in P2, and that quartz."""
    store = dc.Store._from_backend(_FailNTimes(MemoryBackend(), failures=1))
    first = store.upsert(Mineral(qid="Q43010", name="quartz"))
    with pytest.raises(OSError, match="injected"):
        store.commit()
    return store, first


def test_upsert_after_a_p2_failure_still_merges_into_the_batch(
    failed_first_commit: tuple[dc.Store, Mineral],
) -> None:
    """The rollback re-buffered the entity, so the same batch can keep upserting.

    Given quartz upserted and its first commit failed in P2
    When a same-qid Mineral named "rock crystal" is upserted
    Then the first instance comes back
    """
    store, first = failed_first_commit

    survivor = store.upsert(Mineral(qid="Q43010", name="rock crystal"))

    assert survivor is first
    store.close()


def test_upsert_batch_commits_one_row_after_a_p2_failure(
    failed_first_commit: tuple[dc.Store, Mineral],
) -> None:
    """Given quartz upserted, its first commit failed in P2, and "rock crystal" upserted
    When the store commits
    Then Mineral counts 1
    """
    store, _ = failed_first_commit
    store.upsert(Mineral(qid="Q43010", name="rock crystal"))
    store.commit()

    count = store.count(Mineral)

    assert count == 1
    store.close()


def test_upsert_batch_commits_the_merged_name_after_a_p2_failure(
    failed_first_commit: tuple[dc.Store, Mineral],
) -> None:
    """Given quartz upserted, its first commit failed in P2, and "rock crystal" upserted
    When the store commits
    Then the only name in the store is "rock crystal"
    """
    store, _ = failed_first_commit
    store.upsert(Mineral(qid="Q43010", name="rock crystal"))
    store.commit()

    names = store.pluck(Mineral, "name")

    assert names == ["rock crystal"]
    store.close()


# -- a failed P2 on an upsert onto a committed record, on both backends ----------


@dataclass
class _FlakyCabinet:
    """A store over a backend whose next commit can be armed to fail, plus a reopen."""

    store: dc.Store
    backend: _FailNTimes
    reopen: Callable[[], dc.Store]


@pytest.fixture(params=["memory", "sqlite"])
def flaky_cabinet_with_quartz(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[_FlakyCabinet]:
    """Yield a cabinet with committed quartz "quartz" whose backend can fail the next commit."""
    opened: list[dc.Store] = []
    if request.param == "memory":
        memory = MemoryBackend()
        backend = _FailNTimes(memory, failures=0)

        def reopen() -> dc.Store:
            reopened = dc.Store._from_backend(memory)
            opened.append(reopened)
            return reopened
    else:
        directory = tmp_path / "cabinet"
        directory.mkdir()
        backend = _FailNTimes(SqliteBackend(directory / "data.sqlite"), failures=0)

        def reopen() -> dc.Store:
            reopened = dc.Store.open(directory)
            opened.append(reopened)
            return reopened

    store = dc.Store._from_backend(backend)
    store.store(Mineral(qid="Q43010", name="quartz"))
    store.commit()
    yield _FlakyCabinet(store, backend, reopen)
    store.close()
    for reopened in opened:
        reopened.close()


def test_retry_after_a_failed_commit_persists_the_upserted_name(
    flaky_cabinet_with_quartz: _FlakyCabinet,
) -> None:
    """Given quartz committed, "rock crystal" upserted onto it and its commit failed in P2
    When the store retries the commit, closes and reopens
    Then the only name in the reopened store is "rock crystal"
    """
    cabinet = flaky_cabinet_with_quartz
    cabinet.store.upsert(Mineral(qid="Q43010", name="rock crystal"))
    cabinet.backend.failures = 1
    with pytest.raises(OSError, match="injected"):
        cabinet.store.commit()
    cabinet.store.commit()
    cabinet.store.close()

    names = cabinet.reopen().pluck(Mineral, "name")

    assert names == ["rock crystal"]


def test_retry_after_a_failed_commit_still_hands_out_the_upserted_instance(
    flaky_cabinet_with_quartz: _FlakyCabinet,
) -> None:
    """Given quartz committed, "rock crystal" upserted onto it and its commit failed in P2
    When the store retries the commit
    Then the store still hands out the instance the upsert returned (identity map, not disk)
    """
    cabinet = flaky_cabinet_with_quartz
    survivor = cabinet.store.upsert(Mineral(qid="Q43010", name="rock crystal"))
    cabinet.backend.failures = 1
    with pytest.raises(OSError, match="injected"):
        cabinet.store.commit()
    cabinet.store.commit()

    found = cabinet.store.get(Mineral, qid="Q43010")

    assert found is survivor


# -- an upsert after a commit that failed validation --------------------------------


@pytest.fixture
def quartz_with_rejected_duplicate(committed_quartz: Mineral, store: dc.Store) -> Mineral:
    """Return the buffered duplicate whose commit raised UniqueViolationError on quartz's qid."""
    duplicate = Mineral(qid="Q43010", name="duplicate")
    store.store(duplicate)
    with pytest.raises(dc.UniqueViolationError):
        store.commit()
    return duplicate


@pytest.mark.usefixtures("quartz_with_rejected_duplicate")
def test_upsert_after_a_rejected_commit_merges_into_the_committed_record(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Matching sees committed state, not the plain store() the rejected commit kept.

    Given quartz committed and a plain-stored duplicate qid whose commit was rejected
    When a same-qid Mineral named "rock crystal" is upserted
    Then the committed quartz comes back
    """
    survivor = store.upsert(Mineral(qid="Q43010", name="rock crystal"))

    assert survivor is committed_quartz


@pytest.mark.usefixtures("quartz_with_rejected_duplicate")
def test_upsert_after_a_rejected_commit_applies_the_new_name_to_the_committed_record(
    store: dc.Store, committed_quartz: Mineral
) -> None:
    """Given quartz committed and a plain-stored duplicate qid whose commit was rejected
    When a same-qid Mineral named "rock crystal" is upserted
    Then the committed record is named "rock crystal"
    """
    survivor = store.upsert(Mineral(qid="Q43010", name="rock crystal"))

    assert survivor.name == "rock crystal"


@pytest.mark.usefixtures("quartz_with_rejected_duplicate")
def test_commit_after_an_upsert_still_rejects_the_kept_duplicate(store: dc.Store) -> None:
    """A rejected commit keeps its buffers, so the duplicate fails the next commit as well.

    Given quartz committed and a plain-stored duplicate qid whose commit was rejected
    When a same-qid Mineral is upserted and the store commits again
    Then UniqueViolationError is raised
    """
    store.upsert(Mineral(qid="Q43010", name="rock crystal"))

    with pytest.raises(dc.UniqueViolationError):
        store.commit()


@pytest.mark.usefixtures("quartz_with_rejected_duplicate")
def test_commit_after_discarding_the_rejected_duplicate_persists_the_upsert(
    store: dc.Store, store_factory: Callable[[], dc.Store]
) -> None:
    """Given quartz committed and a plain-stored duplicate qid whose commit was rejected
    When the store discards, upserts "rock crystal" onto quartz, commits, closes and reopens
    Then the only name in the reopened store is "rock crystal"
    """
    store.discard()
    store.upsert(Mineral(qid="Q43010", name="rock crystal"))
    store.commit()
    store.close()

    with store_factory() as reopened:
        names = reopened.pluck(Mineral, "name")

    assert names == ["rock crystal"]
