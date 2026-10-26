"""datacrystal[fts] — the FTS5 sidecar as a certified COMMIT-DELTA-v1 consumer.

The extra is the contract's first *real* consumer (ROADMAP item 10): every
test here doubles as validation that the draft contract holds under a
consumer shape it was not designed around, before the lock at the tag.
Engine tests run over both backends via ``store_factory`` (conftest).
"""

from __future__ import annotations

import itertools
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Annotated, NamedTuple

import pytest

pytest.importorskip("snowballstemmer", reason="datacrystal[fts] extra not installed")

import datacrystal as dc
from datacrystal import Snapshot
from datacrystal.contract.applier import DeltaGapError
from datacrystal.fts import FtsConfigError, FullTextIndex
from datacrystal.testing import STREAM_TYPENAME, check_delta_consumer

_ids = itertools.count()
FullTextMap = Mapping[str, Mapping[str, str | None]]


@dc.entity
class Gemstone:
    """A gemstone with German notes and an English description."""

    qid: Annotated[str, dc.Unique]
    name: str
    notes: Annotated[str | None, dc.FullText(language="de")] = None
    description: Annotated[str | None, dc.FullText(language="en")] = None


@dc.entity
class Vendor:
    """A vendor whose motto is indexed bare: fold-only, no stemming."""

    name: str
    motto: Annotated[str | None, dc.FullText] = None


GEM_FULLTEXT = {
    "tests.extras.test_fts:Gemstone": {"notes": "de", "description": "en"},
    "tests.extras.test_fts:Vendor": {"motto": None},
}
GEM_TYPENAME = "tests.extras.test_fts:Gemstone"
VENDOR_TYPENAME = "tests.extras.test_fts:Vendor"


def content_probe(idx: FullTextIndex) -> object:
    """Return the derived state for the conformance kit: every stored doc row."""
    return sorted(tuple(row) for row in idx._conn.execute("SELECT rowid, * FROM docs"))


class IndexShop:
    """Opens full-text indexes under one directory and closes every one of them afterwards."""

    def __init__(self, directory: Path) -> None:
        """Remember the directory the indexes live in."""
        self.directory = directory
        self.opened: list[FullTextIndex] = []

    def open(self, name: str | None = None, fulltext: FullTextMap | None = None) -> FullTextIndex:
        """Open an index file by name, or a fresh one, over the given or the registry config."""
        index = FullTextIndex(self.directory / (name or f"sidecar-{next(_ids)}.fts"), fulltext=fulltext)
        self.opened.append(index)
        return index

    def bootstrap(
        self, name: str, snapshot: Snapshot, fulltext: FullTextMap | None = None
    ) -> FullTextIndex:
        """Build an index file from a snapshot."""
        index = FullTextIndex.bootstrap(self.directory / name, snapshot, fulltext=fulltext)
        self.opened.append(index)
        return index

    def close_all(self) -> None:
        """Close every index this shop opened."""
        for index in self.opened:
            index.close()


@pytest.fixture
def shop(tmp_path: Path) -> Iterator[IndexShop]:
    """Provide an index shop under tmp_path and close its indexes afterwards."""
    index_shop = IndexShop(tmp_path)
    yield index_shop
    index_shop.close_all()


@pytest.fixture
def stream_index_factory(shop: IndexShop) -> Callable[[], FullTextIndex]:
    """Return a factory for fresh indexes over the conformance-kit stream type."""

    def build() -> FullTextIndex:
        return shop.open(fulltext={STREAM_TYPENAME: {"notes": "en"}})

    return build


# -- conformance: the kit certifies the consumer obligations -------------------


def test_conformance_kit_runs_the_prior_unindex_section_for_the_fulltext_index(
    stream_index_factory: Callable[[], FullTextIndex],
) -> None:
    """Given the full-text index over the kit's stream type
    When the conformance kit certifies it
    Then the content-dependent prior un-index section ran
    """
    ran = check_delta_consumer(stream_index_factory, content=content_probe)

    assert any("prior" in label for label in ran)


def test_conformance_kit_runs_the_delete_section_for_the_fulltext_index(
    stream_index_factory: Callable[[], FullTextIndex],
) -> None:
    """Given the full-text index over the kit's stream type
    When the conformance kit certifies it
    Then the content-dependent delete totality section ran
    """
    ran = check_delta_consumer(stream_index_factory, content=content_probe)

    assert any("delete" in label for label in ran)


# -- end-to-end over both backends ---------------------------------------------


@pytest.fixture
def indexed_quartz_and_azurite(store: dc.Store, shop: IndexShop) -> FullTextIndex:
    """Commit a German-and-English quartz and azurite into a store with an index attached."""
    idx = shop.open(fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [
        Gemstone(
            qid="Q1",
            name="quartz",
            notes="Die Kristalle glänzen im Licht",
            description="The crystals form hexagonal prisms",
        ),
        Gemstone(
            qid="Q2",
            name="azurite",
            notes="tiefblaue Stufe aus Tsumeb",
            description="deep blue druzy crusts",
        ),
    ]
    store.commit()
    return idx


def test_german_stemming_finds_the_plural_for_a_singular_query(
    indexed_quartz_and_azurite: FullTextIndex,
) -> None:
    """Given quartz whose German notes say Kristalle
    When the index is searched for the singular Kristall
    Then exactly one document is found
    """
    hits = indexed_quartz_and_azurite.search("Kristall")

    assert len(hits) == 1


def test_stemmed_hit_names_the_gemstone_type(indexed_quartz_and_azurite: FullTextIndex) -> None:
    """Given quartz whose German notes say Kristalle
    When the index is searched for the singular Kristall
    Then the first hit is of the Gemstone type
    """
    hits = indexed_quartz_and_azurite.search("Kristall")

    assert hits[0].typename == GEM_TYPENAME


def test_stemmed_hit_snippet_shows_the_original_surface_form(
    indexed_quartz_and_azurite: FullTextIndex,
) -> None:
    """Given quartz whose German notes say Kristalle
    When the index is searched for the singular Kristall
    Then the notes snippet contains Kristalle
    """
    hits = indexed_quartz_and_azurite.search("Kristall")

    assert "Kristalle" in (hits[0].snippets.get("notes") or "")


def test_english_stemming_finds_the_plural_for_a_singular_query(
    indexed_quartz_and_azurite: FullTextIndex,
) -> None:
    """Given quartz whose English description says crystals
    When the index is searched for the singular crystal
    Then a document is found through the English stem column
    """
    hits = indexed_quartz_and_azurite.search("crystal")

    assert hits != []


def test_exact_surface_form_outranks_a_stem_only_match(
    indexed_quartz_and_azurite: FullTextIndex,
) -> None:
    """Given quartz whose German notes say Kristalle
    When the index is searched for the exact Kristalle and for the stem-only Kristall
    Then the exact form scores at least as high
    """
    stem_hits = indexed_quartz_and_azurite.search("Kristall")
    exact_hits = indexed_quartz_and_azurite.search("Kristalle")

    assert exact_hits[0].score >= stem_hits[0].score


def test_search_for_an_absent_word_finds_nothing(indexed_quartz_and_azurite: FullTextIndex) -> None:
    """Given two indexed gemstones that never mention a volcano
    When the index is searched for Vulkan
    Then nothing is found
    """
    hits = indexed_quartz_and_azurite.search("Vulkan")

    assert hits == []


@pytest.fixture
def indexed_milky_quartz(store: dc.Store, shop: IndexShop) -> tuple[Gemstone, FullTextIndex]:
    """Commit a quartz with milchige Kristalle into a store with an index attached."""
    idx = shop.open(fulltext=GEM_FULLTEXT)
    store.attach(idx)
    gem = Gemstone(qid="Q1", name="quartz", notes="milchige Kristalle")
    store.root = [gem]
    store.commit()
    return gem, idx


def test_committed_prose_is_found_by_its_stem(
    indexed_milky_quartz: tuple[Gemstone, FullTextIndex],
) -> None:
    """Given a committed quartz with milchige Kristalle
    When the index is searched for Kristall
    Then a document is found
    """
    _, idx = indexed_milky_quartz

    hits = idx.search("Kristall")

    assert hits != []


def test_update_unindexes_the_old_terms(
    store: dc.Store, indexed_milky_quartz: tuple[Gemstone, FullTextIndex]
) -> None:
    """Given a committed quartz with milchige Kristalle
    When its notes are replaced and committed
    Then the old term Kristall finds nothing
    """
    gem, idx = indexed_milky_quartz
    gem.notes = "klare Spitzen ohne Einschlüsse"

    store.commit()

    assert idx.search("Kristall") == []


def test_update_indexes_the_new_terms_by_their_german_stem(
    store: dc.Store, indexed_milky_quartz: tuple[Gemstone, FullTextIndex]
) -> None:
    """Given a committed quartz with milchige Kristalle
    When its notes are replaced by prose with Einschlüsse and committed
    Then the stem Einschluss finds a document
    """
    gem, idx = indexed_milky_quartz
    gem.notes = "klare Spitzen ohne Einschlüsse"

    store.commit()

    assert idx.search("Einschluss") != []


def test_delete_tombstone_removes_the_document(
    store: dc.Store, indexed_milky_quartz: tuple[Gemstone, FullTextIndex]
) -> None:
    """Given a quartz whose notes were replaced by prose with Einschlüsse
    When the quartz is deleted and committed
    Then the stem Einschluss finds nothing
    """
    gem, idx = indexed_milky_quartz
    gem.notes = "klare Spitzen ohne Einschlüsse"
    store.commit()
    store.delete(gem)
    store.root = []

    store.commit()

    assert idx.search("Einschluss") == []


@pytest.fixture
def blue_gem_and_vendor(store: dc.Store, shop: IndexShop) -> FullTextIndex:
    """Commit a gemstone and a vendor that both mention blue into a store with an index."""
    idx = shop.open(fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [
        Gemstone(qid="Q1", name="quartz", description="deep blue crusts"),
        Vendor(name="Minerals AG", motto="deep respect for blue stones"),
    ]
    store.commit()
    return idx


def test_search_spans_every_indexed_type(blue_gem_and_vendor: FullTextIndex) -> None:
    """Given a gemstone and a vendor that both mention blue
    When the index is searched for blue
    Then the hits are of both types
    """
    hits = blue_gem_and_vendor.search("blue")

    assert {hit.typename for hit in hits} == {GEM_TYPENAME, VENDOR_TYPENAME}


def test_type_filter_restricts_the_hits_to_one_type(blue_gem_and_vendor: FullTextIndex) -> None:
    """Given a gemstone and a vendor that both mention blue
    When the index is searched for blue with cls=Gemstone
    Then the hits are of the Gemstone type alone
    """
    hits = blue_gem_and_vendor.search("blue", cls=Gemstone)

    assert {hit.typename for hit in hits} == {GEM_TYPENAME}


def test_quoted_phrase_in_the_wrong_word_order_finds_nothing(
    blue_gem_and_vendor: FullTextIndex,
) -> None:
    """Given prose that says deep blue and never blue deep
    When the index is searched for the quoted phrase "blue deep"
    Then nothing is found
    """
    hits = blue_gem_and_vendor.search('"blue deep"')

    assert hits == []


def test_quoted_phrase_in_the_right_word_order_is_found(blue_gem_and_vendor: FullTextIndex) -> None:
    """Given prose that says deep blue
    When the index is searched for the quoted phrase "deep blue"
    Then a document is found
    """
    hits = blue_gem_and_vendor.search('"deep blue"')

    assert hits != []


class StatinWorld(NamedTuple):
    """The three-document corpus of the multi-term tests, with the qid of every oid."""

    index: FullTextIndex
    qid_by_oid: dict[int, str]


@pytest.fixture
def statin_world(store: dc.Store, shop: IndexShop) -> StatinWorld:
    """Commit a full-match, a one-term-match and an off-topic document with an index attached."""
    idx = shop.open(fulltext=GEM_FULLTEXT)
    store.attach(idx)
    full = Gemstone(
        qid="Q1", name="full", description="statin drugs may influence cancer risk"
    )
    partial = Gemstone(qid="Q2", name="partial", description="dietary fibre and cancer prevention")
    off_topic = Gemstone(qid="Q3", name="off-topic", description="hexagonal prisms in clear quartz")
    store.root = [full, partial, off_topic]
    store.commit()
    qid_by_oid = {store.store(gem): gem.qid for gem in (full, partial, off_topic)}
    return StatinWorld(idx, qid_by_oid)


def test_multi_term_search_retrieves_the_partial_match_by_default(statin_world: StatinWorld) -> None:
    """Given a document with all of statin drugs cancer and one with only cancer
    When the index is searched for "statin drugs cancer" with the default match
    Then both documents are retrieved
    """
    hits = statin_world.index.search("statin drugs cancer")

    assert len(hits) == 2, "OR default must retrieve the partial-match doc"


def test_multi_term_search_ranks_the_all_terms_document_first(statin_world: StatinWorld) -> None:
    """Given a document with all of statin drugs cancer and one with only cancer
    When the index is searched for "statin drugs cancer" with the default match
    Then the all-terms document ranks above the one-term document
    """
    hits = statin_world.index.search("statin drugs cancer")

    assert statin_world.qid_by_oid[hits[0].oid] == "Q1"


def test_multi_term_search_never_retrieves_the_off_topic_document(
    statin_world: StatinWorld,
) -> None:
    """Given a document with none of the terms statin drugs cancer
    When the index is searched for "statin drugs cancer" with the default match
    Then only the full and the partial document are retrieved
    """
    hits = statin_world.index.search("statin drugs cancer")

    assert {statin_world.qid_by_oid[hit.oid] for hit in hits} == {"Q1", "Q2"}


def test_match_all_requires_every_term(statin_world: StatinWorld) -> None:
    """Given a document with all of statin drugs cancer and one with only cancer
    When the index is searched for "statin drugs cancer" with match="all"
    Then only the full document is retrieved
    """
    hits = statin_world.index.search("statin drugs cancer", match="all")

    assert {statin_world.qid_by_oid[hit.oid] for hit in hits} == {"Q1"}


def test_match_all_finds_nothing_when_no_document_has_every_term(statin_world: StatinWorld) -> None:
    """Given no document that has all of statin, fibre and quartz
    When the index is searched for "statin fibre quartz" with match="all"
    Then nothing is found
    """
    hits = statin_world.index.search("statin fibre quartz", match="all")

    assert hits == []


def test_match_any_returns_the_union_when_no_document_has_every_term(
    statin_world: StatinWorld,
) -> None:
    """Given no document that has all of statin, fibre and quartz
    When the index is searched for "statin fibre quartz" with the default match
    Then the union of the partial matches is non-empty
    """
    hits = statin_world.index.search("statin fibre quartz")

    assert hits != []


def test_quoted_phrase_stays_one_unit_under_match_all(statin_world: StatinWorld) -> None:
    """Given "cancer risk" adjacent only in the full document
    When the index is searched for the quoted phrase "cancer risk" with match="all"
    Then only the full document is retrieved
    """
    hits = statin_world.index.search('"cancer risk"', match="all")

    assert {statin_world.qid_by_oid[hit.oid] for hit in hits} == {"Q1"}


def test_reversed_quoted_phrase_finds_nothing_under_match_all(statin_world: StatinWorld) -> None:
    """Given "cancer risk" adjacent only in the full document
    When the index is searched for the reversed phrase "risk cancer" with match="all"
    Then nothing is found
    """
    hits = statin_world.index.search('"risk cancer"', match="all")

    assert hits == []


def test_invalid_match_mode_is_refused_loudly(statin_world: StatinWorld) -> None:
    """Given an index over the statin corpus
    When it is searched with match="both"
    Then the mode is refused with ValueError, not silently misinterpreted
    """
    with pytest.raises(ValueError, match="match"):
        statin_world.index.search("statin", match="both")


@pytest.fixture
def indexed_vendor(store: dc.Store, shop: IndexShop) -> FullTextIndex:
    """Commit a vendor whose bare motto is Glänzende Steine into a store with an index."""
    idx = shop.open(fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Vendor(name="AG", motto="Glänzende Steine")]
    store.commit()
    return idx


@pytest.mark.parametrize("query", ["glänzende", "GLANZENDE"])
def test_bare_fulltext_folds_diacritics_and_case_on_both_sides(
    query: str, indexed_vendor: FullTextIndex
) -> None:
    """Given a bare dc.FullText motto Glänzende Steine
    When the index is searched for a differently cased or accented form
    Then the document is found
    """
    hits = indexed_vendor.search(query)

    assert hits != []


def test_bare_fulltext_does_not_stem(indexed_vendor: FullTextIndex) -> None:
    """Given a bare dc.FullText motto Glänzende Steine
    When the index is searched for the stem-only Stein
    Then nothing is found
    """
    hits = indexed_vendor.search("Stein")

    assert hits == []


@pytest.fixture
def indexed_vendor_from_registry(store: dc.Store, shop: IndexShop) -> FullTextIndex:
    """Commit a vendor whose bare motto is Glänzende Steine into an index without config."""
    idx = shop.open()
    store.attach(idx)
    store.root = [Vendor(name="AG", motto="Glänzende Steine")]
    store.commit()
    return idx


def test_zero_config_index_does_not_stem_a_bare_field(
    store: dc.Store, indexed_vendor_from_registry: FullTextIndex,
) -> None:
    """Given an index derived from the registry, where Vendor.motto is a bare dc.FullText
    And the motto Glänzende Steine is committed
    When the index is searched for the stem Stein through a root snapshot
    Then nothing is found
    """
    with store.snapshot(principal=ROOT) as snap:
        hits = indexed_vendor_from_registry.search("Stein", cls=Vendor, snapshot=snap)

    assert hits == []


def test_zero_config_index_folds_case_and_accent_of_a_bare_field(
    store: dc.Store, indexed_vendor_from_registry: FullTextIndex,
) -> None:
    """Given an index derived from the registry, where Vendor.motto is a bare dc.FullText
    And the motto Glänzende Steine is committed
    When the index is searched for GLANZENDE through a root snapshot
    Then the vendor is found
    """
    with store.snapshot(principal=ROOT) as snap:
        hits = indexed_vendor_from_registry.search("GLANZENDE", cls=Vendor, snapshot=snap)

    assert [hit.typename for hit in hits] == [VENDOR_TYPENAME]


# -- linguistics regressions (2026-06-12 adversarial review) --------------------


@dc.entity
class Icon:
    """An icon with a bare caption and a Russian legend."""

    label: str
    caption: Annotated[str | None, dc.FullText] = None
    legend: Annotated[str | None, dc.FullText(language="ru")] = None


ICON_FULLTEXT = {"tests.extras.test_fts:Icon": {"caption": None, "legend": "ru"}}


@pytest.fixture
def indexed_icons(store: dc.Store, shop: IndexShop) -> FullTextIndex:
    """Commit two icons with non-Latin and compatibility-form captions, with an index attached."""
    idx = shop.open(fulltext=ICON_FULLTEXT)
    store.attach(idx)
    store.root = [
        Icon(label="i1", caption="ёлка синий красный"),
        Icon(label="i2", caption="3 m² with ﬁne luster"),
    ]
    store.commit()
    return idx


@pytest.mark.parametrize("query", ["ёлка", "елка", "синий", "m²", "m2", "ﬁne", "fine"])
def test_bare_field_matches_non_latin_and_compat_forms_verbatim(
    query: str, indexed_icons: FullTextIndex
) -> None:
    """Given bare captions with Cyrillic ё and й, m² and the ligature ﬁ
    When the index is searched for the verbatim or the folded form
    Then a document is found, as the exact column is Python-fold-normalized like the query
    """
    hits = indexed_icons.search(query)

    assert hits != []


@pytest.fixture
def indexed_red_icons(store: dc.Store, shop: IndexShop) -> FullTextIndex:
    """Commit two icons with inflected Russian legends, with an index attached."""
    idx = shop.open(fulltext=ICON_FULLTEXT)
    store.attach(idx)
    store.root = [
        Icon(label="i1", legend="красная ёлка"),
        Icon(label="i2", legend="красный кристалл"),
    ]
    store.commit()
    return idx


@pytest.mark.parametrize("query", ["красный", "красная", "красное"])
def test_russian_stemming_conflates_inflections(query: str, indexed_red_icons: FullTextIndex) -> None:
    """Given the legends красная ёлка and красный кристалл, stemmed before they are folded
    When the index is searched for one inflection of красн
    Then both documents are found
    """
    hits = indexed_red_icons.search(query)

    assert len(hits) == 2, f"query {query!r} must find both docs"


def test_nfd_document_text_highlights_like_its_composed_form(
    store: dc.Store, shop: IndexShop
) -> None:
    """Given a caption indexed from decomposed (NFD) text
    When the index is searched for the composed word glänzende
    Then the snippet marks the word in NFC
    """
    idx = shop.open(fulltext=ICON_FULLTEXT)
    store.attach(idx)
    store.root = [Icon(label="i1", caption=unicodedata.normalize("NFD", "glänzende Würfel"))]
    store.commit()

    [hit] = idx.search("glänzende")

    assert "[glänzende]" in (hit.snippets.get("caption") or "")


@pytest.fixture
def indexed_deep_blue(store: dc.Store, shop: IndexShop) -> FullTextIndex:
    """Commit one description with scattered and adjacent deep and blue, with an index."""
    idx = shop.open(fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [
        Gemstone(
            qid="Q1", name="azurite", description="deep water, blue sky, deep blue crusts"
        )
    ]
    store.commit()
    return idx


def test_phrase_highlight_marks_the_adjacent_run(indexed_deep_blue: FullTextIndex) -> None:
    """Given a description with scattered and adjacent deep and blue
    When the index is searched for the quoted phrase "deep blue"
    Then the adjacent run is marked
    """
    [hit] = indexed_deep_blue.search('"deep blue"')

    assert "[deep] [blue] crusts" in hit.snippets["description"]


def test_phrase_highlight_leaves_a_non_adjacent_deep_unmarked(
    indexed_deep_blue: FullTextIndex,
) -> None:
    """Given a description with scattered and adjacent deep and blue
    When the index is searched for the quoted phrase "deep blue"
    Then the non-adjacent deep stays unmarked
    """
    [hit] = indexed_deep_blue.search('"deep blue"')

    assert "[deep] water" not in hit.snippets["description"]


def test_phrase_highlight_leaves_a_non_adjacent_blue_unmarked(
    indexed_deep_blue: FullTextIndex,
) -> None:
    """Given a description with scattered and adjacent deep and blue
    When the index is searched for the quoted phrase "deep blue"
    Then the non-adjacent blue stays unmarked
    """
    [hit] = indexed_deep_blue.search('"deep blue"')

    assert "[blue] sky" not in hit.snippets["description"]


@pytest.mark.parametrize(
    "evil",
    [
        'kristalle"',
        "NEAR(a b)",
        "f_notes : x",
        '" OR rowid:1',
        "kristalle*",
        "^kristalle",
        "NOT kristalle",
        "(((",
        '""',
        "AND OR NOT",
    ],
)
def test_fts5_operators_in_a_query_are_inert_text(
    evil: str, store: dc.Store, shop: IndexShop
) -> None:
    """Given an index over milchige Kristalle
    When a query made of FTS5 operator syntax is searched
    Then it returns a hit list instead of raising, as terms are quoted, not parsed
    """
    idx = shop.open(fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="quartz", notes="milchige Kristalle")]
    store.commit()

    hits = idx.search(evil)

    assert isinstance(hits, list)


@pytest.mark.parametrize("language", ["hi", "hindi", "tamil", "nepali"])
def test_abugida_language_is_refused_loudly(language: str, shop: IndexShop) -> None:
    """Given an abugida language that Snowball cannot stem
    When an index is configured with it
    Then the configuration is refused
    """
    with pytest.raises(FtsConfigError):
        shop.open(fulltext={"T": {"x": language}})


# -- bootstrap + staleness (spec §5: deltas are not retained) -------------------


def test_bootstrap_indexes_the_history_before_attach(
    store: dc.Store, shop: IndexShop
) -> None:
    """Given a lived-in store whose quartz has milchige Kristalle
    When an index is bootstrapped from a snapshot
    Then the pre-attach history is searchable
    """
    store.root = [Gemstone(qid="Q1", name="quartz", notes="milchige Kristalle")]
    store.commit()

    with store.snapshot() as snap:
        idx = shop.bootstrap("boot.fts", snap, fulltext=GEM_FULLTEXT)

    assert idx.search("Kristall") != []


@pytest.fixture
def incremental_index(store: dc.Store, shop: IndexShop) -> FullTextIndex:
    """Bootstrap an index on a lived-in store, attach it and commit an update and an addition."""
    gem = Gemstone(qid="Q1", name="quartz", notes="milchige Kristalle")
    store.root = [gem]
    store.commit()
    with store.snapshot() as snap:
        idx = shop.bootstrap("boot.fts", snap, fulltext=GEM_FULLTEXT)
    store.attach(idx)
    gem.notes = "tiefblaue Stufe"
    store.root = [*store.root, Gemstone(qid="Q2", name="azurite", description="blue crusts")]
    store.commit()
    return idx


def test_incremental_index_equals_a_rebuild_from_scratch(
    store: dc.Store, shop: IndexShop, incremental_index: FullTextIndex
) -> None:
    """Given an index maintained incrementally after a bootstrap
    When the store is rebuilt into a second index from a snapshot (fitness #13)
    Then both indexes hold the same rows
    """
    with store.snapshot() as snap:
        rebuilt = shop.bootstrap("rebuilt.fts", snap, fulltext=GEM_FULLTEXT)

    assert content_probe(incremental_index) == content_probe(rebuilt)


def test_incremental_index_ends_at_the_rebuilt_watermark(
    store: dc.Store, shop: IndexShop, incremental_index: FullTextIndex
) -> None:
    """Given an index maintained incrementally after a bootstrap
    When the store is rebuilt into a second index from a snapshot (fitness #13)
    Then both indexes stand at the same watermark
    """
    with store.snapshot() as snap:
        rebuilt = shop.bootstrap("rebuilt.fts", snap, fulltext=GEM_FULLTEXT)

    assert incremental_index.watermark == rebuilt.watermark


@pytest.fixture
def store_ahead_of_closed_index(store: dc.Store, shop: IndexShop) -> dc.Store:
    """Index one commit, detach and close the index, then commit a second one it misses."""
    idx = shop.open("stale.fts", fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="quartz", notes="Kristalle")]
    store.commit()
    store.detach(idx)
    idx.close()
    store.root = [*store.root, Gemstone(qid="Q2", name="calcite")]
    store.commit()
    return store


def test_stale_sidecar_reopens_at_its_old_watermark(
    store_ahead_of_closed_index: dc.Store, shop: IndexShop
) -> None:
    """Given an index that missed the store's second delta
    When it is reopened
    Then its watermark is still 1
    """
    reopened = shop.open("stale.fts", fulltext=GEM_FULLTEXT)

    assert reopened.watermark == 1


def test_stale_sidecar_is_refused_on_attach(
    store_ahead_of_closed_index: dc.Store, shop: IndexShop
) -> None:
    """Given an index that missed the store's second delta, which is not retained
    When it is attached
    Then the attach is refused as a gap, to be rebuilt
    """
    reopened = shop.open("stale.fts", fulltext=GEM_FULLTEXT)

    with pytest.raises(DeltaGapError):
        store_ahead_of_closed_index.attach(reopened)


@pytest.fixture
def closed_index_watermark(store: dc.Store, shop: IndexShop) -> int:
    """Index one commit, detach and close the index, and return its watermark."""
    idx = shop.open("persist.fts", fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="quartz", notes="Kristalle")]
    store.commit()
    watermark = idx.watermark
    store.detach(idx)
    idx.close()
    return watermark


def test_reopened_index_keeps_its_watermark(closed_index_watermark: int, shop: IndexShop) -> None:
    """Given an index closed after one commit
    When it is reopened
    Then its watermark is the one it had when closed
    """
    reopened = shop.open("persist.fts", fulltext=GEM_FULLTEXT)

    assert reopened.watermark == closed_index_watermark


def test_reopened_index_keeps_its_content(closed_index_watermark: int, shop: IndexShop) -> None:
    """Given an index closed after one commit
    When it is reopened and searched for Kristall
    Then the content survived the reopen
    """
    reopened = shop.open("persist.fts", fulltext=GEM_FULLTEXT)

    hits = reopened.search("Kristall")

    assert hits != []


def test_reopened_index_is_accepted_at_the_store_watermark_and_follows_on(
    store: dc.Store, closed_index_watermark: int, shop: IndexShop
) -> None:
    """Given an index reopened at the store's last transaction id
    When it is attached and a further gemstone is committed
    Then the new gemstone is searchable
    """
    reopened = shop.open("persist.fts", fulltext=GEM_FULLTEXT)
    store.attach(reopened)
    store.root = [*store.root, Gemstone(qid="Q2", name="calcite", notes="Spitzen")]

    store.commit()

    assert reopened.search("Spitze") != []


def test_config_drift_is_refused(shop: IndexShop) -> None:
    """Given an index file created with German notes
    When it is reopened with notes configured as English
    Then the drift is refused
    """
    shop.open("drift.fts", fulltext=GEM_FULLTEXT)

    with pytest.raises(FtsConfigError):
        shop.open("drift.fts", fulltext={"tests.extras.test_fts:Gemstone": {"notes": "en"}})


def test_unknown_language_is_refused(shop: IndexShop) -> None:
    """Given a language code Snowball does not know
    When an index is configured with it
    Then the configuration is refused
    """
    with pytest.raises(FtsConfigError):
        shop.open(fulltext={"T": {"notes": "xx"}})


def test_registry_derived_config_indexes_fulltext_entities(
    store: dc.Store, shop: IndexShop
) -> None:
    """Given a zero-config index that reads dc.FullText markers from the @entity registry
    When a root snapshot searches Kristall within Gemstone
    Then the hit is of the Gemstone type
    """
    idx = shop.open()
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="quartz", notes="glänzende Kristalle")]
    store.commit()

    with store.snapshot(principal=ROOT) as snap:
        hits = idx.search("Kristall", cls=Gemstone, snapshot=snap)

    assert [hit.typename for hit in hits] == [GEM_TYPENAME]


# -- failure honesty -------------------------------------------------------------


def explode(*_args: object, **_kwargs: object) -> None:
    """Fail like a full sidecar disk."""
    raise RuntimeError("sidecar disk full")


@pytest.fixture
def indexed_one_gem(store: dc.Store, shop: IndexShop) -> FullTextIndex:
    """Commit one gemstone into a store with an index attached."""
    idx = shop.open(fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="quartz", notes="Kristalle")]
    store.commit()
    return idx


@pytest.fixture
def content_before_failure(indexed_one_gem: FullTextIndex) -> object:
    """Return the index rows before a failing commit."""
    return content_probe(indexed_one_gem)


@pytest.fixture
def failing_inserts(indexed_one_gem: FullTextIndex, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the index raise while inserting a document."""
    monkeypatch.setattr(indexed_one_gem, "_insert_doc", explode)


@pytest.fixture
def failed_commit(
    store: dc.Store, indexed_one_gem: FullTextIndex, content_before_failure: object, failing_inserts: None
) -> FullTextIndex:
    """Commit a second gemstone while the index fails mid-apply, and return the index."""
    store.root = [*store.root, Gemstone(qid="Q2", name="azurite", notes="tiefblau")]
    with pytest.warns(dc.ConsumerDetachedWarning):
        store.commit()
    return indexed_one_gem


def test_failing_consumer_is_detached_with_a_warning_while_the_commit_succeeds(
    store: dc.Store, indexed_one_gem: FullTextIndex, failing_inserts: None
) -> None:
    """Given an index whose document insert raises mid-apply
    When a second gemstone is committed
    Then the commit succeeds and warns that the consumer was detached
    """
    store.root = [*store.root, Gemstone(qid="Q2", name="azurite", notes="tiefblau")]

    with pytest.warns(dc.ConsumerDetachedWarning):
        store.commit()


def test_failing_apply_rolls_the_sidecar_content_back_whole(
    failed_commit: FullTextIndex, content_before_failure: object
) -> None:
    """Given an index that failed mid-apply of a second gemstone
    When its rows are read
    Then they equal the rows before the failing commit
    """
    assert content_probe(failed_commit) == content_before_failure


def test_failing_apply_leaves_the_sidecar_watermark_untouched(
    failed_commit: FullTextIndex,
) -> None:
    """Given an index that failed mid-apply of its second delta
    When its watermark is read
    Then it is still 1
    """
    assert failed_commit.watermark == 1


def test_store_stays_healthy_after_a_consumer_failed(
    store: dc.Store, failed_commit: FullTextIndex
) -> None:
    """Given an index that failed mid-apply and was detached
    When the store counts its gemstones
    Then both committed gemstones are there
    """
    counted = store.count(Gemstone)

    assert counted == 2


def test_failed_sidecar_cannot_silently_rejoin(store: dc.Store, failed_commit: FullTextIndex) -> None:
    """Given an index that failed mid-apply and is now behind the store
    When it is attached again
    Then the attach is refused as a gap: rebuild, do not silently rejoin
    """
    with pytest.raises(DeltaGapError):
        store.attach(failed_commit)


@pytest.fixture
def indexed_corpus(
    shop: IndexShop, tmp_path: Path
) -> Iterator[Callable[[int], tuple[dc.Store, FullTextIndex]]]:
    """Return a factory for a committed, indexed corpus; close the stores after."""
    stores: list[dc.Store] = []

    def build(size: int) -> tuple[dc.Store, FullTextIndex]:
        corpus_store = dc.Store.open(tmp_path / f"corpus-{size}", durability="never")
        stores.append(corpus_store)
        idx = shop.open(fulltext=GEM_FULLTEXT)
        corpus_store.attach(idx)
        corpus_store.root = [
            Gemstone(qid=f"Q{size}-{i}", name=f"g{i}", notes=f"Stufe {i}") for i in range(size)
        ]
        corpus_store.commit()
        return corpus_store, idx

    yield build
    for corpus_store in stores:
        corpus_store.close()


def test_apply_cost_does_not_grow_with_the_corpus(
    indexed_corpus: Callable[[int], tuple[dc.Store, FullTextIndex]],
) -> None:
    """Given two independent stores indexing 10 and 100 gemstones
    When one fixed-size gemstone is committed into each
    Then the sidecar runs the same number of statements for both (fitness #9)
    """
    small_store, small_index = indexed_corpus(10)
    large_store, large_index = indexed_corpus(100)
    small_store.root = [*small_store.root, Gemstone(qid="Qx-10", name="probe", notes="Kristalle")]
    large_store.root = [*large_store.root, Gemstone(qid="Qx-100", name="probe", notes="Kristalle")]

    small_store.commit()
    large_store.commit()

    assert large_index.statements == small_index.statements


# -- W4-5: FTS post-filter over protected classes (ADR-008 R13) ----------------

TEAM = 5
OWNER = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
MEMBER = dc.Principal(uid=3, memberships={TEAM: dc.AGENT})
OUTSIDER = dc.Principal(uid=4, memberships={99: dc.CURATOR})
ROOT = dc.root_principal(1)


@dc.entity(protected=True)
class Secret:
    """A protected record with an indexed English body."""

    label: Annotated[str, dc.Unique]
    body: Annotated[str | None, dc.FullText(language="en")] = None


SECRET_FULLTEXT = {"tests.extras.test_fts:Secret": {"body": "en"}}


class SecretsWorld(NamedTuple):
    """An index over two crystal secrets: one team-shared, one owner-only."""

    index: FullTextIndex
    shared_oid: int
    secret_oid: int


@pytest.fixture
def secrets_world(store: dc.Store, shop: IndexShop) -> SecretsWorld:
    """Commit one member-readable and one owner-only secret, both matching crystal."""
    idx = shop.open(fulltext=SECRET_FULLTEXT)
    store.attach(idx)
    with store.acting_as(OWNER):
        shared = Secret(label="shared", body="the crystal lattice diagram")
        shared_oid = store.store(shared)
        dc.share(shared, TEAM, read=dc.AGENT, write=dc.CURATOR)
        secret = Secret(label="secret", body="the crystal vault combination")
        secret_oid = store.store(secret)
        store.commit()
    return SecretsWorld(idx, shared_oid, secret_oid)


def test_search_without_a_snapshot_over_a_protected_class_is_refused(shop: IndexShop) -> None:
    """Given an index covering a protected class
    When it is searched with no snapshot
    Then the search is refused (R12), rather than read with full visibility
    """
    idx = shop.open(fulltext=SECRET_FULLTEXT)

    with pytest.raises(dc.ReadDeniedError):
        idx.search("crystal")


@pytest.fixture
def indexed_gem_without_protection(store: dc.Store, shop: IndexShop) -> FullTextIndex:
    """Commit one gemstone into a store whose index covers only unprotected classes."""
    idx = shop.open(fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="quartz", notes="milchige Kristalle")]
    store.commit()
    return idx


def test_search_without_a_snapshot_over_unprotected_classes_still_finds_the_hit(
    indexed_gem_without_protection: FullTextIndex,
) -> None:
    """Given an index covering only unprotected classes
    When it is searched with no snapshot
    Then it does not raise and finds the document
    """
    hits = indexed_gem_without_protection.search("Kristall")

    assert len(hits) == 1


def test_search_without_a_snapshot_over_unprotected_classes_renders_sidecar_snippets(
    indexed_gem_without_protection: FullTextIndex,
) -> None:
    """Given an index covering only unprotected classes
    When it is searched with no snapshot
    Then the snippet is rendered from the sidecar
    """
    hits = indexed_gem_without_protection.search("Kristall")

    assert "Kristalle" in (hits[0].snippets.get("notes") or "")


def test_post_filter_leaves_a_team_member_only_the_shared_hit(
    store: dc.Store, secrets_world: SecretsWorld
) -> None:
    """Given a team-shared and an owner-only secret that both match crystal
    When a team member searches through a snapshot
    Then only the shared secret is a hit
    """
    with store.snapshot(principal=MEMBER) as snap:
        hits = secrets_world.index.search("crystal", snapshot=snap)

    assert {hit.oid for hit in hits} == {secrets_world.shared_oid}


def test_post_filter_leaves_an_outsider_no_hit(store: dc.Store, secrets_world: SecretsWorld) -> None:
    """Given a team-shared and an owner-only secret that both match crystal
    When an outsider searches through a snapshot
    Then no hit is left
    """
    with store.snapshot(principal=OUTSIDER) as snap:
        hits = secrets_world.index.search("crystal", snapshot=snap)

    assert hits == []


def test_post_filter_leaves_root_both_hits(store: dc.Store, secrets_world: SecretsWorld) -> None:
    """Given a team-shared and an owner-only secret that both match crystal
    When root searches through a snapshot
    Then both secrets are hits, as root filters nothing
    """
    with store.snapshot(principal=ROOT) as snap:
        hits = secrets_world.index.search("crystal", snapshot=snap)

    assert {hit.oid for hit in hits} == {secrets_world.shared_oid, secrets_world.secret_oid}


def test_snippets_render_the_survivor_and_never_the_denied_text(
    store: dc.Store, secrets_world: SecretsWorld
) -> None:
    """Given a team-shared and an owner-only secret that both match crystal
    When a team member searches through a snapshot
    Then exactly one hit survives, the shared secret
    And its snippet renders the survivor's own text
    And the denied row's text never appears
    """
    with store.snapshot(principal=MEMBER) as snap:
        hits = secrets_world.index.search("crystal", snapshot=snap)

    assert len(hits) == 1
    assert hits[0].oid == secrets_world.shared_oid
    assert "lattice" in " ".join(hits[0].snippets.values())
    assert "vault" not in " ".join(hits[0].snippets.values())


class CrowdedSecrets(NamedTuple):
    """An index over many top-ranked owner-only secrets and one lower-ranked shared one."""

    index: FullTextIndex
    shared_oid: int


@pytest.fixture
def crowded_secrets(store: dc.Store, shop: IndexShop) -> CrowdedSecrets:
    """Commit 8 short owner-only secrets above one longer team-shared secret."""
    idx = shop.open(fulltext=SECRET_FULLTEXT)
    store.attach(idx)
    with store.acting_as(OWNER):
        for i in range(8):
            store.store(Secret(label=f"d{i}", body="crystal"))
        shared = Secret(label="shared", body="crystal " + " ".join(f"w{j}" for j in range(40)))
        shared_oid = store.store(shared)
        dc.share(shared, TEAM, read=dc.AGENT, write=dc.CURATOR)
        store.commit()
    return CrowdedSecrets(idx, shared_oid)


def test_over_fetch_fills_the_page_past_denied_top_hits(
    store: dc.Store, crowded_secrets: CrowdedSecrets
) -> None:
    """Given 8 denied hits ranked above the one readable hit
    When a team member searches with limit=1
    Then the readable hit fills the page, as the over-fetch widens past the denied window
    """
    with store.snapshot(principal=MEMBER) as snap:
        hits = crowded_secrets.index.search("crystal", snapshot=snap, limit=1)

    assert [hit.oid for hit in hits] == [crowded_secrets.shared_oid]


class GetManySpy:
    """Wraps a snapshot's get_many and records every ref it was asked for."""

    def __init__(self, real: Callable[..., list]) -> None:
        """Wrap the real get_many."""
        self.real = real
        self.seen: list[int] = []

    def __call__(self, refs: list[int]) -> list:
        """Record the refs and delegate."""
        batch = list(refs)
        self.seen.extend(batch)
        return self.real(batch)


@pytest.fixture
def fifty_denied_secrets(store: dc.Store, shop: IndexShop) -> FullTextIndex:
    """Commit 50 owner-only secrets that all match crystal."""
    idx = shop.open(fulltext=SECRET_FULLTEXT)
    store.attach(idx)
    with store.acting_as(OWNER):
        for i in range(50):
            store.store(Secret(label=f"d{i}", body="crystal"))
        store.commit()
    return idx


@pytest.fixture
def spied_outsider_snapshot(store: dc.Store) -> Iterator[tuple[Snapshot, GetManySpy]]:
    """Open an outsider's snapshot whose get_many is spied on."""
    with store.snapshot(principal=OUTSIDER) as snap:
        spy = GetManySpy(snap.get_many)
        snap.get_many = spy  # pyrefly: ignore[bad-assignment]
        yield snap, spy


def test_all_denied_search_returns_no_hit(
    fifty_denied_secrets: FullTextIndex, spied_outsider_snapshot: tuple[Snapshot, GetManySpy]
) -> None:
    """Given 50 secrets an outsider may not read
    When the outsider searches crystal with scan_cap=10 and limit=5
    Then no hit is returned
    """
    snap, _ = spied_outsider_snapshot

    hits = fifty_denied_secrets.search("crystal", snapshot=snap, scan_cap=10, limit=5)

    assert hits == []


def test_scan_cap_bounds_the_candidates_an_all_denied_search_examines(
    fifty_denied_secrets: FullTextIndex, spied_outsider_snapshot: tuple[Snapshot, GetManySpy]
) -> None:
    """Given 50 secrets an outsider may not read
    When the outsider searches crystal with scan_cap=10 and limit=5
    Then at most 10 candidates were examined, not the 50-row table
    And at least one was examined
    """
    snap, spy = spied_outsider_snapshot

    fifty_denied_secrets.search("crystal", snapshot=snap, scan_cap=10, limit=5)

    assert len(spy.seen) <= 10
    assert len(spy.seen) > 0


@pytest.fixture
def two_committed_secrets(store: dc.Store) -> None:
    """Commit two owner-only secrets that both match crystal."""
    with store.acting_as(OWNER):
        store.store(Secret(label="s1", body="crystal one"))
        store.store(Secret(label="s2", body="crystal vault two"))
        store.commit()


@pytest.mark.usefixtures("two_committed_secrets")
def test_protected_bootstrap_from_a_non_root_snapshot_is_refused(
    store: dc.Store, shop: IndexShop
) -> None:
    """Given two committed secrets of a protected class
    When an index is bootstrapped from the owner's non-root snapshot
    Then it is refused, as it would build a silently partial mirror
    """
    with store.snapshot(principal=OWNER) as snap, pytest.raises(dc.ReadDeniedError):
        shop.bootstrap("b.fts", snap, fulltext=SECRET_FULLTEXT)


@pytest.mark.usefixtures("two_committed_secrets")
def test_protected_bootstrap_from_a_root_snapshot_mirrors_every_secret(
    store: dc.Store, shop: IndexShop
) -> None:
    """Given two committed secrets of a protected class
    When an index is bootstrapped from a root snapshot and searched through another
    Then both secrets are hits
    """
    with store.snapshot(principal=ROOT) as snap:
        idx = shop.bootstrap("b2.fts", snap, fulltext=SECRET_FULLTEXT)
    with store.snapshot(principal=ROOT) as snap:
        hits = idx.search("crystal", snapshot=snap)

    assert len(hits) == 2
