"""W4-6: the CAPSTONE read-fence sieve — one protected record, EVERY public read surface.

ADR-008, epic #168, issue #174. The per-tier sieves already prove each tier in isolation: the live
store (``tests/unit/test_read_fence_sieve.py``, W3), the snapshot
(``tests/unit/test_read_fence_snapshot.py``, W4-1/2/3), the ``[web]`` per-request principal
(``tests/web/test_read_principal.py``, W4-4), and the ``[fts]`` post-filter
(``tests/extras/test_fts.py``, W4-5). This module is the campaign's *completeness* proof: it takes
ONE protected ``Specimen`` — owned by principal ``A``, shared to nobody — and drives the SAME
record through the whole public read matrix at once, asserting it is invisible-or-denied to a
reader ``B`` who has no standing, and fully readable to ``A`` and to ``ROOT``. Each surface is its
own test, so a reviewer reads the matrix off the failures; a future surface that forgets the fence
fails HERE, naming itself, rather than leaking silently three PRs later.

The matrix (surface -> expected for B / expected for A+ROOT):

LIVE STORE (``store.acting_as(B)``)
  get/get_many(key=)/query/query_iter        -> absent            / present
  count/pluck/explain.extent/.candidates     -> excluded numbers  / included
  incoming(anchor)                           -> denied ref dropped / present
  Lazy[Specimen].get() / get_many([oid])     -> redacted twin      / real record
  upsert(probe with the key)                 -> ReadDeniedError    / real survivor
SNAPSHOT (``store.snapshot(principal=B)``)
  get()                                      -> ReadDeniedError    / view
  get_many()                                 -> redacted twin slot / view
  all(cls)/all("Specimen")/query/count       -> excluded           / included
  all(str) over a no-live _dc_* type         -> fails closed       / (root) dumps
  explain / incoming                         -> filtered / dropped / included
  open_blob                                  -> ReadDeniedError    / bytes
  index_bitmaps(protected)                   -> raises (even root) / raises
  _stream("Specimen")                        -> raises (non-root)  / (root) streams
WEB (per-request ``principal=B``)
  REST discovery / GraphQL discovery         -> excluded           / included
  REST ref / GraphQL ref                     -> wire-null          / rendered
  any ``_dc_*`` column on the wire           -> never              / never
FTS
  search(q, snapshot=B)                      -> denied hit dropped / included
  search(q) with NO snapshot (protected)     -> ReadDeniedError    / (root snap) hit
BLOBS
  store.open_blob / snapshot.open_blob       -> ReadDeniedError    / bytes
  BlobHandle.bytes()                         -> ReadDeniedError    / bytes

The live/snapshot/blob tiers ride both backends via the ``store``/``store_factory`` fixtures
(``tests/conftest.py``); the ``[web]`` and ``[fts]`` tiers skip cleanly when their extra is absent
(a fixture ``importorskip``), so the extra-free tiers of the matrix always run.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, NamedTuple

import pytest

import datacrystal as dc
from datacrystal._entity import TYPES_BY_NAME, oid_of, type_info
from tests.extras.fence_world import (
    CAPSTONE_FULLTEXT,
    LABEL,
    PRINCIPALS,
    ROOT,
    SECRET_BYTES,
    SPECIMEN_TYPENAME,
    TERM,
    A,
    B,
    Cabinet,
    Specimen,
    seed,
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from datacrystal.fts import FullTextIndex

READERS = pytest.mark.parametrize("who", ["A", "ROOT"])


class Seeded(NamedTuple):
    """The seeded world: the hidden record, its WORLD-readable anchor and the holding cabinet."""

    hidden: Specimen
    anchor: Specimen
    cabinet: Cabinet


def oid_int(entity: object) -> int:
    """Return the oid of a committed entity."""
    oid = oid_of(entity)
    if oid is None:
        raise LookupError("entity was not committed")
    return oid


@pytest.fixture
def seeded(store: dc.Store) -> Seeded:
    """Seed the protected world into the store."""
    return Seeded(*seed(store))


@pytest.fixture
def principal(who: str) -> dc.Principal:
    """Return the principal a parametrized ``who`` names."""
    return PRINCIPALS[who]


# === LIVE STORE ==============================================================

def test_get_hides_the_record_from_a_denied_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record owned by A and shared to nobody
    When B asks the live store for the record by its unique key with get
    Then B gets nothing
    """
    with store.acting_as(B):
        found = store.get(Specimen, label=LABEL)

    assert found is None


@READERS
def test_get_shows_the_record_to_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner or root asks the live store for the record by its unique key with get
    Then the record is found
    """
    with store.acting_as(principal):
        found = store.get(Specimen, label=LABEL)

    assert found is not None


def test_get_many_by_key_hides_the_record_from_a_denied_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record owned by A and shared to nobody
    When B asks the live store for the record by its unique key with get_many
    Then B gets nothing
    """
    with store.acting_as(B):
        [found] = store.get_many(Specimen, label=[LABEL])

    assert found is None


@READERS
def test_get_many_by_key_shows_the_record_to_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner or root asks the live store for the record by its unique key with get_many
    Then the record is found
    """
    with store.acting_as(principal):
        [found] = store.get_many(Specimen, label=[LABEL])

    assert found is not None


def test_query_by_class_hides_the_record_from_a_denied_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record owned by A and shared to nobody
    When B asks the live store for every Specimen with query
    Then the record is not among them
    """
    with store.acting_as(B):
        found = store.query(Specimen)

    assert LABEL not in {specimen.label for specimen in found}


@READERS
def test_query_by_class_shows_the_record_to_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner or root asks the live store for every Specimen with query
    Then the record is among them
    """
    with store.acting_as(principal):
        found = store.query(Specimen)

    assert LABEL in {specimen.label for specimen in found}


def test_query_by_condition_hides_the_record_from_a_denied_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record owned by A and shared to nobody
    When B asks the live store for the record by a condition with query
    Then the record is not among them
    """
    with store.acting_as(B):
        found = store.query(dc.fields(Specimen).label == LABEL)

    assert LABEL not in {specimen.label for specimen in found}


@READERS
def test_query_by_condition_shows_the_record_to_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner or root asks the live store for the record by a condition with query
    Then the record is among them
    """
    with store.acting_as(principal):
        found = store.query(dc.fields(Specimen).label == LABEL)

    assert LABEL in {specimen.label for specimen in found}


def test_query_iter_hides_the_record_from_a_denied_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record owned by A and shared to nobody
    When B asks the live store for every Specimen with query_iter
    Then the record is not among them
    """
    with store.acting_as(B):
        found = list(store.query_iter(Specimen))

    assert LABEL not in {specimen.label for specimen in found}


@READERS
def test_query_iter_shows_the_record_to_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner or root asks the live store for every Specimen with query_iter
    Then the record is among them
    """
    with store.acting_as(principal):
        found = list(store.query_iter(Specimen))

    assert LABEL in {specimen.label for specimen in found}


def test_count_hides_the_record_from_a_denied_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record owned by A and shared to nobody
    When B asks the live store to count the record by a condition
    Then the count is 0
    """
    with store.acting_as(B):
        found = store.count(dc.fields(Specimen).label == LABEL)

    assert found == 0


@READERS
def test_count_shows_the_record_to_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner or root asks the live store to count the record by a condition
    Then the count is 1
    """
    with store.acting_as(principal):
        found = store.count(dc.fields(Specimen).label == LABEL)

    assert found == 1


def test_pluck_hides_the_record_from_a_denied_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record owned by A and shared to nobody
    When B asks the live store to pluck every Specimen label
    Then the record's label is not among them
    """
    with store.acting_as(B):
        found = store.pluck(Specimen, "label")

    assert LABEL not in found


@READERS
def test_pluck_shows_the_record_to_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner or root asks the live store to pluck every Specimen label
    Then the record's label is among them
    """
    with store.acting_as(principal):
        found = store.pluck(Specimen, "label")

    assert LABEL in found


def test_explain_extent_hides_the_record_from_a_denied_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record owned by A and shared to nobody
    When B asks the live store to explain the Specimen extent
    Then the extent is 1, the anchor alone
    """
    with store.acting_as(B):
        plan = store.explain(Specimen)

    assert plan.extent == 1


@READERS
def test_explain_extent_shows_the_record_to_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner or root asks the live store to explain the Specimen extent
    Then the extent is 2
    """
    with store.acting_as(principal):
        plan = store.explain(Specimen)

    assert plan.extent == 2


def test_explain_candidates_hides_the_record_from_a_denied_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record owned by A and shared to nobody
    When B asks the live store to explain the record's condition
    Then there are 0 candidates
    """
    with store.acting_as(B):
        plan = store.explain(dc.fields(Specimen).label == LABEL)

    assert plan.candidates == 0


@READERS
def test_explain_candidates_shows_the_record_to_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner or root asks the live store to explain the record's condition
    Then there is 1 candidate
    """
    with store.acting_as(principal):
        plan = store.explain(dc.fields(Specimen).label == LABEL)

    assert plan.candidates == 1


def test_incoming_hides_the_record_from_a_denied_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record owned by A and shared to nobody
    When B asks the live store for the referrers of the WORLD anchor with incoming
    Then the record is not among the referrers
    """
    with store.acting_as(B):
        found = store.incoming(seeded.anchor)

    assert LABEL not in {getattr(referrer, "label", None) for referrer in found}


@READERS
def test_incoming_shows_the_record_to_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner or root asks the live store for the referrers of the WORLD anchor with incoming
    Then the record is among the referrers
    """
    with store.acting_as(principal):
        found = store.incoming(seeded.anchor)

    assert LABEL in {getattr(referrer, "label", None) for referrer in found}


def test_get_many_by_oid_hands_a_denied_reader_a_masked_twin(
    store: dc.Store, seeded: Seeded
) -> None:
    """Given a protected record owned by A and shared to nobody
    When B fetches its oid with get_many
    Then B gets a redacted twin
    And the twin is still a Specimen for traversal
    And reading the twin's note is denied
    """
    with store.acting_as(B):
        [twin] = store.get_many([oid_int(seeded.hidden)])

    assert isinstance(twin, dc.Redacted)
    assert isinstance(twin, Specimen)
    with pytest.raises(dc.ReadDeniedError):
        _ = twin.note


def test_lazy_get_hands_a_denied_reader_a_masked_twin(store: dc.Store, seeded: Seeded) -> None:
    """Given a cabinet that holds a Lazy handle onto a protected record
    When B dereferences the handle
    Then B gets a redacted twin
    And the twin is still a Specimen for traversal
    And reading the twin's note is denied
    """
    with store.acting_as(B):
        twin = seeded.cabinet.holds.get()  # pyrefly: ignore[missing-attribute]

    assert isinstance(twin, dc.Redacted)
    assert isinstance(twin, Specimen)
    with pytest.raises(dc.ReadDeniedError):
        _ = twin.note


@READERS
def test_get_many_by_oid_hands_owner_and_root_the_real_record(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner or root fetches its oid with get_many
    Then the record is real, not a twin
    And it carries its label
    """
    with store.acting_as(principal):
        [record] = store.get_many([oid_int(seeded.hidden)])

    assert not isinstance(record, dc.Redacted)
    assert record.label == LABEL


@READERS
def test_lazy_get_hands_owner_and_root_the_real_record(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a cabinet that holds a Lazy handle onto a protected record
    When the owner or root dereferences the handle
    Then the record is real, not a twin
    And it carries its label
    """
    with store.acting_as(principal):
        record = seeded.cabinet.holds.get()  # pyrefly: ignore[missing-attribute]

    assert not isinstance(record, dc.Redacted)
    assert record.label == LABEL


def test_upsert_of_the_key_is_denied_to_a_reader_without_standing(
    store: dc.Store, seeded: Seeded
) -> None:
    """Given a protected record B knows only by its unique key
    When B upserts a probe with that key
    Then the committed survivor is fenced with ReadDeniedError, never handed back
    """
    with store.acting_as(B), pytest.raises(dc.ReadDeniedError):
        store.upsert(Specimen(label=LABEL))


@READERS
def test_upsert_of_the_key_hands_owner_and_root_the_real_survivor(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record committed under a unique key
    When the owner or root upserts a probe with that key
    Then the survivor is real, not a twin
    And it carries its label
    """
    with store.acting_as(principal):
        survivor = store.upsert(Specimen(label=LABEL))

    assert not isinstance(survivor, dc.Redacted)
    assert survivor.label == LABEL


# === SNAPSHOT ================================================================


def test_snapshot_get_denies_a_reader_without_standing(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record owned by A and shared to nobody
    When B's snapshot gets it by oid
    Then the strict deref is denied
    """
    with store.snapshot(principal=B) as snapshot, pytest.raises(dc.ReadDeniedError):
        snapshot.get(oid_int(seeded.hidden))


def test_snapshot_get_many_hands_a_denied_reader_a_twin_slot(
    store: dc.Store, seeded: Seeded
) -> None:
    """Given a protected record owned by A and shared to nobody
    When B's snapshot fetches its oid with get_many
    Then the slot is a redacted twin
    And the twin's identity is readable
    And reading the twin's note is denied
    """
    with store.snapshot(principal=B) as snapshot:
        [twin] = snapshot.get_many([oid_int(seeded.hidden)])

    assert isinstance(twin, dc.Redacted)
    assert twin.oid == oid_int(seeded.hidden)
    with pytest.raises(dc.ReadDeniedError):
        _ = twin.note


def test_snapshot_all_by_class_excludes_the_record_for_a_denied_reader(
    store: dc.Store, seeded: Seeded
) -> None:
    """Given a protected record owned by A and shared to nobody
    When B's snapshot lists all Specimens by class
    Then the record is not among them
    """
    with store.snapshot(principal=B) as snapshot:
        listed = snapshot.all(Specimen)

    assert LABEL not in {specimen.label for specimen in listed}


def test_snapshot_all_by_typename_excludes_the_record_for_a_denied_reader(
    store: dc.Store, seeded: Seeded
) -> None:
    """Given a protected record owned by A and shared to nobody
    When B's snapshot lists all Specimens by typename
    Then the record is not among them
    """
    with store.snapshot(principal=B) as snapshot:
        listed = snapshot.all(SPECIMEN_TYPENAME)

    assert LABEL not in {specimen.label for specimen in listed}


def test_snapshot_query_excludes_the_record_for_a_denied_reader(
    store: dc.Store, seeded: Seeded
) -> None:
    """Given a protected record owned by A and shared to nobody
    When B's snapshot queries every Specimen
    Then the record is not among them
    """
    with store.snapshot(principal=B) as snapshot:
        found = snapshot.query(Specimen)

    assert LABEL not in {specimen.label for specimen in found}


def test_snapshot_count_excludes_the_record_for_a_denied_reader(
    store: dc.Store, seeded: Seeded
) -> None:
    """Given a protected record owned by A and shared to nobody
    When B's snapshot counts the record by a condition
    Then the count is 0
    """
    with store.snapshot(principal=B) as snapshot:
        counted = snapshot.count(dc.fields(Specimen).label == LABEL)  # pyrefly: ignore[bad-argument-type]

    assert counted == 0


def test_snapshot_explain_finds_no_candidates_for_a_denied_reader(
    store: dc.Store, seeded: Seeded
) -> None:
    """Given a protected record owned by A and shared to nobody
    When B's snapshot explains the record's condition
    Then there are 0 candidates
    """
    with store.snapshot(principal=B) as snapshot:
        plan = snapshot.explain(dc.fields(Specimen).label == LABEL)  # pyrefly: ignore[bad-argument-type]

    assert plan.candidates == 0


def test_snapshot_incoming_drops_the_denied_referrer(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected record that links to a WORLD-readable anchor
    When B's snapshot lists the anchor's referrers
    Then the record is not among them
    """
    with store.snapshot(principal=B) as snapshot:
        referrers = snapshot.incoming(oid_int(seeded.anchor))

    assert LABEL not in {getattr(referrer, "label", None) for referrer in referrers}


def test_snapshot_open_blob_denies_a_reader_without_standing(
    store: dc.Store, seeded: Seeded
) -> None:
    """Given a protected record with a scan blob
    When B's snapshot opens the blob
    Then the open is denied
    """
    with store.snapshot(principal=B) as snapshot, pytest.raises(dc.ReadDeniedError):
        snapshot.open_blob(oid_int(seeded.hidden), "scan")


def test_snapshot_index_bitmaps_refuses_a_denied_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected class
    When B's snapshot asks for its index bitmaps
    Then the request is refused
    """
    with store.snapshot(principal=B) as snapshot, pytest.raises(dc.ReadDeniedError):
        snapshot.index_bitmaps(Specimen)


def test_snapshot_stream_refuses_a_non_root_reader(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected class mirrored to a stream
    When B's snapshot streams it, the non-root mirror guard
    Then the stream is refused
    """
    with store.snapshot(principal=B) as snapshot, pytest.raises(dc.ReadDeniedError):
        list(snapshot._stream(SPECIMEN_TYPENAME))


@READERS
def test_snapshot_get_shows_owner_and_root_the_record(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner's or root's snapshot gets it by oid
    Then the view carries its label
    """
    with store.snapshot(principal=principal) as snapshot:
        view = snapshot.get(oid_int(seeded.hidden))

    assert view.label == LABEL  # pyrefly: ignore[missing-attribute]


@READERS
def test_snapshot_get_many_shows_owner_and_root_the_real_record(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner's or root's snapshot fetches its oid with get_many
    Then the record is real, not a twin
    And it carries its label
    """
    with store.snapshot(principal=principal) as snapshot:
        [record] = snapshot.get_many([oid_int(seeded.hidden)])

    assert not isinstance(record, dc.Redacted)
    assert record.label == LABEL  # pyrefly: ignore[missing-attribute]


@READERS
def test_snapshot_all_by_class_lists_the_record_for_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner's or root's snapshot lists all Specimens by class
    Then the record is among them
    """
    with store.snapshot(principal=principal) as snapshot:
        listed = snapshot.all(Specimen)

    assert LABEL in {specimen.label for specimen in listed}


@READERS
def test_snapshot_all_by_typename_lists_the_record_for_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner's or root's snapshot lists all Specimens by typename
    Then the record is among them
    """
    with store.snapshot(principal=principal) as snapshot:
        listed = snapshot.all(SPECIMEN_TYPENAME)

    assert LABEL in {specimen.label for specimen in listed}


@READERS
def test_snapshot_count_includes_the_record_for_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner's or root's snapshot counts the record by a condition
    Then the count is 1
    """
    with store.snapshot(principal=principal) as snapshot:
        counted = snapshot.count(dc.fields(Specimen).label == LABEL)  # pyrefly: ignore[bad-argument-type]

    assert counted == 1


@READERS
def test_snapshot_explain_finds_the_candidate_for_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record owned by A and shared to nobody
    When the owner's or root's snapshot explains the record's condition
    Then there is 1 candidate
    """
    with store.snapshot(principal=principal) as snapshot:
        plan = snapshot.explain(dc.fields(Specimen).label == LABEL)  # pyrefly: ignore[bad-argument-type]

    assert plan.candidates == 1


@READERS
def test_snapshot_incoming_keeps_the_readable_referrer_for_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record that links to a WORLD-readable anchor
    When the owner's or root's snapshot lists the anchor's referrers
    Then the record is among them
    """
    with store.snapshot(principal=principal) as snapshot:
        referrers = snapshot.incoming(oid_int(seeded.anchor))

    assert LABEL in {getattr(referrer, "label", None) for referrer in referrers}


@READERS
def test_snapshot_open_blob_reads_the_scan_for_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected record with a scan blob
    When the owner's or root's snapshot opens the blob
    Then its bytes are the scan
    """
    with store.snapshot(principal=principal) as snapshot, snapshot.open_blob(
        oid_int(seeded.hidden), "scan"
    ) as blob:
        content = blob.read()

    assert content == SECRET_BYTES


@READERS
def test_snapshot_index_bitmaps_refuses_even_owner_and_root(
    store: dc.Store, seeded: Seeded, principal: dc.Principal
) -> None:
    """Given a protected class
    When the owner's or root's snapshot asks for its index bitmaps
    Then the request is refused for everyone
    """
    with store.snapshot(principal=principal) as snapshot, pytest.raises(dc.ReadDeniedError):
        snapshot.index_bitmaps(Specimen)


def test_snapshot_stream_gives_root_the_protected_mirror(store: dc.Store, seeded: Seeded) -> None:
    """Given a protected class holding the anchor and the hidden record
    When root's snapshot streams it
    Then two records stream
    """
    with store.snapshot(principal=ROOT) as snapshot:
        streamed = list(snapshot._stream(SPECIMEN_TYPENAME))

    assert len(streamed) == 2


@pytest.fixture
def without_live_specimen_class(seeded: Seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the live Specimen class from the type registry, leaving its persisted lineage."""
    monkeypatch.delitem(TYPES_BY_NAME, type_info(Specimen).typename)


@pytest.mark.usefixtures("without_live_specimen_class")
def test_snapshot_all_by_typename_fails_closed_without_the_live_class(store: dc.Store) -> None:
    """Given a persisted _dc_* lineage whose live class was removed
    When a non-root snapshot lists it by typename
    Then it fails closed, as no readable bitmap compiles without the class
    """
    with store.snapshot(principal=B) as snapshot, pytest.raises(dc.ReadDeniedError):
        snapshot.all(SPECIMEN_TYPENAME)


@pytest.mark.usefixtures("without_live_specimen_class")
def test_snapshot_all_by_typename_dumps_the_lineage_for_root(store: dc.Store) -> None:
    """Given a persisted _dc_* lineage whose live class was removed
    When root's snapshot lists it by typename
    Then both records are dumped (R9)
    """
    with store.snapshot(principal=ROOT) as snapshot:
        dumped = snapshot.all(SPECIMEN_TYPENAME)

    assert len(dumped) == 2


# === WEB (datacrystal[web]) ==================================================


@pytest.fixture
def fence_web() -> ModuleType:
    """Import the web helper, skipping when a datacrystal[web] dependency is missing."""
    pytest.importorskip("pydantic", reason="datacrystal[web] extra not installed")
    pytest.importorskip("fastapi", reason="datacrystal[web] extra not installed")
    pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")
    pytest.importorskip("httpx", reason="pip install httpx (ASGITransport)")
    from tests.extras import fence_web as web_helpers

    return web_helpers


@pytest.fixture
def web_store(fence_web: ModuleType) -> Iterator[dc.Store]:
    """Open the web tier's in-memory store holding the hidden record, and close it afterwards."""
    store = fence_web.seed_web_store()
    yield store
    store.close()


@pytest.fixture
def rest_client_as(
    fence_web: ModuleType, web_store: dc.Store
) -> Callable[[dc.Principal], TestClient]:
    """Return a factory for a REST test client whose requests read as the given principal."""
    from fastapi.testclient import TestClient

    app = fence_web.rest_app(web_store)

    def client_as(reader: dc.Principal) -> TestClient:
        return TestClient(fence_web.read_as(app, reader))

    return client_as


@pytest.fixture
def graphql_client_as(
    fence_web: ModuleType, web_store: dc.Store
) -> Callable[[dc.Principal], TestClient]:
    """Return a factory for a GraphQL test client whose requests read as the given principal."""
    from fastapi.testclient import TestClient

    app = fence_web.graphql_app(web_store)

    def client_as(reader: dc.Principal) -> TestClient:
        return TestClient(fence_web.read_as(app, reader))

    return client_as


GRAPHQL_CABINET = '{ cabinet(tag: "d1") { tag holds { label } } }'


def test_rest_discovery_excludes_the_record_for_a_denied_reader(rest_client_as: Callable[[dc.Principal], TestClient]) -> None:
    """Given a REST app over a protected record
    When B lists the specimens
    Then no label is listed
    """
    response = rest_client_as(B).get("/specimens")

    assert response.json() == {"labels": []}


def test_rest_ref_is_wire_null_for_a_denied_reader(rest_client_as: Callable[[dc.Principal], TestClient]) -> None:
    """Given a REST app whose cabinet refers to a protected record
    When B fetches the cabinet
    Then the cabinet is found
    And its reference to the record is null on the wire
    """
    response = rest_client_as(B).get("/cabinet/d1")

    assert response.json() == {"found": True, "holds": None}


@READERS
def test_rest_discovery_lists_the_record_for_owner_and_root(
    rest_client_as: Callable[[dc.Principal], TestClient], principal: dc.Principal
) -> None:
    """Given a REST app over a protected record
    When the owner or root lists the specimens
    Then the record's label is listed
    """
    response = rest_client_as(principal).get("/specimens")

    assert response.json() == {"labels": [LABEL]}


@READERS
def test_rest_ref_is_rendered_for_owner_and_root(
    rest_client_as: Callable[[dc.Principal], TestClient], principal: dc.Principal
) -> None:
    """Given a REST app whose cabinet refers to a protected record
    When the owner or root fetches the cabinet
    Then the cabinet is found
    And the reference renders the record's label
    """
    body = rest_client_as(principal).get("/cabinet/d1").json()

    assert body["found"] is True
    assert body["holds"]["label"] == LABEL


@READERS
def test_rest_wire_carries_no_lib_managed_column(
    fence_web: ModuleType, rest_client_as: Callable[[dc.Principal], TestClient], principal: dc.Principal
) -> None:
    """Given a REST app whose cabinet refers to a protected record
    When the owner or root fetches the cabinet
    Then no _dc_* column is on the wire
    """
    body = rest_client_as(principal).get("/cabinet/d1").json()

    assert fence_web.dc_keys_on_wire(body) == []


def test_graphql_request_for_a_denied_reader_succeeds(graphql_client_as: Callable[[dc.Principal], TestClient]) -> None:
    """Given a GraphQL app whose cabinet refers to a protected record
    When B queries the cabinet
    Then the response status is 200
    """
    response = graphql_client_as(B).post("/graphql", json={"query": GRAPHQL_CABINET})

    assert response.status_code == 200


def test_graphql_ref_is_null_for_a_denied_reader(graphql_client_as: Callable[[dc.Principal], TestClient]) -> None:
    """Given a GraphQL app whose cabinet refers to a protected record
    When B queries the cabinet
    Then there are no errors
    And the cabinet's reference to the record is null
    """
    body = graphql_client_as(B).post("/graphql", json={"query": GRAPHQL_CABINET}).json()

    assert body.get("errors") is None
    assert body["data"] == {"cabinet": {"tag": "d1", "holds": None}}


@READERS
def test_graphql_ref_is_rendered_for_owner_and_root(
    graphql_client_as: Callable[[dc.Principal], TestClient], principal: dc.Principal
) -> None:
    """Given a GraphQL app whose cabinet refers to a protected record
    When the owner or root queries the cabinet
    Then there are no errors
    And the reference renders the record's label
    """
    body = graphql_client_as(principal).post("/graphql", json={"query": GRAPHQL_CABINET}).json()

    assert body.get("errors") is None
    assert body["data"] == {"cabinet": {"tag": "d1", "holds": {"label": LABEL}}}


@READERS
def test_graphql_wire_carries_no_lib_managed_column(
    fence_web: ModuleType, graphql_client_as: Callable[[dc.Principal], TestClient], principal: dc.Principal
) -> None:
    """Given a GraphQL app whose cabinet refers to a protected record
    When the owner or root queries the cabinet
    Then no _dc_* column is on the wire
    """
    body = graphql_client_as(principal).post("/graphql", json={"query": GRAPHQL_CABINET}).json()

    assert fence_web.dc_keys_on_wire(body) == []


# === FTS (datacrystal[fts]) ==================================================


@pytest.fixture
def fts_seeded(store: dc.Store, tmp_path: Path) -> Iterator[tuple[FullTextIndex, Seeded]]:
    """Attach a full-text index first, then seed so the index rides the commit."""
    pytest.importorskip("snowballstemmer", reason="datacrystal[fts] extra not installed")
    from datacrystal.fts import FullTextIndex

    index = FullTextIndex(tmp_path / "capstone.fts", fulltext=CAPSTONE_FULLTEXT)
    store.attach(index)
    yield index, Seeded(*seed(store))
    index.close()


def test_fts_search_without_a_snapshot_fails_closed(fts_seeded: tuple[FullTextIndex, Seeded]) -> None:
    """Given an index covering a protected class
    When it is searched with no snapshot
    Then the search is denied (R12)
    """
    index, _ = fts_seeded

    with pytest.raises(dc.ReadDeniedError):
        index.search(TERM)


def test_fts_search_drops_the_denied_hit_for_a_denied_reader(
    store: dc.Store, fts_seeded: tuple[FullTextIndex, Seeded]
) -> None:
    """Given an index covering a protected record
    When it is searched through B's snapshot
    Then the denied hit is dropped from the ranked results
    """
    index, _ = fts_seeded

    with store.snapshot(principal=B) as snapshot:
        hits = index.search(TERM, snapshot=snapshot)

    assert hits == []


@READERS
def test_fts_search_returns_the_hit_for_owner_and_root(
    store: dc.Store, fts_seeded: tuple[FullTextIndex, Seeded], principal: dc.Principal
) -> None:
    """Given an index covering a protected record
    When it is searched through the owner's or root's snapshot
    Then the hit is the record
    """
    index, seeded = fts_seeded

    with store.snapshot(principal=principal) as snapshot:
        hits = index.search(TERM, snapshot=snapshot)

    assert {hit.oid for hit in hits} == {oid_int(seeded.hidden)}


# === BLOBS ===================================================================


@pytest.fixture
def seeded_with_blob(store_factory: Callable[[], dc.Store]) -> Iterator[tuple[dc.Store, int]]:
    """Seed a protected record with a scan blob and yield the store that committed it."""
    seeded = store_factory()
    hidden, _anchor, _cabinet = seed(seeded)
    yield seeded, oid_int(hidden)
    seeded.close()


@pytest.fixture
def owner_view(seeded_with_blob: tuple[dc.Store, int]) -> Specimen:
    """Fetch the hidden record as its owner, holding the real entity and its blob handle."""
    store, hidden_oid = seeded_with_blob
    with store.acting_as(A):
        [view] = store.get_many([hidden_oid])
    return view


def test_blob_handle_bytes_are_denied_to_a_reader_without_standing(
    seeded_with_blob: tuple[dc.Store, int], owner_view: Specimen
) -> None:
    """Given a blob handle the owner fetched
    When B reads the SAME handle's bytes
    Then the read is re-gated and denied
    """
    store, _ = seeded_with_blob

    with store.acting_as(B), pytest.raises(dc.ReadDeniedError):
        owner_view.scan.bytes()  # pyrefly: ignore[missing-attribute]


def test_store_open_blob_is_denied_to_a_reader_holding_the_entity(
    seeded_with_blob: tuple[dc.Store, int], owner_view: Specimen
) -> None:
    """Given the real entity in B's hand, fetched by the owner
    When B opens its scan blob through the store
    Then the open is still denied
    """
    store, _ = seeded_with_blob

    with store.acting_as(B), pytest.raises(dc.ReadDeniedError):
        store.open_blob(owner_view, "scan")


@READERS
def test_blob_handle_bytes_are_readable_to_owner_and_root(
    seeded_with_blob: tuple[dc.Store, int], owner_view: Specimen, principal: dc.Principal
) -> None:
    """Given a blob handle the owner fetched
    When the owner or root reads the handle's bytes
    Then they are the scan
    """
    store, _ = seeded_with_blob

    with store.acting_as(principal):
        content = owner_view.scan.bytes()  # pyrefly: ignore[missing-attribute]

    assert content == SECRET_BYTES


@READERS
def test_store_open_blob_is_readable_to_owner_and_root(
    seeded_with_blob: tuple[dc.Store, int], owner_view: Specimen, principal: dc.Principal
) -> None:
    """Given the real entity fetched by the owner
    When the owner or root opens its scan blob through the store
    Then its bytes are the scan
    """
    store, _ = seeded_with_blob

    with store.acting_as(principal), store.open_blob(owner_view, "scan") as blob:
        content = blob.read()

    assert content == SECRET_BYTES


def test_snapshot_open_blob_is_denied_to_a_reader_without_standing(
    seeded_with_blob: tuple[dc.Store, int],
) -> None:
    """Given a protected record with a scan blob, in the committing store
    When B's snapshot opens the blob
    Then the open is denied
    """
    store, hidden_oid = seeded_with_blob

    with store.snapshot(principal=B) as snapshot, pytest.raises(dc.ReadDeniedError):
        snapshot.open_blob(hidden_oid, "scan")


@READERS
def test_snapshot_open_blob_reads_the_scan_for_owner_and_root(
    seeded_with_blob: tuple[dc.Store, int], principal: dc.Principal
) -> None:
    """Given a protected record with a scan blob, in the committing store
    When the owner's or root's snapshot opens the blob
    Then its bytes are the scan
    """
    store, hidden_oid = seeded_with_blob

    with store.snapshot(principal=principal) as snapshot, snapshot.open_blob(
        hidden_oid, "scan"
    ) as blob:
        content = blob.read()

    assert content == SECRET_BYTES
