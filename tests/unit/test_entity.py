"""@entity mechanics: slots, identity, markers, frozen mode, field exprs."""

from __future__ import annotations

import weakref
from dataclasses import field
from datetime import datetime
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
        m.nonexistent_field = 1  # pyright: ignore[reportAttributeAccessIssue] — slots reject unknown attributes


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


def test_fulltext_marker_bare_and_parameterized():
    """dc.FullText stays inert in core, but both forms — bare and
    FullText(language="de") — must round-trip through the FieldSpec: the
    parameterized form is frozen core API before datacrystal[fts] ships
    (decided 2026-06-12; the extra reads field + language from the model)."""
    @dc.entity
    class FieldNote:
        label: str
        befund: Annotated[str, dc.FullText(language="de")] = ""
        summary: Annotated[str, dc.FullText] = ""

    by_name = {s.name: s for s in type_info(FieldNote).specs}
    assert by_name["befund"].fulltext
    assert by_name["befund"].fulltext_language == "de"
    assert by_name["summary"].fulltext
    assert by_name["summary"].fulltext_language is None
    assert not by_name["label"].fulltext
    assert repr(dc.FullText(language="de")) == "datacrystal.FullText(language='de')"

    with pytest.raises(TypeError, match="language code"):
        dc.FullText(language="")


def test_index_on_non_scalar_field_rejected():
    # #19: the "must be scalar" TypeError fires at decoration (the hints resolve
    # there — no forward ref), not lazily at first commit(). A bare `list` (no
    # element type) stays a reject after #13 added list[scalar] support.
    class Bad:
        refs: Annotated[list, dc.Index]

    with pytest.raises(TypeError, match="must be scalar"):
        dc.entity(Bad)


def test_list_of_scalars_index_accepted_and_multivalued():
    # #13: Annotated[list[str], Index] is a multi-valued (inverted) index.
    @dc.entity
    class Tagged:
        tags: Annotated[list[str], dc.Index] = field(default_factory=list)
        codes: Annotated[list[int] | None, dc.Index] = None

    by_name = {s.name: s for s in type_info(Tagged).specs}
    assert by_name["tags"].multivalued and by_name["tags"].indexed
    assert by_name["codes"].multivalued


def test_list_of_refs_index_rejected():
    # #13: only list[scalar] is indexable — a list of entity refs is not.
    class Bad:
        peers: Annotated[list[Mineral], dc.Index]

    with pytest.raises(TypeError, match="must be scalar"):
        dc.entity(Bad)


def test_unique_on_list_field_rejected():
    # #13: a multi-valued field has no single key, so Unique on a list is out.
    class Bad:
        tags: Annotated[list[str], dc.Unique]

    with pytest.raises(TypeError, match="cannot be a list"):
        dc.entity(Bad)


def test_double_decoration_rejected():
    with pytest.raises(TypeError, match="already an @entity"):
        dc.entity(Mineral)


def test_store_rejects_non_entities(store):
    with pytest.raises(dc.NotAnEntityError):
        store.store({"not": "an entity"})


# --- #19: eager Index/Unique validation is forward-ref-safe ------------------
# Up references Down, which is defined just below it, so at @entity decoration of
# Up the name Down is not yet bound: an eager get_type_hints() raises NameError
# under `from __future__ import annotations`. This module importing at all is the
# load-bearing regression guard — decoration-time validation must fall back to
# the lazy path on an unresolved ref (mirrors the demo's Lazy[T] graph).


@dc.entity
class Up:
    label: str
    down: dc.Lazy[Down] | None = None


@dc.entity
class Down:
    label: str
    up: dc.Lazy[Up] | None = None


def test_forward_ref_entities_decorate_then_resolve():
    # Up/Down decorated at import despite the forward ref (eager validation fell
    # back to the lazy path); their specs resolve now that both names are bound.
    # AC3 (a deferred class still validates on the unchanged lazy path) is held
    # jointly by this + the reject tests: _resolve_specs is the same code on
    # both paths, so a bad type deferred by a forward ref still raises by commit.
    up_specs = {s.name: s for s in type_info(Up).specs}
    down_specs = {s.name: s for s in type_info(Down).specs}
    assert up_specs["down"].lazy_refs
    assert down_specs["up"].lazy_refs


def test_index_on_datetime_rejected_at_decoration():
    # The original report (timeseries probe): Annotated[datetime, Index] was
    # accepted at @entity and only failed at first commit(). Now it fails at the
    # definition site.
    class Reading:
        ts: Annotated[datetime, dc.Index]

    with pytest.raises(TypeError, match="must be scalar"):
        dc.entity(Reading)
