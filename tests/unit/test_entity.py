"""@entity mechanics: slots, identity, markers, frozen mode, field exprs."""

from __future__ import annotations

import weakref
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._conditions import FieldExpr, Pred
from datacrystal._entity import type_info
from tests.conftest import LogEntry, Mineral


def test_entities_are_slots_classes():
    m = Mineral(qid="Q1", name="quartz")
    with pytest.raises(AttributeError):
        _ = m.__dict__
    with pytest.raises(AttributeError):
        m.nonexistent_field = 1  # slots reject unknown attributes


def test_entities_support_weakrefs():
    m = Mineral(qid="Q1", name="quartz")
    assert weakref.ref(m)() is m


def test_equality_is_identity():
    a = Mineral(qid="Q1", name="quartz")
    b = Mineral(qid="Q1", name="quartz")
    assert a != b and a == a
    assert len({id(a), id(b)}) == 2


def test_frozen_entity_rejects_mutation():
    entry = LogEntry(note="acquired")
    assert entry.note == "acquired"
    with pytest.raises(dc.FrozenEntityError):
        entry.note = "edited"


def test_class_access_yields_field_exprs_instance_access_values():
    expr = Mineral.name
    assert isinstance(expr, FieldExpr)
    pred = Mineral.name == "quartz"
    assert isinstance(pred, Pred)
    m = Mineral(qid="Q1", name="quartz")
    assert m.name == "quartz"  # instance access is plain slot access


def test_marker_harvest():
    ti = type_info(Mineral)
    by_name = {s.name: s for s in ti.specs}
    assert by_name["qid"].unique and not by_name["qid"].indexed
    assert by_name["crystal_system"].indexed
    assert by_name["type_locality"].lazy_refs
    assert not by_name["name"].indexed


def test_index_on_non_scalar_field_rejected():
    @dc.entity
    class Bad:
        refs: Annotated[list, dc.Index]

    with pytest.raises(TypeError, match="must be scalar"):
        _ = type_info(Bad).specs


def test_double_decoration_rejected():
    with pytest.raises(TypeError, match="already an @entity"):
        dc.entity(Mineral)


def test_store_rejects_non_entities(store):
    with pytest.raises(dc.NotAnEntityError):
        store.store({"not": "an entity"})
