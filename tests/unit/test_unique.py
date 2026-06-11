"""The unique secondary-key index (SDA delta 1)."""

from __future__ import annotations

import pytest

import datacrystal as dc
from tests.conftest import Locality, Mineral


def test_get_by_unique_key(store):
    store.root = [Mineral(qid="Q43010", name="quartz")]
    store.commit()
    assert store.get(Mineral, qid="Q43010").name == "quartz"
    assert store.get(Mineral, qid="Q-missing") is None


def test_get_requires_a_unique_field(store):
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()
    with pytest.raises(dc.QueryError):
        store.get(Mineral, name="quartz")  # name is not Unique


def test_duplicate_unique_key_rejected_atomically(store_factory):
    store = store_factory()
    store.root = [Mineral(qid="Q1", name="first")]
    store.commit()
    store.store(Mineral(qid="Q1", name="dupe"))
    with pytest.raises(dc.UniqueViolationError):
        store.commit()
    store.close()

    reopened = store_factory()  # nothing of the failed commit is visible
    assert reopened.get(Mineral, qid="Q1").name == "first"
    assert reopened.last_tid == 1
    reopened.close()


def test_intra_commit_duplicates_rejected(store):
    store.store(Locality(qid="QX", name="one"))
    store.store(Locality(qid="QX", name="two"))
    with pytest.raises(dc.UniqueViolationError):
        store.commit()


def test_failed_commit_burns_no_tid(store):
    store.root = [Mineral(qid="Q1", name="first")]
    assert store.commit() == 1
    store.store(Mineral(qid="Q1", name="dupe"))
    with pytest.raises(dc.UniqueViolationError):
        store.commit()
    # Fix the conflict and recommit: the TID sequence stays gapless.
    dupe = [o for o in store._new.values()][0]
    dupe.qid = "Q2"
    assert store.commit() == 2


def test_updating_the_key_moves_the_index_entry(store):
    store.root = [Mineral(qid="Q-old", name="m")]
    store.commit()
    m = store.get(Mineral, qid="Q-old")
    m.qid = "Q-new"
    store.commit()
    assert store.get(Mineral, qid="Q-old") is None
    assert store.get(Mineral, qid="Q-new") is m
