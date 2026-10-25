"""datacrystal[web] — ``from_pydantic`` reconstructs a live entity from a DTO.

Sprint 9 REST boundary (#98 / #49 spike S4, build plan #23): ``from_pydantic``
closes the round-trip opened by ``to_pydantic`` (#97). A boundary DTO — a parsed
request body or any :func:`entity_model` face — is rebuilt through the **public**
``cls(**field_values)`` constructor, so the result is a fresh instance
that earns its OID + dirty-tracking through the engine on the next
``store.root``/``upsert`` + ``commit``. These tests assert: the constructor path
(a fresh entity the store does not know until it is stored), the OID-stays-OID default,
the ``store=`` resolution on the owner thread (ADR-001), a ``frozen=True`` entity
reconstructing, and the ``create``/``public`` face split.
The engine path parametrizes over both backends.

The web extra ships ``pydantic``; importorskip so the bare suite stays green
without it (mirrors ``tests/extras/`` for the fts/arrow extras).
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from dataclasses import field
from typing import Annotated, NamedTuple

import pytest

pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")

import datacrystal as dc
from datacrystal.web import entity_model, from_pydantic, to_pydantic


@dc.entity
class Locality:
    """A place a mineral was found."""

    qid: Annotated[str, dc.Unique]
    name: str


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


@dc.entity(frozen=True)
class LogEntry:
    """A frozen cabinet log line."""

    note: str
    kind: Annotated[str, dc.Index] = "misc"


@dc.entity
class Sample:
    """A specimen with a small inline binary fingerprint.

    The fingerprint is a plain ``bytes`` field, not a ``dc.Blob``; the create face must
    round-trip arbitrary binary.
    """

    qid: Annotated[str, dc.Unique]
    fingerprint: bytes = b""


def _record_error_type(captured: list[type[BaseException]], call: Callable[[], object]) -> None:
    """Thread target: run ``call`` and record the type of the exception it raises."""
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 — record the type
        captured.append(type(exc))


class Stock(NamedTuple):
    """A committed quartz-from-Brazil pair with the OIDs the store gave them."""

    mineral: Mineral
    loc: Locality
    mineral_oid: int
    loc_oid: int


@pytest.fixture
def stock(store: dc.Store) -> Stock:
    """Commit a quartz-from-Brazil pair and return it with its OIDs."""
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
    return Stock(mineral, loc, store.store(mineral), store.store(loc))


# --- the headline reconstruction (a fresh entity via the public constructor) --------


def test_from_pydantic_yields_a_fresh_entity_the_store_does_not_know(store: dc.Store) -> None:
    """Given a scalar-only create DTO
    When from_pydantic rebuilds it as a Mineral
    Then the result is a Mineral
    And it carries the DTO's qid, name and hardness
    And a field can be assigned before it is stored
    And storing and committing it creates one record holding the assigned value
    """
    create = entity_model(Mineral, face="create")
    dto = create.model_validate({"qid": "Q-NEW", "name": "fresh", "mohs": 5.5})

    mineral = from_pydantic(dto, Mineral)

    assert isinstance(mineral, Mineral)
    assert mineral.qid == "Q-NEW"
    assert mineral.name == "fresh"
    assert mineral.mohs == 5.5
    mineral.name = "renamed"
    store.store(mineral)
    store.commit()
    stored = store.get(Mineral, qid="Q-NEW")
    assert store.count(Mineral) == 1
    assert stored is not None
    assert stored.name == "renamed"


def test_reconstructed_entity_commits_and_round_trips(store: dc.Store) -> None:
    """Given a Mineral rebuilt from a create DTO
    When it is rooted and committed
    Then the OID it earned through the engine reads back that record
    And a fresh get reads back its name
    And a fresh get reads back its tags
    """
    dto = entity_model(Mineral, face="create").model_validate(
        {"qid": "Q-RT", "name": "roundtrip", "tags": ["a", "b"]}
    )
    mineral = from_pydantic(dto, Mineral)

    store.root = mineral
    store.commit()

    fetched = store.get(Mineral, qid="Q-RT")
    assert fetched is not None
    assert store.get_many([store.store(mineral)])[0] is fetched
    assert fetched.name == "roundtrip"
    assert list(fetched.tags) == ["a", "b"]


def test_missing_defaulted_field_falls_to_dataclass_default() -> None:
    """Given a create DTO that omits every defaulted Mineral field
    When from_pydantic rebuilds it
    Then the crystal system is None
    And the hardness is None
    And the tags are an empty list
    """
    dto = entity_model(Mineral, face="create").model_validate({"qid": "Q-D", "name": "d"})

    mineral = from_pydantic(dto, Mineral)

    assert mineral.crystal_system is None
    assert mineral.mohs is None
    assert list(mineral.tags) == []


# --- the to_pydantic <-> from_pydantic round-trip ------------------------------


def test_round_trips_through_to_pydantic(store: dc.Store, stock: Stock) -> None:
    """Given a committed quartz projected by to_pydantic
    When from_pydantic rebuilds it
    Then the qid and hardness survive verbatim
    And the discovered_in reference stays the locality's OID
    """
    dto = to_pydantic(stock.mineral)

    rebuilt = from_pydantic(dto, Mineral)

    assert rebuilt.qid == "Q-QTZ"
    assert rebuilt.mohs == 7.0
    assert rebuilt.discovered_in == stock.loc_oid


# --- reference fields: OID by default, resolved with a store -------------------


def test_ref_fields_stay_oids_without_store(stock: Stock) -> None:
    """Given a DTO whose reference fields hold the locality's OID
    When from_pydantic rebuilds it without a store
    Then discovered_in stays the raw OID
    And type_locality stays the raw OID
    """
    dto = entity_model(Mineral).model_validate(
        {
            "qid": "Q-REF",
            "name": "ref",
            "discovered_in": stock.loc_oid,
            "type_locality": stock.loc_oid,
        }
    )

    rebuilt = from_pydantic(dto, Mineral)

    assert rebuilt.discovered_in == stock.loc_oid
    assert rebuilt.type_locality == stock.loc_oid


def test_ref_fields_resolve_with_store(store: dc.Store, stock: Stock) -> None:
    """Given a DTO whose reference fields hold the locality's OID
    When from_pydantic rebuilds it with store=
    Then discovered_in is the live locality, by identity
    And type_locality is a Lazy
    And that Lazy peeks the live locality without forcing a load
    """
    dto = entity_model(Mineral).model_validate(
        {
            "qid": "Q-RES",
            "name": "res",
            "discovered_in": stock.loc_oid,
            "type_locality": stock.loc_oid,
        }
    )

    rebuilt = from_pydantic(dto, Mineral, store=store)

    assert rebuilt.discovered_in is stock.loc
    assert isinstance(rebuilt.type_locality, dc.Lazy)
    assert rebuilt.type_locality.peek() is stock.loc


def test_resolved_entity_commits_cleanly(store: dc.Store, stock: Stock) -> None:
    """Given a Mineral rebuilt with store= so its references are live entities
    When it is rooted and committed
    Then the store holds it as a second mineral
    """
    dto = entity_model(Mineral).model_validate(
        {
            "qid": "Q-RES",
            "name": "res",
            "discovered_in": stock.loc_oid,
            "type_locality": stock.loc_oid,
        }
    )
    rebuilt = from_pydantic(dto, Mineral, store=store)

    store.root = [rebuilt]
    store.commit()

    assert store.count(Mineral) == 2


def test_none_ref_stays_none_with_store(store: dc.Store, stock: Stock) -> None:
    """Given a DTO with no reference values
    When from_pydantic rebuilds it with store=
    Then discovered_in is None
    And type_locality is None
    """
    dto = entity_model(Mineral).model_validate({"qid": "Q-N", "name": "n"})

    rebuilt = from_pydantic(dto, Mineral, store=store)

    assert rebuilt.discovered_in is None
    assert rebuilt.type_locality is None


def test_store_resolution_is_owner_confined(store: dc.Store, stock: Stock) -> None:
    """Given a DTO with a reference OID and a store owned by this thread
    When a foreign thread calls from_pydantic with store=
    Then the call raises WrongThreadError
    """
    dto = entity_model(Mineral).model_validate(
        {"qid": "Q-T", "name": "t", "discovered_in": stock.loc_oid}
    )
    captured: list[type[BaseException]] = []
    resolve = functools.partial(from_pydantic, dto, Mineral, store=store)
    worker = threading.Thread(target=_record_error_type, args=(captured, resolve))

    worker.start()
    worker.join(timeout=10)

    assert captured == [dc.WrongThreadError]


# --- frozen entity reconstructs ------------------------------------------------


def test_frozen_entity_reconstructs(store: dc.Store) -> None:
    """Given a plain-face DTO of a frozen LogEntry
    When from_pydantic rebuilds it
    Then the result is a LogEntry
    And it carries the DTO's note and kind
    """
    dto = entity_model(LogEntry).model_validate({"note": "found in drawer 3", "kind": "find"})

    entry = from_pydantic(dto, LogEntry)

    assert isinstance(entry, LogEntry)
    assert entry.note == "found in drawer 3"
    assert entry.kind == "find"


def test_frozen_entity_commits_and_reads_back(store: dc.Store) -> None:
    """Given a frozen LogEntry rebuilt from a DTO
    When it is rooted and committed
    Then it is read back by the OID it earned with its note
    """
    dto = entity_model(LogEntry).model_validate({"note": "found in drawer 3", "kind": "find"})
    entry = from_pydantic(dto, LogEntry)

    store.root = entry
    store.commit()

    fetched = store.get_many([store.store(entry)])[0]
    assert fetched.note == "found in drawer 3"


# --- create / public face split ------------------------------------------------


def test_create_face_omits_oid_and_public_face_carries_it() -> None:
    """Given the create and public faces of Mineral
    When their fields are listed
    Then the create face has no oid field
    And the public face has an oid field
    """
    create = entity_model(Mineral, face="create")
    public = entity_model(Mineral, face="public")

    assert "oid" not in create.model_fields
    assert "oid" in public.model_fields


def test_face_models_are_named_for_their_face() -> None:
    """Given the create and public faces of Mineral
    When their class names are read
    Then the create face is MineralCreate
    And the public face is MineralPublic
    """
    create = entity_model(Mineral, face="create")
    public = entity_model(Mineral, face="public")

    assert create.__name__ == "MineralCreate"
    assert public.__name__ == "MineralPublic"


def test_public_face_is_distinct_from_the_other_faces() -> None:
    """Given the create, public and plain faces of Mineral
    When they are compared
    Then the public face is not the create face
    And the public face is not the plain face
    """
    create = entity_model(Mineral, face="create")
    public = entity_model(Mineral, face="public")

    assert public is not create
    assert public is not entity_model(Mineral)


def test_public_face_carries_oid_from_a_view(store: dc.Store, stock: Stock) -> None:
    """Given a committed quartz read through a snapshot view
    When to_pydantic projects the view with face="public"
    Then the DTO is an instance of the public face
    And it carries the engine-assigned oid
    And it carries the qid
    """
    with store.snapshot() as snap:
        view = snap.get(stock.mineral_oid)
        dto = to_pydantic(view, face="public")

    assert isinstance(dto, entity_model(Mineral, face="public"))
    assert dto.oid == stock.mineral_oid  # pyrefly: ignore[missing-attribute]
    assert dto.qid == "Q-QTZ"  # pyrefly: ignore[missing-attribute]


def test_public_face_from_a_live_entity(stock: Stock) -> None:
    """Given a committed live quartz
    When to_pydantic projects it with face="public"
    Then the DTO carries the engine-assigned oid
    And it carries the name
    """
    dto = to_pydantic(stock.mineral, face="public")

    assert dto.oid == stock.mineral_oid  # pyrefly: ignore[missing-attribute]
    assert dto.name == "quartz"  # pyrefly: ignore[missing-attribute]


def test_public_face_requires_a_committed_source() -> None:
    """Given a Mineral that was never committed and so has no OID
    When to_pydantic projects it with face="public"
    Then it raises ValueError naming the missing OID
    """
    fresh = Mineral(qid="X", name="x")

    with pytest.raises(ValueError, match="OID-bearing"):
        to_pydantic(fresh, face="public")


def test_from_attributes_reads_scalars_off_a_view(store: dc.Store) -> None:
    """Given a committed LogEntry read through a snapshot view
    When the plain-face model validates the view by attribute
    Then the DTO carries the note
    And it carries the default kind
    """
    entry = LogEntry(note="raw read")
    store.root = entry
    store.commit()

    with store.snapshot() as snap:
        view = snap.query(LogEntry)[0]
        dto = entity_model(LogEntry).model_validate(view)

    assert dto.note == "raw read"  # pyrefly: ignore[missing-attribute]
    assert dto.kind == "misc"  # pyrefly: ignore[missing-attribute]


# --- loud rejection ------------------------------------------------------------


def test_rejects_non_entity_class() -> None:
    """Given a valid Mineral DTO
    When from_pydantic is asked to rebuild it as dict
    Then it raises NotAnEntityError
    """
    dto = entity_model(Mineral).model_validate({"qid": "Q", "name": "q"})

    with pytest.raises(dc.NotAnEntityError):
        from_pydantic(dto, dict)


# --- binary bytes round-trip (#153 peer-review fix: base64, not utf-8) ----------


def test_binary_bytes_field_round_trips_through_json() -> None:
    """Given a sample whose fingerprint is not valid utf-8
    When it is dumped to the create-face JSON, validated and rebuilt by from_pydantic
    Then the wire value is base64 text, not raw bytes
    And the rebuilt fingerprint is the exact original bytes
    """
    binary = b"\x89PNG\r\n\x00\xff\xfe"
    create = entity_model(Sample, face="create")
    wire = to_pydantic(Sample(qid="S1", fingerprint=binary), face="create").model_dump(
        mode="json"
    )

    rebuilt = from_pydantic(create.model_validate(wire), Sample)

    assert isinstance(wire["fingerprint"], str)
    assert rebuilt.fingerprint == binary
