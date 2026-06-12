"""The decode-level read path (ROADMAP item 4, 2026-06-12 amendment):
count() / pluck() without entity construction, bulk unique-key get_many(),
and the loud-empty UnseenTypeWarning."""

from __future__ import annotations

import gc
import warnings

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of
from tests.conftest import Locality, Mineral

M = dc.fields(Mineral)


def _cabinet(store: dc.Store) -> None:
    store.store(Mineral(qid="Q43010", name="quartz", crystal_system="trigonal",
                        mohs=7.0, tags=["common"]))
    store.store(Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic",
                        mohs=3.8))
    store.store(Mineral(qid="Q134583", name="topaz", crystal_system="orthorhombic",
                        mohs=8.0))
    store.commit()


# -- count ---------------------------------------------------------------------


def test_count_class_is_the_extent(store):
    _cabinet(store)
    assert store.count(Mineral) == 3


def test_count_indexed_condition_is_bitmap_cardinality(store):
    _cabinet(store)
    assert store.count(M.crystal_system == "trigonal") == 1
    assert store.count(M.crystal_system.in_(["trigonal", "monoclinic"])) == 2
    assert store.count(M.crystal_system == "cubic") == 0


def test_count_residual_scans_without_hydration(store):
    _cabinet(store)
    gc.collect()
    live_before = len(store._registry)
    assert store.count(M.mohs >= 7.0) == 2
    assert store.count((M.crystal_system == "trigonal") & (M.mohs >= 7.0)) == 1
    gc.collect()
    assert len(store._registry) == live_before  # constructed nothing


def test_count_reads_committed_state(store):
    _cabinet(store)
    store.store(Mineral(qid="Q-pending", name="pending"))
    assert store.count(Mineral) == 3  # the buffered insert is not committed
    store.commit()
    assert store.count(Mineral) == 4
    store.delete(Mineral, qid="Q-pending")
    assert store.count(Mineral) == 4  # the buffered delete is not committed
    store.commit()
    assert store.count(Mineral) == 3


def test_count_rejects_non_entity_targets(store):
    with pytest.raises(TypeError, match="@entity class or a Condition"):
        store.count("Mineral")


# -- pluck ---------------------------------------------------------------------


def test_pluck_single_field_returns_values(store):
    _cabinet(store)
    assert sorted(store.pluck(Mineral, "name")) == ["azurite", "quartz", "topaz"]


def test_pluck_many_fields_returns_tuples(store):
    _cabinet(store)
    rows = store.pluck(M.mohs >= 7.0, "name", "mohs")
    assert sorted(rows) == [("quartz", 7.0), ("topaz", 8.0)]


def test_pluck_constructs_no_entities(store):
    _cabinet(store)
    gc.collect()
    live_before = len(store._registry)
    store.pluck(Mineral, "name", "tags")
    gc.collect()
    assert len(store._registry) == live_before


def test_pluck_refs_come_back_as_ref_tokens_for_get_many(store):
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.store(Mineral(qid="Q193563", name="azurite",
                        type_locality=dc.Lazy.of(tsumeb)))
    store.commit()
    (ref,) = store.pluck(M.qid == "Q193563", "type_locality")
    assert isinstance(ref, dc.Ref)
    (resolved,) = store.get_many([ref])
    assert resolved is tsumeb


def test_pluck_validates_field_names(store):
    _cabinet(store)
    with pytest.raises(dc.QueryError, match="not a persisted field"):
        store.pluck(Mineral, "firmenname")
    with pytest.raises(TypeError, match="at least one field"):
        store.pluck(Mineral)


def test_pluck_containers_come_back_plain(store):
    _cabinet(store)
    (tags,) = store.pluck(M.qid == "Q43010", "tags")
    assert tags == ["common"]
    assert type(tags) is list


def test_count_and_pluck_match_entity_valued_predicates(store):
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.store(Mineral(qid="Q193563", name="azurite",
                        type_locality=dc.Lazy.of(tsumeb)))
    store.store(Mineral(qid="Q43010", name="quartz"))
    store.commit()
    cond = M.type_locality == dc.Lazy.of(tsumeb)
    assert store.count(cond) == 1
    assert store.pluck(cond, "name") == ["azurite"]


# -- bulk unique-key get_many ----------------------------------------------------


def test_get_many_by_unique_key_aligns_with_input(store):
    _cabinet(store)
    quartz, azurite = store.get(Mineral, qid="Q43010"), store.get(Mineral, qid="Q193563")
    got = store.get_many(Mineral, qid=["Q193563", "no-such", "Q43010"])
    assert got == [azurite, None, quartz]


def test_get_many_by_key_validates_like_get(store):
    _cabinet(store)
    with pytest.raises(dc.QueryError, match="not a Unique field"):
        store.get_many(Mineral, name=["quartz"])
    with pytest.raises(TypeError, match="@entity class"):
        store.get_many([1, 2], qid=["x"])


def test_get_many_accepts_snapshot_refs(store):
    _cabinet(store)
    quartz = store.get(Mineral, qid="Q43010")
    oid = oid_of(quartz)
    assert oid is not None
    got = store.get_many([dc.Ref(oid)])
    assert got == [quartz]


# -- the loud empty ---------------------------------------------------------------


def test_unseen_type_warns_on_query_count_pluck(store):
    with pytest.warns(dc.UnseenTypeWarning, match="no committed records"):
        assert store.query(M.crystal_system == "trigonal") == []
    with pytest.warns(dc.UnseenTypeWarning):
        assert store.count(Mineral) == 0
    with pytest.warns(dc.UnseenTypeWarning):
        assert store.pluck(Mineral, "name") == []


def test_get_stays_silent_on_unseen_types(store):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert store.get(Mineral, qid="Q43010") is None  # the get-or-create idiom
        assert store.get_many(Mineral, qid=["Q43010"]) == [None]


def test_seen_types_do_not_warn(store):
    _cabinet(store)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert store.count(Mineral) == 3
        assert len(store.query(M.crystal_system == "trigonal")) == 1
