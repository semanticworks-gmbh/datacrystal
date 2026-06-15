"""datacrystal[web] — ``to_pydantic`` projects a live entity / view into a DTO.

Sprint 9 REST boundary (#97 / #49 spike S3, build plan #23): ``to_pydantic``
turns a live entity (read on the owner thread) **or** an already-detached
``EntityView`` (a ``store.snapshot()`` read) into a validated, store-free Pydantic
DTO of its ``entity_model``. These tests exercise both inputs over the canonical
mineral cabinet and parametrize the engine path over both backends — refs cross as
OIDs (``peek()``-else-``.oid``, never a forced load), containers decay to plain
``list`` / ``dict``, the DTO leaks no live object, and bounded nested recursion is
opt-in.

The web extra ships ``pydantic``; importorskip so the bare suite stays green
without it (mirrors ``tests/extras/`` for the fts/arrow extras).
"""

# A ``create_model`` DTO is a dynamically built type, so pyright cannot see its
# reflected fields (``dto.qid``) — the same untypeable situation the magic-query
# tests carry a pragma for. File-scoped here because these tests read the DTO's
# reflected attributes throughout.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import field
from typing import Annotated

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")

import pydantic

import datacrystal as dc
from datacrystal._entity import oid_of
from datacrystal.web import entity_model, to_pydantic


@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: Annotated[str, dc.FullText]
    crystal_system: Annotated[str | None, dc.Index] = None
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None
    discovered_in: Locality | None = None
    tags: list[str] = field(default_factory=list)


@dc.entity
class Event:
    label: str
    when: dt.datetime | None = None
    on_day: dt.date | None = None
    facets: dict = field(default_factory=dict)


@dc.entity(frozen=True)
class LogEntry:
    note: str
    kind: Annotated[str, dc.Index] = "misc"


def _stock(store: dc.Store) -> tuple[Mineral, Locality]:
    """A quartz-from-Brazil pair, committed; returns the live (mineral, locality)."""
    loc = Locality(qid="Q-BR", name="Brazil")
    mineral = Mineral(
        qid="Q-QTZ",
        name="quartz",
        crystal_system="trigonal",
        mohs=7.0,
        type_locality=dc.Lazy.of(loc),
        discovered_in=loc,
        tags=["display", "drawer-3"],
    )
    store.root = [mineral, loc]
    store.commit()
    return mineral, loc


# --- live-entity input (both backends) ---------------------------------------


def test_live_entity_to_validated_dto(store) -> None:
    mineral, _ = _stock(store)
    dto = to_pydantic(mineral)
    assert isinstance(dto, entity_model(Mineral))
    assert isinstance(dto, pydantic.BaseModel)
    assert dto.qid == "Q-QTZ"
    assert dto.name == "quartz"
    assert dto.crystal_system == "trigonal"
    assert dto.mohs == 7.0


def test_refs_cross_as_oid(store) -> None:
    # Both a Lazy ref and a directly-typed @entity field project to the
    # referent's OID (int) — the request edge carries the id, not the object.
    mineral, loc = _stock(store)
    dto = to_pydantic(mineral)
    assert dto.type_locality == oid_of(loc)
    assert dto.discovered_in == oid_of(loc)
    assert isinstance(dto.type_locality, int)


def test_containers_decay_to_plain_list(store) -> None:
    # A PersistentList field decays to a plain list on the DTO (not the
    # owner-bound container) — the DTO carries inert transport data.
    mineral, _ = _stock(store)
    dto = to_pydantic(mineral)
    assert dto.tags == ["display", "drawer-3"]
    assert type(dto.tags) is list


def test_dict_field_and_temporals_pass_native(store) -> None:
    when = dt.datetime(2026, 6, 15, 9, 30)
    day = dt.date(2026, 6, 15)
    ev = Event(label="acquired", when=when, on_day=day,
               facets={"by": "S.H.", "lot": "7"})
    store.root = ev
    store.commit()
    dto = to_pydantic(ev)
    assert dto.when == when
    assert dto.on_day == day
    assert dto.facets == {"by": "S.H.", "lot": "7"}
    assert type(dto.facets) is dict


def test_dto_holds_no_live_reference(store) -> None:
    # The DTO is plain validated data: no engine stamp, no store, no registry —
    # owner-confinement-safe by construction (ADR-001, the EntityView rule).
    mineral, _ = _stock(store)
    dto = to_pydantic(mineral)
    assert not hasattr(dto, "__dc_oid__")
    assert not hasattr(dto, "__dc_store__")
    assert not hasattr(dto, "__dc_state__")


def test_frozen_entity_projects_to_frozen_dto(store) -> None:
    entry = LogEntry(note="found in drawer 3")
    store.root = entry
    store.commit()
    dto = to_pydantic(entry)
    assert dto.note == "found in drawer 3"
    with pytest.raises(pydantic.ValidationError):
        # dynamically built model — pyright can't see the reflected field.
        dto.note = "edited"  # pyright: ignore[reportAttributeAccessIssue]


# --- snapshot-view input (the preferred, store-free path) --------------------


def test_entity_view_to_dto(store) -> None:
    mineral, loc = _stock(store)
    with store.snapshot() as snap:
        view = snap.get(oid_of(mineral))
        dto = to_pydantic(view)
    assert isinstance(dto, entity_model(Mineral))
    assert dto.qid == "Q-QTZ"
    # A view's Ref decays to its OID; its frozen tuple to a plain list.
    assert dto.discovered_in == oid_of(loc)
    assert dto.type_locality == oid_of(loc)
    assert dto.tags == ["display", "drawer-3"]
    assert type(dto.tags) is list


def test_view_dto_matches_live_dto(store) -> None:
    # The preferred (view) path and the live path produce the same transport data.
    mineral, _ = _stock(store)
    with store.snapshot() as snap:
        view_dto = to_pydantic(snap.get(oid_of(mineral)))
    live_dto = to_pydantic(mineral)
    assert view_dto.model_dump() == live_dto.model_dump()


def test_view_dto_survives_snapshot_close(store) -> None:
    # The DTO is detached: it stays fully readable after the snapshot is closed
    # (it holds no reference back into the read view).
    mineral, _ = _stock(store)
    with store.snapshot() as snap:
        dto = to_pydantic(snap.get(oid_of(mineral)))
    assert dto.qid == "Q-QTZ"
    assert dto.tags == ["display", "drawer-3"]


# --- reference / load discipline ---------------------------------------------


def test_unloaded_lazy_projects_oid_without_loading(store_factory) -> None:
    # An unloaded Lazy crosses as its OID — to_pydantic NEVER forces a .get().
    store = store_factory()
    mineral, loc = _stock(store)
    target_oid = oid_of(loc)
    store.close()

    store2 = store_factory()
    fetched = store2.get(Mineral, qid="Q-QTZ")
    assert fetched is not None
    handle = fetched.type_locality
    assert handle is not None
    # Drop the resident referent so the handle is genuinely unloaded, then prove
    # projection reads .oid (peek() is None) rather than loading it.
    handle._obj = None  # pyright: ignore[reportPrivateUsage]
    assert not handle.loaded
    dto = to_pydantic(fetched)
    assert dto.type_locality == target_oid
    assert not handle.loaded  # still unloaded — no forced load
    store2.close()


def test_unstored_entity_projects(store) -> None:
    # A never-stored entity has no OID and no owner: its scalars project; a
    # never-stored referent (no OID) raises loudly (mirror the engine).
    fresh = Mineral(qid="NEW", name="fresh")
    dto = to_pydantic(fresh)
    assert dto.qid == "NEW"
    assert dto.discovered_in is None

    dangling = Mineral(qid="DANGLE", name="x", discovered_in=Locality(qid="?", name="?"))
    with pytest.raises(ValueError, match="never stored"):
        to_pydantic(dangling)


def test_rejects_non_entity_non_view() -> None:
    with pytest.raises(TypeError, match="live @entity or an EntityView"):
        to_pydantic(object())
    with pytest.raises(TypeError):
        to_pydantic({"qid": "X"})


# --- owner confinement (ADR-001) ---------------------------------------------


def test_foreign_thread_raises_before_read(store) -> None:
    # A live entity is owner-confined: to_pydantic re-asserts the ADR-001 guard
    # before reading any field, so a foreign thread raises WrongThreadError.
    mineral, _ = _stock(store)
    captured: list[type[BaseException]] = []

    def worker() -> None:
        try:
            to_pydantic(mineral)
        except BaseException as exc:  # noqa: BLE001 — record the type
            captured.append(type(exc))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert captured == [dc.WrongThreadError]


def test_view_is_thread_safe(store) -> None:
    # The view path is store-free, so it works from a foreign thread (the
    # preferred cross-thread recipe).
    mineral, _ = _stock(store)
    with store.snapshot() as snap:
        view = snap.get(oid_of(mineral))
        out: list[object] = []

        def worker() -> None:
            out.append(to_pydantic(view).model_dump())

        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert out and out[0]["qid"] == "Q-QTZ"  # type: ignore[index]


# --- bounded nested recursion (opt-in) ---------------------------------------


def test_default_never_hydrates_resident_referent(store) -> None:
    # nested=0 (default): a resident referent still crosses as its OID — the
    # no-auto-hydrate default that can never re-introduce N+1.
    mineral, loc = _stock(store)
    # discovered_in holds the live Locality directly (resident); still an OID.
    dto = to_pydantic(mineral)
    assert dto.discovered_in == oid_of(loc)
    assert not isinstance(dto.discovered_in, pydantic.BaseModel)


def test_nested_recurses_resident_referent(store) -> None:
    mineral, loc = _stock(store)
    dto = to_pydantic(mineral, nested=1)
    # The directly-held @entity field becomes a nested Locality DTO.
    assert isinstance(dto.discovered_in, entity_model(Locality))
    assert dto.discovered_in.name == "Brazil"
    # A loaded Lazy recurses too (peek() resolves the resident target).
    assert isinstance(dto.type_locality, entity_model(Locality))
    assert dto.type_locality.qid == "Q-BR"


def test_nested_is_bounded_by_depth(store_factory) -> None:
    # Recursion stops at the requested depth: at the boundary the deeper ref is
    # back to an OID, never an unbounded walk.
    store = store_factory()
    loc = Locality(qid="DEEP-L", name="Deep")
    inner = Mineral(qid="INNER", name="inner", discovered_in=loc)
    outer = Mineral(qid="OUTER", name="outer", discovered_in=loc,
                    type_locality=dc.Lazy.of(loc))
    # Chain outer -> inner via a Lazy so depth is observable.
    outer.tags = []
    store.root = [outer, inner, loc]
    store.commit()

    # depth 1 from outer: discovered_in (loc) becomes a DTO; loc has no further
    # @entity refs, so a depth-1 walk terminates cleanly.
    dto = to_pydantic(outer, nested=1)
    assert isinstance(dto.discovered_in, entity_model(Locality))
    assert dto.discovered_in.name == "Deep"
    store.close()


def test_nested_unloaded_lazy_stays_oid(store_factory) -> None:
    # Even with nested>0, an UNLOADED Lazy stays an OID — recursion only follows
    # referents already resident in RAM, never forcing a load.
    store = store_factory()
    _stock(store)
    store.close()
    store2 = store_factory()
    fetched = store2.get(Mineral, qid="Q-QTZ")
    assert fetched is not None and fetched.type_locality is not None
    fetched.type_locality._obj = None  # pyright: ignore[reportPrivateUsage]
    assert not fetched.type_locality.loaded
    dto = to_pydantic(fetched, nested=2)
    assert isinstance(dto.type_locality, int)  # unloaded → OID, not hydrated
    assert not fetched.type_locality.loaded
    store2.close()


def test_nested_over_view_stays_oid(store) -> None:
    # A snapshot view is store-free, so it has nothing resident to recurse into:
    # nested>0 still yields OIDs (honoring "the DTO holds no store reference").
    mineral, loc = _stock(store)
    with store.snapshot() as snap:
        dto = to_pydantic(snap.get(oid_of(mineral)), nested=3)
    assert dto.discovered_in == oid_of(loc)
    assert dto.type_locality == oid_of(loc)
