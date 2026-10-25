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

from __future__ import annotations

import datetime as dt
import functools
import threading
from collections.abc import Callable, Iterator
from dataclasses import field
from itertools import pairwise
from typing import Annotated

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")

import pydantic

import datacrystal as dc
from datacrystal._entity import oid_of
from datacrystal._snapshot import EntityView
from datacrystal.web import entity_model, to_pydantic


@dc.entity
class Locality:
    """A place a mineral was found."""

    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Vein:
    """A mineral vein that branches into a deeper vein, a chain of resident referents."""

    qid: Annotated[str, dc.Unique]
    deeper: Vein | None = None


@dc.entity
class Mineral:
    """A mineral species with scalar, reference and list fields."""

    qid: Annotated[str, dc.Unique]
    name: Annotated[str, dc.FullText]
    crystal_system: Annotated[str | None, dc.Index] = None
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None
    discovered_in: Locality | None = None
    tags: list[str] = field(default_factory=list)


@dc.entity
class Event:
    """An acquisition event with temporal and dict fields."""

    label: str
    when: dt.datetime | None = None
    on_day: dt.date | None = None
    facets: dict = field(default_factory=dict)


@dc.entity(frozen=True)
class LogEntry:
    """A frozen cabinet log line."""

    note: str
    kind: Annotated[str, dc.Index] = "misc"


def _record_error_type(captured: list[type[BaseException]], call: Callable[[], object]) -> None:
    """Thread target: run ``call`` and record the type of the exception it raises."""
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 — record the type
        captured.append(type(exc))


def _dump_view_in_thread(out: list[dict[str, object]], view: EntityView) -> None:
    """Thread target: project ``view`` to a DTO and keep its dump."""
    out.append(to_pydantic(view).model_dump())


def _oid(entity: object) -> int:
    """Return the OID of a committed entity."""
    oid = oid_of(entity)
    if oid is None:
        raise ValueError(f"{entity!r} has no OID, it was never committed")
    return oid


def _stock_in(store: dc.Store) -> tuple[Mineral, Locality]:
    """Commit a quartz-from-Brazil pair into ``store`` and return the live (mineral, locality)."""
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


@pytest.fixture
def stock(store: dc.Store) -> tuple[Mineral, Locality]:
    """Commit a quartz-from-Brazil pair and return the live (mineral, locality)."""
    return _stock_in(store)


@pytest.fixture
def reopened_stock(store_factory: Callable[[], dc.Store]) -> Iterator[tuple[dc.Store, int]]:
    """Provide a store reopened after the stock was committed, with Brazil's OID."""
    first = store_factory()
    _, brazil = _stock_in(first)
    brazil_oid = _oid(brazil)
    first.close()
    reopened = store_factory()
    yield reopened, brazil_oid
    reopened.close()


# --- live-entity input (both backends) ---------------------------------------


def test_live_entity_to_validated_dto(stock: tuple[Mineral, Locality]) -> None:
    """Given a committed live quartz
    When to_pydantic projects it
    Then the DTO is an instance of the Mineral model
    And it is a pydantic BaseModel
    And it carries the qid, name, crystal system and hardness
    """
    mineral, _ = stock

    dto = to_pydantic(mineral)

    assert isinstance(dto, entity_model(Mineral))
    assert isinstance(dto, pydantic.BaseModel)
    assert dto.qid == "Q-QTZ"  # pyrefly: ignore[missing-attribute]
    assert dto.name == "quartz"  # pyrefly: ignore[missing-attribute]
    assert dto.crystal_system == "trigonal"  # pyrefly: ignore[missing-attribute]
    assert dto.mohs == 7.0  # pyrefly: ignore[missing-attribute]


def test_refs_cross_as_oid(stock: tuple[Mineral, Locality]) -> None:
    """Given a committed quartz with a Lazy and a directly-typed reference to Brazil
    When to_pydantic projects it
    Then the Lazy reference is Brazil's OID
    And the directly-typed reference is Brazil's OID
    And the Lazy reference is an int
    """
    mineral, loc = stock

    dto = to_pydantic(mineral)

    assert dto.type_locality == oid_of(loc)  # pyrefly: ignore[missing-attribute]
    assert dto.discovered_in == oid_of(loc)  # pyrefly: ignore[missing-attribute]
    assert isinstance(dto.type_locality, int)


def test_containers_decay_to_plain_list(stock: tuple[Mineral, Locality]) -> None:
    """Given a committed quartz whose tags are a persistent list
    When to_pydantic projects it
    Then the DTO's tags equal the two tags
    And they are a plain list, not the owner-bound container
    """
    mineral, _ = stock

    dto = to_pydantic(mineral)

    assert dto.tags == ["display", "drawer-3"]  # pyrefly: ignore[missing-attribute]
    assert type(dto.tags) is list


def test_dict_field_and_temporals_pass_native(store: dc.Store) -> None:
    """Given a committed event with a datetime, a date and a dict
    When to_pydantic projects it
    Then the DTO's datetime is unchanged
    And its date is unchanged
    And its facets equal the dict
    And its facets are a plain dict
    """
    when = dt.datetime(2026, 6, 15, 9, 30)
    day = dt.date(2026, 6, 15)
    ev = Event(label="acquired", when=when, on_day=day, facets={"by": "S.H.", "lot": "7"})
    store.root = ev
    store.commit()

    dto = to_pydantic(ev)

    assert dto.when == when  # pyrefly: ignore[missing-attribute]
    assert dto.on_day == day  # pyrefly: ignore[missing-attribute]
    assert dto.facets == {"by": "S.H.", "lot": "7"}  # pyrefly: ignore[missing-attribute]
    assert type(dto.facets) is dict


@pytest.mark.parametrize("engine_attribute", ["__dc_oid__", "__dc_store__", "__dc_state__"])
def test_dto_holds_no_live_reference(
    stock: tuple[Mineral, Locality], engine_attribute: str
) -> None:
    """Given a committed live quartz
    When to_pydantic projects it
    Then the DTO carries no engine slot, no stamp, no store, no state
    """
    mineral, _ = stock

    dto = to_pydantic(mineral)

    assert not hasattr(dto, engine_attribute)


def test_frozen_entity_projects_its_fields(store: dc.Store) -> None:
    """Given a committed frozen LogEntry
    When to_pydantic projects it
    Then the DTO carries the note
    """
    entry = LogEntry(note="found in drawer 3")
    store.root = entry
    store.commit()

    dto = to_pydantic(entry)

    assert dto.note == "found in drawer 3"  # pyrefly: ignore[missing-attribute]


def test_frozen_entity_projects_to_frozen_dto(store: dc.Store) -> None:
    """Given the DTO of a committed frozen LogEntry
    When its note is assigned
    Then pydantic raises ValidationError
    """
    entry = LogEntry(note="found in drawer 3")
    store.root = entry
    store.commit()
    dto = to_pydantic(entry)

    with pytest.raises(pydantic.ValidationError):
        dto.note = "edited"  # pyrefly: ignore[missing-attribute]


# --- snapshot-view input (the preferred, store-free path) --------------------


def test_entity_view_to_dto(store: dc.Store, stock: tuple[Mineral, Locality]) -> None:
    """Given a committed quartz read as a snapshot view
    When to_pydantic projects the view
    Then the DTO is an instance of the Mineral model
    And it carries the qid
    """
    mineral, _ = stock

    with store.snapshot() as snap:
        dto = to_pydantic(snap.get(_oid(mineral)))

    assert isinstance(dto, entity_model(Mineral))
    assert dto.qid == "Q-QTZ"  # pyrefly: ignore[missing-attribute]


def test_view_refs_decay_to_oids(store: dc.Store, stock: tuple[Mineral, Locality]) -> None:
    """Given a committed quartz read as a snapshot view
    When to_pydantic projects the view
    Then the discovered_in Ref is Brazil's OID
    And the type_locality Ref is Brazil's OID
    """
    mineral, loc = stock

    with store.snapshot() as snap:
        dto = to_pydantic(snap.get(_oid(mineral)))

    assert dto.discovered_in == oid_of(loc)  # pyrefly: ignore[missing-attribute]
    assert dto.type_locality == oid_of(loc)  # pyrefly: ignore[missing-attribute]


def test_view_frozen_tuple_decays_to_plain_list(
    store: dc.Store, stock: tuple[Mineral, Locality]
) -> None:
    """Given a committed quartz read as a snapshot view whose tags are a frozen tuple
    When to_pydantic projects the view
    Then the DTO's tags equal the two tags
    And they are a plain list
    """
    mineral, _ = stock

    with store.snapshot() as snap:
        dto = to_pydantic(snap.get(_oid(mineral)))

    assert dto.tags == ["display", "drawer-3"]  # pyrefly: ignore[missing-attribute]
    assert type(dto.tags) is list


def test_view_dto_matches_live_dto(store: dc.Store, stock: tuple[Mineral, Locality]) -> None:
    """Given a committed quartz projected from a snapshot view and from the live entity
    When both DTOs are dumped
    Then the dumps are equal
    """
    mineral, _ = stock
    with store.snapshot() as snap:
        view_dto = to_pydantic(snap.get(_oid(mineral)))

    live_dto = to_pydantic(mineral)

    assert view_dto.model_dump() == live_dto.model_dump()


def test_view_dto_survives_snapshot_close(store: dc.Store, stock: tuple[Mineral, Locality]) -> None:
    """Given a DTO projected from a snapshot view whose snapshot has since closed
    When the DTO is read
    Then it still carries the qid
    And it still carries the tags
    """
    mineral, _ = stock
    with store.snapshot() as snap:
        dto = to_pydantic(snap.get(_oid(mineral)))

    assert dto.qid == "Q-QTZ"  # pyrefly: ignore[missing-attribute]
    assert dto.tags == ["display", "drawer-3"]  # pyrefly: ignore[missing-attribute]


# --- reference / load discipline ---------------------------------------------


def test_unloaded_lazy_projects_oid_without_loading(reopened_stock: tuple[dc.Store, int]) -> None:
    """Given a quartz fetched from a reopened store whose Lazy referent was dropped
    When to_pydantic projects it
    Then type_locality is Brazil's OID
    And the Lazy handle is still unloaded
    """
    reopened, target_oid = reopened_stock
    fetched = reopened.get(Mineral, qid="Q-QTZ")
    assert fetched is not None
    handle = fetched.type_locality
    assert handle is not None
    handle._obj = None
    assert not handle.loaded

    dto = to_pydantic(fetched)

    assert dto.type_locality == target_oid  # pyrefly: ignore[missing-attribute]
    assert not handle.loaded


def test_unstored_entity_projects_its_scalars() -> None:
    """Given a never-stored mineral with no referents
    When to_pydantic projects it
    Then the DTO carries the qid
    And discovered_in is None
    """
    fresh = Mineral(qid="NEW", name="fresh")

    dto = to_pydantic(fresh)

    assert dto.qid == "NEW"  # pyrefly: ignore[missing-attribute]
    assert dto.discovered_in is None  # pyrefly: ignore[missing-attribute]


def test_unstored_referent_raises_loudly() -> None:
    """Given a never-stored mineral referencing a never-stored locality
    When to_pydantic projects it
    Then it raises ValueError naming that the referent was never stored
    """
    dangling = Mineral(qid="DANGLE", name="x", discovered_in=Locality(qid="?", name="?"))

    with pytest.raises(ValueError, match="never stored"):
        to_pydantic(dangling)


def test_rejects_a_plain_object() -> None:
    """Given a plain object that is neither a live entity nor a view
    When to_pydantic projects it
    Then it raises TypeError naming a live @entity or an EntityView
    """
    with pytest.raises(TypeError, match="live @entity or an EntityView"):
        to_pydantic(object())


def test_rejects_a_dict() -> None:
    """Given a dict that is neither a live entity nor a view
    When to_pydantic projects it
    Then it raises TypeError
    """
    with pytest.raises(TypeError):
        to_pydantic({"qid": "X"})


# --- owner confinement (ADR-001) ---------------------------------------------


def test_foreign_thread_raises_before_read(stock: tuple[Mineral, Locality]) -> None:
    """Given a live committed quartz owned by this thread
    When a foreign thread projects it with to_pydantic
    Then the call raises WrongThreadError
    """
    mineral, _ = stock
    captured: list[type[BaseException]] = []
    worker = threading.Thread(
        target=_record_error_type, args=(captured, functools.partial(to_pydantic, mineral))
    )

    worker.start()
    worker.join(timeout=10)

    assert captured == [dc.WrongThreadError]


def test_view_is_thread_safe(store: dc.Store, stock: tuple[Mineral, Locality]) -> None:
    """Given a snapshot view of a committed quartz
    When a foreign thread projects it with to_pydantic
    Then the thread produced one DTO dump
    And the dump carries the qid
    """
    mineral, _ = stock
    out: list[dict[str, object]] = []

    with store.snapshot() as snap:
        worker = threading.Thread(
            target=_dump_view_in_thread, args=(out, snap.get(_oid(mineral)))
        )
        worker.start()
        worker.join(timeout=10)

    assert [dump["qid"] for dump in out] == ["Q-QTZ"]


# --- bounded nested recursion (opt-in) ---------------------------------------


def test_default_never_hydrates_resident_referent(stock: tuple[Mineral, Locality]) -> None:
    """Given a committed quartz whose discovered_in holds the live resident locality
    When to_pydantic projects it with the default nested=0
    Then discovered_in is Brazil's OID
    And it is not a nested DTO
    """
    mineral, loc = stock

    dto = to_pydantic(mineral)

    assert dto.discovered_in == oid_of(loc)  # pyrefly: ignore[missing-attribute]
    assert not isinstance(dto.discovered_in, pydantic.BaseModel)


def test_nested_recurses_directly_held_resident_referent(stock: tuple[Mineral, Locality]) -> None:
    """Given a committed quartz holding the resident locality directly
    When to_pydantic projects it with nested=1
    Then discovered_in is a nested Locality DTO
    And its name is Brazil
    """
    mineral, _ = stock

    dto = to_pydantic(mineral, nested=1)

    assert isinstance(dto.discovered_in, entity_model(Locality))  # pyrefly: ignore[missing-attribute]
    assert dto.discovered_in.name == "Brazil"  # pyrefly: ignore[missing-attribute]


def test_nested_recurses_loaded_lazy_referent(stock: tuple[Mineral, Locality]) -> None:
    """Given a committed quartz whose Lazy referent is loaded
    When to_pydantic projects it with nested=1
    Then type_locality is a nested Locality DTO
    And its qid is Q-BR
    """
    mineral, _ = stock

    dto = to_pydantic(mineral, nested=1)

    assert isinstance(dto.type_locality, entity_model(Locality))  # pyrefly: ignore[missing-attribute]
    assert dto.type_locality.qid == "Q-BR"  # pyrefly: ignore[missing-attribute]


def test_nested_depth_one_terminates_at_a_leaf_referent(store: dc.Store) -> None:
    """Given an outer mineral whose referent locality has no further entity references
    When to_pydantic projects it with nested=1
    Then discovered_in is a nested Locality DTO
    And its name is Deep
    """
    loc = Locality(qid="DEEP-L", name="Deep")
    inner = Mineral(qid="INNER", name="inner", discovered_in=loc)
    outer = Mineral(qid="OUTER", name="outer", discovered_in=loc, type_locality=dc.Lazy.of(loc))
    store.root = [outer, inner, loc]
    store.commit()

    dto = to_pydantic(outer, nested=1)

    assert isinstance(dto.discovered_in, entity_model(Locality))  # pyrefly: ignore[missing-attribute]
    assert dto.discovered_in.name == "Deep"  # pyrefly: ignore[missing-attribute]


def test_nested_unloaded_lazy_stays_oid(reopened_stock: tuple[dc.Store, int]) -> None:
    """Given a quartz fetched from a reopened store whose Lazy referent was dropped
    When to_pydantic projects it with nested=2
    Then type_locality stays an int OID
    And the Lazy handle is still unloaded
    """
    reopened, _ = reopened_stock
    fetched = reopened.get(Mineral, qid="Q-QTZ")
    assert fetched is not None
    handle = fetched.type_locality
    assert handle is not None
    handle._obj = None
    assert not handle.loaded

    dto = to_pydantic(fetched, nested=2)

    assert isinstance(dto.type_locality, int)  # pyrefly: ignore[missing-attribute]
    assert not handle.loaded


def test_nested_over_view_stays_oid(store: dc.Store, stock: tuple[Mineral, Locality]) -> None:
    """Given a committed quartz read as a snapshot view
    When to_pydantic projects the view with nested=3
    Then discovered_in is Brazil's OID
    And type_locality is Brazil's OID
    """
    mineral, loc = stock

    with store.snapshot() as snap:
        dto = to_pydantic(snap.get(_oid(mineral)), nested=3)

    assert dto.discovered_in == oid_of(loc)  # pyrefly: ignore[missing-attribute]
    assert dto.type_locality == oid_of(loc)  # pyrefly: ignore[missing-attribute]


def _vein_chain(store: dc.Store, length: int) -> list[Vein]:
    """Commit a chain of veins, each branching into the next; return them from the top."""
    veins = [Vein(qid=f"V{i}") for i in range(length)]
    for upper, lower in pairwise(veins):
        upper.deeper = lower
    store.root = veins
    store.commit()
    return veins


def test_nested_depth_bound_projects_exactly_the_requested_levels(store: dc.Store) -> None:
    """Given a committed chain of four veins, each resident in the one above
    When to_pydantic projects the top vein with nested=2
    Then the third vein is still a nested Vein DTO
    """
    veins = _vein_chain(store, 4)

    dto = to_pydantic(veins[0], nested=2)

    assert isinstance(dto.deeper.deeper, entity_model(Vein))  # pyrefly: ignore[missing-attribute]


def test_nested_depth_bound_stops_at_an_oid_past_the_requested_levels(store: dc.Store) -> None:
    """Given a committed chain of four veins, each resident in the one above
    When to_pydantic projects the top vein with nested=2
    Then the fourth vein crosses as its OID
    """
    veins = _vein_chain(store, 4)

    dto = to_pydantic(veins[0], nested=2)

    assert dto.deeper.deeper.deeper == oid_of(veins[3])  # pyrefly: ignore[missing-attribute]


def test_nested_depth_bound_ends_a_reference_cycle(store: dc.Store) -> None:
    """Given two committed veins that branch into each other
    When to_pydantic projects the first with nested=3
    Then the projection ends with the fourth level as the first vein's OID
    """
    first, second = Vein(qid="LOOP-A"), Vein(qid="LOOP-B")
    first.deeper, second.deeper = second, first
    store.root = [first, second]
    store.commit()

    dto = to_pydantic(first, nested=3)

    assert dto.deeper.deeper.deeper.deeper == oid_of(first)  # pyrefly: ignore[missing-attribute]
