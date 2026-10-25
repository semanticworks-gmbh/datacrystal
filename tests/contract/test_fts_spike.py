"""The FTS5 consumer spike against REAL engine deltas (M3 exit gate).

KICKOFF M3 exit: "spike proves prior-value sufficiency". The sidecar's
contentless FTS5 table cannot un-index without the old column values, and
the sidecar never reads the store — every un-index below ran on nothing
but ``delta["prior"]``. If priors were insufficient, these tests could not
pass; the draft freezes with the schema validated (COMMIT-DELTA-v1 §3).

The prose is mineral-cabinet German + English on purpose: it pins what
stock unicode61 already gives multilingual corpora (case folding,
diacritic folding) and what it honestly does NOT (stemming, ß/ss and
ue/ü equivalence — Snowball's job, in the datacrystal[fts] extra).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated

import msgspec
import pytest

import datacrystal as dc
from datacrystal import testing as dct
from datacrystal._entity import oid_of, type_info
from datacrystal._storage.memory import MemoryBackend
from datacrystal.contract import DeltaFormatError
from tests.contract.fts_consumer import FtsSidecar, fts5_available

pytestmark = pytest.mark.skipif(
    not fts5_available(), reason="this sqlite3 build lacks FTS5"
)


@dc.entity
class Specimen:
    """Spike-local cabinet entity with prose fields (KICKOFF §5 shape).

    The language declarations are inert today — the spike's unicode61 path
    ignores them; datacrystal[fts] will pick the Snowball stemmer per field.
    """

    catalog_no: Annotated[str, dc.Unique]
    label: str
    notes: Annotated[str, dc.FullText(language="de")] = ""
    description: Annotated[str, dc.FullText(language="en")] = ""


BERGKRISTALL_NOTES = "Klarer Bergkristall aus den Alpen, sechseckige Prismen"
AZURIT_NOTES = "Azurit mit tiefblauen Kristallen aus Tsumeb"
SOLD_NOTES = "Verkauft an einen Sammler in München"


class _CrashMidApply(FtsSidecar):
    """A sidecar that, once armed, dies after the first op of a delta is applied."""

    armed = False

    def _apply_op(self, op: dict) -> None:
        super()._apply_op(op)
        if self.armed:
            raise OSError("injected crash after the first op")


def _fulltext_config() -> dict[str, list[str]]:
    """Return the sidecar config straight from the entity's dc.FullText field specs."""
    ti = type_info(Specimen)
    return {ti.typename: [s.name for s in ti.specs if s.fulltext]}


def _cabinet() -> list[Specimen]:
    """Build the three-specimen cabinet: German crystal, German azurite, English quartz."""
    return [
        Specimen(catalog_no="DC-001", label="Bergkristall", notes=BERGKRISTALL_NOTES),
        Specimen(catalog_no="DC-002", label="Azurit", notes=AZURIT_NOTES),
        Specimen(
            catalog_no="DC-003",
            label="Milky quartz",
            notes="Milky quartz vein specimen from Arkansas, United States",
        ),
    ]


def _oid(specimen: Specimen) -> int:
    """Return the oid of a committed specimen."""
    oid = oid_of(specimen)
    if oid is None:
        raise LookupError("specimen was not committed")
    return oid


def _cid_of(sidecar: FtsSidecar, typename: str) -> int:
    """Return the class id the sidecar recorded for a typename."""
    return next(cid for cid, (name, _) in sidecar._types.items() if name == typename)


def _kit_upsert(oid: int, notes: str) -> dict:
    """Build an upsert op of the conformance-kit stream type."""
    payload = msgspec.msgpack.Encoder().encode(["Q", "name", notes])
    return {"op": "upsert", "oid": oid, "cid": 1, "payload": payload, "prior": None}


def _terms_of(sidecar: FtsSidecar) -> object:
    """Return the sidecar's indexed (oid, term) instances for the kit's content checks."""
    return sidecar.terms()


@pytest.fixture
def sidecar(tmp_path: Path) -> Iterator[FtsSidecar]:
    """Open a fresh sidecar on a per-test SQLite file and close it afterwards."""
    consumer = FtsSidecar(str(tmp_path / "fts.sqlite"), fulltext=_fulltext_config())
    yield consumer
    consumer.close()


@pytest.fixture
def indexed_cabinet(store: dc.Store, sidecar: FtsSidecar) -> list[Specimen]:
    """Commit the cabinet into a store with the sidecar attached and return the specimens."""
    store.attach(sidecar)
    specimens = _cabinet()
    store.root = specimens
    store.commit()
    return specimens


@pytest.fixture
def indexed_bergkristall(store: dc.Store, sidecar: FtsSidecar) -> Specimen:
    """Commit one German-prose specimen into a store with the sidecar attached."""
    store.attach(sidecar)
    specimen = Specimen(catalog_no="DC-001", label="Bergkristall", notes=BERGKRISTALL_NOTES)
    store.root = [specimen]
    store.commit()
    return specimen


@pytest.fixture
def tombstone(store: dc.Store, sidecar: FtsSidecar) -> dict:
    """Index azurite, detach the sidecar, and hand-craft the delete that carries azurite's prior.

    v0.x emits no deletes (spec §3.1, reserved), so the tombstone is built by hand.
    """
    store.attach(sidecar)
    specimen = Specimen(catalog_no="DC-001", label="Azurit", notes=AZURIT_NOTES)
    store.root = [specimen]
    store.commit()
    oid = _oid(specimen)
    prior = store._backend.load_many([oid])[oid].payload
    store.detach(sidecar)
    cid = _cid_of(sidecar, type_info(Specimen).typename)
    return {
        "f": "datacrystal-delta",
        "v": 2,
        "tid": sidecar.watermark + 1,
        "actor": 0,
        "at": 0,
        "ops": [{"op": "delete", "oid": oid, "cid": cid, "payload": None, "prior": prior}],
        "types": [],
        "root": None,
    }


@pytest.fixture
def persisted_fts(tmp_path: Path, store_factory: Callable[[], dc.Store]) -> tuple[str, int]:
    """Index one specimen, close sidecar and store, return the sidecar file and its watermark."""
    fts_path = str(tmp_path / "fts.sqlite")
    store = store_factory()
    persisted = FtsSidecar(fts_path, fulltext=_fulltext_config())
    store.attach(persisted)
    store.root = [
        Specimen(
            catalog_no="DC-001", label="Bergkristall", notes="sechseckige Prismen aus den Alpen"
        )
    ]
    store.commit()
    watermark = persisted.watermark
    persisted.close()
    store.close()
    return fts_path, watermark


@pytest.fixture
def reopened_sidecar(persisted_fts: tuple[str, int]) -> Iterator[FtsSidecar]:
    """Reopen the persisted sidecar file and close it afterwards."""
    fts_path, _ = persisted_fts
    reopened = FtsSidecar(fts_path, fulltext=_fulltext_config())
    yield reopened
    reopened.close()


@pytest.fixture
def reopened_specimen(persisted_fts: tuple[str, int], store: dc.Store) -> Specimen:
    """Load the persisted specimen from the reopened store."""
    specimen = store.get(Specimen, catalog_no="DC-001")
    if specimen is None:
        raise LookupError("DC-001 was not persisted")
    return specimen


@pytest.fixture
def crash_sidecar(tmp_path: Path) -> Iterator[_CrashMidApply]:
    """Open a crash-injecting sidecar over the conformance-kit stream type."""
    crashing = _CrashMidApply(
        str(tmp_path / "fts.sqlite"), fulltext={dct.STREAM_TYPENAME: ["notes"]}
    )
    yield crashing
    crashing.close()


@pytest.fixture
def two_upserts_delta() -> dict:
    """Build one delta that upserts two kit records."""
    return {
        "f": "datacrystal-delta",
        "v": 2,
        "tid": 1,
        "actor": 0,
        "at": 0,
        "root": None,
        "types": [[1, dct.STREAM_TYPENAME, list(dct.STREAM_FIELDS)]],
        "ops": [_kit_upsert(4096, "klare Prismen"), _kit_upsert(4097, "tiefblauer Azurit")],
    }


@pytest.fixture
def crashed_sidecar(crash_sidecar: _CrashMidApply, two_upserts_delta: dict) -> _CrashMidApply:
    """Return the sidecar after a crash mid-apply of the two-upsert delta, disarmed again."""
    crash_sidecar.armed = True
    with pytest.raises(OSError, match="injected crash"):
        crash_sidecar.apply(two_upserts_delta)
    crash_sidecar.armed = False
    return crash_sidecar


@pytest.fixture
def sidecar_factory(tmp_path: Path) -> Callable[[], FtsSidecar]:
    """Return a factory for fresh sidecars, each on its own SQLite file."""
    paths = (str(tmp_path / f"kit-{number}.sqlite") for number in range(1_000_000))

    def build() -> FtsSidecar:
        return FtsSidecar(next(paths), fulltext={dct.STREAM_TYPENAME: ["notes"]})

    return build


@pytest.fixture
def corpus_factory(
    tmp_path: Path,
) -> Iterator[Callable[[int], tuple[dc.Store, FtsSidecar, list[Specimen]]]]:
    """Return a factory for a committed, indexed corpus of a given size; close all afterwards."""
    opened: list[tuple[dc.Store, FtsSidecar]] = []

    def build(corpus_size: int) -> tuple[dc.Store, FtsSidecar, list[Specimen]]:
        store = dc.Store._from_backend(MemoryBackend())
        indexed = FtsSidecar(str(tmp_path / f"size-{corpus_size}.sqlite"), fulltext=_fulltext_config())
        store.attach(indexed)
        specimens = [
            Specimen(
                catalog_no=f"DC-{corpus_size}-{i}",
                label=f"specimen {i}",
                notes=f"Stufe {i} mit Kristallen",
            )
            for i in range(corpus_size)
        ]
        store.root = specimens
        store.commit()
        opened.append((store, indexed))
        return store, indexed, specimens

    yield build
    for store, indexed in opened:
        indexed.close()
        store.close()


def test_sidecar_follows_the_store_watermark_after_a_commit(
    store: dc.Store, sidecar: FtsSidecar
) -> None:
    """Given a store with the sidecar attached
    When three specimens are committed
    Then the sidecar's watermark is the store's last transaction id
    """
    store.attach(sidecar)
    store.root = _cabinet()

    store.commit()

    assert sidecar.watermark == store.last_tid


@pytest.mark.parametrize(
    ("query", "position"),
    [
        ("Prismen", 0),
        ("Tsumeb", 1),
        ("quartz", 2),
        ("prismen", 0),
        ("tiefblauen", 1),
        ("Tiefblauen", 1),
        ("Kristallen", 1),
    ],
)
def test_search_finds_the_specimen_whose_prose_has_the_term(
    query: str, position: int, indexed_cabinet: list[Specimen], sidecar: FtsSidecar
) -> None:
    """Given German and English prose committed live to a store with the sidecar attached
    When the cabinet is searched for a term of one specimen, in any letter case
    Then the hit is exactly that specimen
    """
    hits = sidecar.search(query)

    assert hits == {_oid(indexed_cabinet[position])}


def test_search_does_not_stem_the_singular_to_the_indexed_plural(
    indexed_cabinet: list[Specimen], sidecar: FtsSidecar
) -> None:
    """Given azurite's notes indexed with the plural Kristallen
    When the cabinet is searched for the singular Kristall
    Then nothing is found, the pinned limitation of stock unicode61
    """
    hits = sidecar.search("Kristall")

    assert hits == set()


@pytest.mark.parametrize("query", ["Prismen", "Alpen"])
def test_update_unindexes_old_prose_using_only_the_prior(
    query: str, indexed_bergkristall: Specimen, store: dc.Store, sidecar: FtsSidecar
) -> None:
    """Given a specimen whose German notes are indexed
    When its notes are replaced and committed
    Then the old prose no longer matches, though the sidecar never read the store
    """
    indexed_bergkristall.notes = SOLD_NOTES

    store.commit()

    assert sidecar.search(query) == set()


@pytest.mark.parametrize("query", ["Sammler", "münchen", "MUNCHEN"])
def test_update_indexes_the_new_prose_with_case_and_diacritic_folding(
    query: str, indexed_bergkristall: Specimen, store: dc.Store, sidecar: FtsSidecar
) -> None:
    """Given a specimen whose German notes are indexed
    When its notes are replaced by prose with an umlaut and committed
    Then the new prose matches the term, folded for case and diacritic
    """
    indexed_bergkristall.notes = SOLD_NOTES

    store.commit()

    assert sidecar.search(query) == {_oid(indexed_bergkristall)}


def test_search_does_not_fold_the_ue_transliteration_to_the_umlaut(
    indexed_bergkristall: Specimen, store: dc.Store, sidecar: FtsSidecar
) -> None:
    """Given a specimen whose updated notes contain München
    When the cabinet is searched for Muenchen
    Then nothing is found, the pinned limitation (Snowball german2 handles it in the extra)
    """
    indexed_bergkristall.notes = SOLD_NOTES
    store.commit()

    hits = sidecar.search("Muenchen")

    assert hits == set()


def test_delete_tombstone_unindexes_via_prior(sidecar: FtsSidecar, tombstone: dict) -> None:
    """Given an indexed azurite and a hand-crafted tombstone carrying its prior
    When the sidecar applies the tombstone
    Then it is accepted
    And no search finds azurite's prose
    And no term stays indexed
    """
    accepted = sidecar.apply(tombstone)

    assert accepted is True
    assert sidecar.search("Tsumeb") == set()
    assert sidecar.terms() == set()


def test_delete_tombstone_without_a_prior_is_a_format_error(
    sidecar: FtsSidecar, tombstone: dict
) -> None:
    """Given an indexed azurite and a tombstone whose prior is None
    When the sidecar applies it
    Then it is refused as carrying no prior, never guessed
    """
    naked = {**tombstone, "ops": [{**tombstone["ops"][0], "prior": None}]}

    with pytest.raises(DeltaFormatError, match="no prior"):
        sidecar.apply(naked)


def test_reopened_sidecar_resumes_at_its_persisted_watermark(
    persisted_fts: tuple[str, int], store: dc.Store
) -> None:
    """Given a sidecar file closed after indexing one commit, and the store reopened
    When the sidecar is reopened
    Then its watermark is the one it had when closed
    And it equals the store's last transaction id
    """
    fts_path, watermark_when_closed = persisted_fts

    reopened = FtsSidecar(fts_path, fulltext=_fulltext_config())
    reopened_watermark = reopened.watermark
    reopened.close()

    assert reopened_watermark == watermark_when_closed
    assert reopened_watermark == store.last_tid


def test_reopened_sidecar_unindexes_an_update_after_reattaching(
    reopened_sidecar: FtsSidecar, reopened_specimen: Specimen, store: dc.Store
) -> None:
    """Given a sidecar reopened at the store's watermark and attached without a rebuild
    When a specimen's notes are replaced and committed
    Then the old prose no longer matches
    """
    store.attach(reopened_sidecar)
    reopened_specimen.notes = SOLD_NOTES

    store.commit()

    assert reopened_sidecar.search("Prismen") == set()


def test_reopened_sidecar_indexes_an_update_after_reattaching(
    reopened_sidecar: FtsSidecar, reopened_specimen: Specimen, store: dc.Store
) -> None:
    """Given a sidecar reopened at the store's watermark and attached without a rebuild
    When a specimen's notes are replaced and committed
    Then the new prose matches the specimen
    """
    store.attach(reopened_sidecar)
    reopened_specimen.notes = SOLD_NOTES

    store.commit()

    assert reopened_sidecar.search("Sammler") == {_oid(reopened_specimen)}


def test_crash_mid_apply_rolls_back_the_whole_delta(crashed_sidecar: _CrashMidApply) -> None:
    """Given a sidecar that crashed after the first op of a two-upsert delta
    When its state is read
    Then the watermark is still 0
    And no term is indexed
    """
    assert crashed_sidecar.watermark == 0
    assert crashed_sidecar.terms() == set()


def test_crash_mid_apply_replays_the_same_delta_clean(
    crashed_sidecar: _CrashMidApply, two_upserts_delta: dict
) -> None:
    """Given a sidecar that crashed after the first op of a two-upsert delta
    When the same delta is applied again
    Then it is accepted
    And both records are searchable
    """
    accepted = crashed_sidecar.apply(two_upserts_delta)

    assert accepted is True
    assert crashed_sidecar.search("Prismen") == {4096}
    assert crashed_sidecar.search("Azurit") == {4097}


def test_fts_sidecar_passes_the_prior_unindex_section(
    sidecar_factory: Callable[[], FtsSidecar],
) -> None:
    """Given the spike consumer, certified with a reader of its indexed terms
    When the conformance kit runs
    Then the prior un-index section ran
    """
    ran = dct.check_delta_consumer(sidecar_factory, content=_terms_of)

    assert "§3 prior un-index" in ran


def test_fts_sidecar_passes_the_delete_totality_section(
    sidecar_factory: Callable[[], FtsSidecar],
) -> None:
    """Given the spike consumer, certified with a reader of its indexed terms
    When the conformance kit runs
    Then the delete totality section ran
    """
    ran = dct.check_delta_consumer(sidecar_factory, content=_terms_of)

    assert "§3.1 delete totality" in ran


def test_apply_cost_is_the_same_for_a_small_and_a_large_corpus(
    corpus_factory: Callable[[int], tuple[dc.Store, FtsSidecar, list[Specimen]]],
) -> None:
    """Given two indexed corpora of 10 and of 200 specimens
    When one specimen's notes are replaced in each and committed
    Then the delta costs the sidecar the same number of statements in both
    """
    small_store, small_sidecar, small = corpus_factory(10)
    large_store, large_sidecar, large = corpus_factory(200)
    small[0].notes = "Verkauft nach München"
    large[0].notes = "Verkauft nach München"

    small_store.commit()
    large_store.commit()

    assert large_sidecar.statements == small_sidecar.statements


def test_apply_cost_of_one_update_is_at_most_eight_statements(
    corpus_factory: Callable[[int], tuple[dc.Store, FtsSidecar, list[Specimen]]],
) -> None:
    """Given an indexed corpus of 200 specimens
    When one specimen's notes are replaced and committed
    Then the delta costs the sidecar at most 8 statements, which is O(delta) not O(corpus)
    """
    store, indexed, specimens = corpus_factory(200)
    specimens[0].notes = "Verkauft nach München"

    store.commit()

    assert indexed.statements <= 8
