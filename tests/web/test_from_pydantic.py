"""datacrystal[web] — ``from_pydantic`` reconstructs a live entity from a DTO.

Sprint 9 REST boundary (#98 / #49 spike S4, build plan #23): ``from_pydantic``
closes the round-trip opened by ``to_pydantic`` (#97). A boundary DTO — a parsed
request body or any :func:`entity_model` face — is rebuilt through the **public**
``cls(**field_values)`` constructor, so the result is a ``STATE_NEW`` instance
that earns its OID + dirty-tracking through the engine on the next
``store.root``/``upsert`` + ``commit``. These tests assert: the constructor path
(never the ``__dc_*`` slots), the OID-stays-OID default, the ``store=`` resolution
on the owner thread (ADR-001), a ``frozen=True`` entity reconstructing, and the
``create``/``public`` face split. The engine path parametrizes over both backends.

The web extra ships ``pydantic``; importorskip so the bare suite stays green
without it (mirrors ``tests/extras/`` for the fts/arrow extras).
"""

# A ``create_model`` DTO is a dynamically built type, so pyright cannot see its
# reflected fields (``dto.qid``) — the same untypeable situation the magic-query
# tests carry a pragma for.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import threading
from dataclasses import field
from typing import Annotated

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")

import datacrystal as dc
from datacrystal._entity import STATE_NEW, oid_of, state_of
from datacrystal.web import entity_model, from_pydantic, to_pydantic


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


# --- the headline reconstruction (STATE_NEW via the public constructor) --------


def test_from_pydantic_yields_state_new_entity() -> None:
    # A scalar-only DTO reconstructs to a fresh STATE_NEW instance — not stamped,
    # not registered, just a normally-constructed @entity awaiting store.root.
    create = entity_model(Mineral, face="create")
    dto = create.model_validate({"qid": "Q-NEW", "name": "fresh", "mohs": 5.5})
    mineral = from_pydantic(dto, Mineral)
    assert isinstance(mineral, Mineral)
    assert state_of(mineral) == STATE_NEW
    assert oid_of(mineral) is None  # the engine assigns identity, not the DTO
    assert mineral.qid == "Q-NEW"
    assert mineral.name == "fresh"
    assert mineral.mohs == 5.5


def test_reconstructed_entity_commits_and_round_trips(store) -> None:
    # The reconstructed STATE_NEW entity is a first-class engine object: rooting +
    # committing stamps it, and a fresh get() reads it back identically.
    dto = entity_model(Mineral, face="create").model_validate(
        {"qid": "Q-RT", "name": "roundtrip", "tags": ["a", "b"]}
    )
    mineral = from_pydantic(dto, Mineral)
    store.root = mineral
    store.commit()
    assert oid_of(mineral) is not None  # earned its OID through the engine
    fetched = store.get(Mineral, qid="Q-RT")
    assert fetched is not None
    assert fetched.name == "roundtrip"
    assert list(fetched.tags) == ["a", "b"]


def test_missing_defaulted_field_falls_to_dataclass_default() -> None:
    # A create DTO that omits a defaulted field reconstructs with the dataclass
    # default, exactly like a hand-written Mineral(qid=..., name=...).
    dto = entity_model(Mineral, face="create").model_validate({"qid": "Q-D", "name": "d"})
    mineral = from_pydantic(dto, Mineral)
    assert mineral.crystal_system is None
    assert mineral.mohs is None
    assert list(mineral.tags) == []  # default_factory=list


def test_never_pokes_engine_slots_on_a_fresh_entity() -> None:
    # from_pydantic must never stamp the __dc_* slots: a fresh entity carries only
    # the STATE_NEW state __new__ set, no oid and no store weakref.
    dto = entity_model(Mineral).model_validate({"qid": "Q-S", "name": "slots"})
    mineral = from_pydantic(dto, Mineral)
    assert not hasattr(mineral, "__dc_oid__")
    assert not hasattr(mineral, "__dc_store__")
    assert state_of(mineral) == STATE_NEW


# --- the to_pydantic <-> from_pydantic round-trip ------------------------------


def test_round_trips_through_to_pydantic(store) -> None:
    # to_pydantic projects a committed entity to a DTO; from_pydantic rebuilds an
    # equivalent STATE_NEW entity. Scalars survive verbatim; refs stay OIDs.
    mineral, loc = _stock(store)
    dto = to_pydantic(mineral)
    rebuilt = from_pydantic(dto, Mineral)
    assert state_of(rebuilt) == STATE_NEW
    assert rebuilt.qid == "Q-QTZ"
    assert rebuilt.mohs == 7.0
    assert rebuilt.discovered_in == oid_of(loc)  # OID stays an OID (no store)


# --- reference fields: OID by default, resolved with a store -------------------


def test_ref_fields_stay_oids_without_store(store) -> None:
    # No store passed: a ref field's OID stays the raw OID — the honest no-hydrate
    # default that never silently touches storage.
    _, loc = _stock(store)
    dto = entity_model(Mineral).model_validate(
        {"qid": "Q-REF", "name": "ref", "discovered_in": oid_of(loc),
         "type_locality": oid_of(loc)}
    )
    rebuilt = from_pydantic(dto, Mineral)
    assert rebuilt.discovered_in == oid_of(loc)
    assert rebuilt.type_locality == oid_of(loc)


def test_ref_fields_resolve_with_store(store) -> None:
    # With store=: each ref OID resolves to its live entity on the owner thread —
    # a directly-typed @entity field gets the entity, a Lazy field a Lazy.of(it).
    _, loc = _stock(store)
    dto = entity_model(Mineral).model_validate(
        {"qid": "Q-RES", "name": "res", "discovered_in": oid_of(loc),
         "type_locality": oid_of(loc)}
    )
    rebuilt = from_pydantic(dto, Mineral, store=store)
    assert rebuilt.discovered_in is loc  # the live entity, by identity
    assert isinstance(rebuilt.type_locality, dc.Lazy)
    assert rebuilt.type_locality.peek() is loc  # resident, no forced load
    # And it commits cleanly through the engine.
    store.root = [rebuilt]
    store.commit()
    assert oid_of(rebuilt) is not None


def test_none_ref_stays_none_with_store(store) -> None:
    # A None ref is not an OID, so it is left as None even when a store is passed.
    _stock(store)
    dto = entity_model(Mineral).model_validate({"qid": "Q-N", "name": "n"})
    rebuilt = from_pydantic(dto, Mineral, store=store)
    assert rebuilt.discovered_in is None
    assert rebuilt.type_locality is None


def test_store_resolution_is_owner_confined(store) -> None:
    # store= resolution goes through the public store.get_many, which guards the
    # owner thread (ADR-001) — a foreign thread raises WrongThreadError, no new
    # check needed. (Without a store the same call is thread-free.)
    _, loc = _stock(store)
    dto = entity_model(Mineral).model_validate(
        {"qid": "Q-T", "name": "t", "discovered_in": oid_of(loc)}
    )
    captured: list[type[BaseException]] = []

    def worker() -> None:
        try:
            from_pydantic(dto, Mineral, store=store)
        except BaseException as exc:  # noqa: BLE001 — record the type
            captured.append(type(exc))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert captured == [dc.WrongThreadError]


# --- frozen entity reconstructs ------------------------------------------------


def test_frozen_entity_reconstructs(store) -> None:
    # A frozen=True entity reconstructs the same way: the DTO's frozen config
    # constrains the DTO, not the constructor, so cls(**values) still runs.
    dto = entity_model(LogEntry).model_validate({"note": "found in drawer 3", "kind": "find"})
    entry = from_pydantic(dto, LogEntry)
    assert isinstance(entry, LogEntry)
    assert state_of(entry) == STATE_NEW
    assert entry.note == "found in drawer 3"
    assert entry.kind == "find"
    # It is a real frozen entity: it commits and reads back (kind is indexed, not
    # unique, so read back via the OID it earned).
    store.root = entry
    store.commit()
    fetched = store.get_many([oid_of(entry)])[0]
    assert fetched.note == "found in drawer 3"


# --- create / public face split ------------------------------------------------


def test_create_face_omits_oid_public_face_carries_it(store) -> None:
    mineral, _ = _stock(store)
    create = entity_model(Mineral, face="create")
    public = entity_model(Mineral, face="public")
    assert "oid" not in create.model_fields
    assert "oid" in public.model_fields
    assert create.__name__ == "MineralCreate"
    assert public.__name__ == "MineralPublic"
    # The public face is a distinct model from the plain/create ones.
    assert public is not create
    assert public is not entity_model(Mineral)


def test_public_face_carries_oid_from_a_view(store) -> None:
    # to_pydantic(face="public") projects a committed read into the output DTO,
    # carrying the engine-assigned oid — the response_model= shape.
    mineral, _ = _stock(store)
    with store.snapshot() as snap:
        view = snap.get(oid_of(mineral))
        dto = to_pydantic(view, face="public")
    assert isinstance(dto, entity_model(Mineral, face="public"))
    assert dto.oid == oid_of(mineral)
    assert dto.qid == "Q-QTZ"


def test_public_face_from_a_live_entity(store) -> None:
    mineral, _ = _stock(store)
    dto = to_pydantic(mineral, face="public")
    assert dto.oid == oid_of(mineral)
    assert dto.name == "quartz"


def test_public_face_requires_a_committed_source() -> None:
    # A never-committed entity has no OID, so the output face cannot be built.
    fresh = Mineral(qid="X", name="x")
    with pytest.raises(ValueError, match="OID-bearing"):
        to_pydantic(fresh, face="public")


def test_from_attributes_reads_scalars_off_a_view(store) -> None:
    # The headline from_attributes AC: a generated model reads fields by name off
    # a live read. A scalar-only LogEntry validates straight from its view.
    entry = LogEntry(note="raw read")
    store.root = entry
    store.commit()
    with store.snapshot() as snap:
        view = snap.get(oid_of(entry))
        dto = entity_model(LogEntry).model_validate(view)  # from_attributes=True
    assert dto.note == "raw read"
    assert dto.kind == "misc"


# --- loud rejection ------------------------------------------------------------


def test_rejects_non_entity_class() -> None:
    dto = entity_model(Mineral).model_validate({"qid": "Q", "name": "q"})
    with pytest.raises(dc.NotAnEntityError):
        from_pydantic(dto, dict)  # not an @entity class
