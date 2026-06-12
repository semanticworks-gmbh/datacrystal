"""store.upsert() — insert-or-merge by natural key (KICKOFF M4).

The contract under test: the existing live instance always survives a
match (identity is never broken), only fields that actually changed are
written (an unchanged re-import buffers nothing — the commit is
O(changed), not O(rows)), and one batch may upsert the same key twice.
"""

from __future__ import annotations

import warnings
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from tests.conftest import Locality, Mineral


@dc.entity
class DualKeyed:
    """Two natural keys — upsert must demand an explicit choice."""

    qid: Annotated[str, dc.Unique]
    catalog_no: Annotated[str | None, dc.Unique] = None


@dc.entity
class Keyless:
    label: str


# -- insert and merge --------------------------------------------------------


def test_upsert_inserts_when_the_key_is_unseen(store):
    quartz = Mineral(qid="Q43010", name="quartz")
    assert store.upsert(quartz) is quartz
    store.commit()
    assert store.get(Mineral, qid="Q43010") is quartz


def test_upsert_merges_into_the_existing_instance(store):
    quartz = Mineral(qid="Q43010", name="quartz", mohs=7.0)
    store.store(quartz)
    store.commit()

    survivor = store.upsert(Mineral(qid="Q43010", name="rock crystal", mohs=7.0))
    assert survivor is quartz  # identity is never broken
    assert quartz.name == "rock crystal"
    store.commit()
    assert store.pluck(Mineral, "name") == ["rock crystal"]


def test_unchanged_reimport_buffers_nothing(store):
    """The bulk-refresh property: re-upserting identical rows is a no-op —
    commit() finds nothing to do."""
    store.store(Mineral(qid="Q43010", name="quartz", mohs=7.0,
                        crystal_system="trigonal", tags=["common"]))
    store.commit()

    store.upsert(Mineral(qid="Q43010", name="quartz", mohs=7.0,
                         crystal_system="trigonal", tags=["common"]))
    assert store.commit() is None  # nothing buffered, no TID consumed


def test_partial_change_dirties_only_the_survivor(store):
    store.store(Mineral(qid="Q43010", name="quartz", mohs=7.0))
    store.commit()
    survivor = store.upsert(Mineral(qid="Q43010", name="quartz", mohs=7.5))
    assert survivor.mohs == 7.5
    assert store.commit() is not None


def test_upserting_the_canonical_instance_is_a_noop(store):
    quartz = Mineral(qid="Q43010", name="quartz")
    store.store(quartz)
    store.commit()
    assert store.upsert(quartz) is quartz
    assert store.commit() is None


def test_same_batch_upserts_of_one_key_merge(store):
    first = store.upsert(Mineral(qid="Q43010", name="quartz"))
    second = store.upsert(Mineral(qid="Q43010", name="rock crystal", mohs=7.0))
    assert second is first
    assert first.name == "rock crystal" and first.mohs == 7.0
    store.commit()
    assert store.count(Mineral) == 1


# -- reference-valued fields -------------------------------------------------


def test_same_reference_target_does_not_rewrite(store):
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.store(Mineral(qid="Q193563", name="azurite",
                        type_locality=dc.Lazy.of(tsumeb)))
    store.commit()
    # a fresh Lazy handle to the SAME target encodes to the same bytes
    store.upsert(Mineral(qid="Q193563", name="azurite",
                         type_locality=dc.Lazy.of(tsumeb)))
    assert store.commit() is None


def test_changed_reference_target_rewrites(store):
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.store(Mineral(qid="Q193563", name="azurite",
                        type_locality=dc.Lazy.of(tsumeb)))
    store.commit()
    chessy = Locality(qid="Q1075580", name="Chessy-les-Mines")
    survivor = store.upsert(Mineral(qid="Q193563", name="azurite",
                                    type_locality=dc.Lazy.of(chessy)))
    assert store.commit() is not None
    assert survivor.type_locality.get().name == "Chessy-les-Mines"


# -- key selection and validation ---------------------------------------------


def test_key_is_optional_only_with_exactly_one_unique_field(store):
    with pytest.raises(TypeError, match="2 Unique fields"):
        store.upsert(DualKeyed(qid="Q1", catalog_no="C-1"))
    with pytest.raises(TypeError, match="no Unique field"):
        store.upsert(Keyless(label="x"))
    row = DualKeyed(qid="Q1", catalog_no="C-1")
    assert store.upsert(row, key="qid") is row


def test_explicit_key_must_name_a_unique_field(store):
    with pytest.raises(dc.QueryError, match="not a Unique field"):
        store.upsert(Mineral(qid="Q1", name="quartz"), key="name")


def test_none_key_value_is_rejected(store):
    with pytest.raises(dc.QueryError, match="natural key must have a value"):
        store.upsert(DualKeyed(qid="Q1"), key="catalog_no")


def test_upsert_rejects_non_entities(store):
    with pytest.raises(dc.NotAnEntityError):
        store.upsert({"qid": "Q1"})


def test_upsert_on_unseen_type_stays_silent(store):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        store.upsert(Mineral(qid="Q1", name="quartz"))  # get-or-create idiom


def test_a_second_registered_instance_is_a_loud_conflict(store):
    store.store(Mineral(qid="Q43010", name="quartz"))
    store.commit()
    rival = Mineral(qid="Q43010", name="impostor")
    store.store(rival)  # registered as its own entity, NOT matched by upsert
    with pytest.raises(dc.UniqueViolationError, match="itself registered"):
        store.upsert(rival)


def test_plain_store_duplicates_still_raise_at_commit(store):
    """upsert() matches committed state and earlier upserts — entities
    buffered via plain store() keep their loud commit-time failure."""
    store.store(Mineral(qid="Q43010", name="quartz"))
    store.upsert(Mineral(qid="Q43010", name="rock crystal"))
    with pytest.raises(dc.UniqueViolationError):
        store.commit()


# -- interaction with delete (ADR-003) ----------------------------------------


def test_upsert_reuses_a_key_freed_by_a_buffered_delete(store):
    old = Mineral(qid="Q43010", name="quartz")
    store.store(old)
    store.commit()
    store.delete(old)
    fresh = store.upsert(Mineral(qid="Q43010", name="quartz v2"))
    assert fresh is not old  # the freed key inserts, never merges
    store.commit()
    assert store.count(Mineral) == 1
    assert store.get(Mineral, qid="Q43010").name == "quartz v2"


def test_deleting_a_pending_upsert_frees_its_batch_slot(store):
    first = store.upsert(Mineral(qid="Q43010", name="quartz"))
    store.delete(first)  # cancels the pending insert (NEW-cancel)
    second = store.upsert(Mineral(qid="Q43010", name="quartz v2"))
    assert second is not first
    store.commit()
    assert store.pluck(Mineral, "name") == ["quartz v2"]


def test_mutating_a_pending_upserts_key_frees_its_batch_slot(store):
    first = store.upsert(Mineral(qid="Q1", name="quartz"))
    first.qid = "Q2"
    second = store.upsert(Mineral(qid="Q1", name="azurite"))
    assert second is not first
    store.commit()
    assert store.count(Mineral) == 2


# -- frozen entities -----------------------------------------------------------


@dc.entity(frozen=True)
class SealedCert:
    cert_no: Annotated[str, dc.Unique]
    grade: str = "A"


def test_upsert_into_a_frozen_entity_raises_on_change(store):
    store.store(SealedCert(cert_no="C-1", grade="A"))
    store.commit()
    # identical data: nothing to write, the no-op path works on frozen too
    store.upsert(SealedCert(cert_no="C-1", grade="A"))
    assert store.commit() is None
    # a changed field would need a write — frozen means frozen, loudly
    with pytest.raises(dc.FrozenEntityError):
        store.upsert(SealedCert(cert_no="C-1", grade="B"))


# -- the batch memory survives a failed P2 --------------------------------------


def test_upsert_batch_memory_survives_a_p2_failure():
    from tests.unit.test_three_phase import _FailNTimes

    backend = _FailNTimes(MemoryBackend(), failures=1)
    store = dc.Store._from_backend(backend)
    first = store.upsert(Mineral(qid="Q43010", name="quartz"))
    with pytest.raises(OSError, match="injected"):
        store.commit()
    # the rollback re-buffered the entity; the same batch can keep upserting
    survivor = store.upsert(Mineral(qid="Q43010", name="rock crystal"))
    assert survivor is first
    store.commit()
    assert store.count(Mineral) == 1
    assert store.pluck(Mineral, "name") == ["rock crystal"]
    store.close()
