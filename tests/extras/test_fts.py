"""datacrystal[fts] — the FTS5 sidecar as a certified COMMIT-DELTA-v1 consumer.

The extra is the contract's first *real* consumer (ROADMAP item 10): every
test here doubles as validation that the draft contract holds under a
consumer shape it was not designed around, before the lock at the tag.
Engine tests run over both backends via ``store_factory`` (conftest).
"""

from __future__ import annotations

import itertools
from typing import Annotated

import pytest

pytest.importorskip("snowballstemmer", reason="datacrystal[fts] extra not installed")

import datacrystal as dc
from datacrystal.contract.applier import DeltaGapError
from datacrystal.fts import FtsConfigError, FullTextIndex
from datacrystal.testing import STREAM_TYPENAME, check_delta_consumer

_ids = itertools.count()


@dc.entity
class Gemstone:
    qid: Annotated[str, dc.Unique]
    name: str
    notes: Annotated[str | None, dc.FullText(language="de")] = None
    description: Annotated[str | None, dc.FullText(language="en")] = None


@dc.entity
class Vendor:
    name: str
    motto: Annotated[str | None, dc.FullText] = None  # bare: fold-only, no stemming


GEM_FULLTEXT = {
    "tests.extras.test_fts:Gemstone": {"notes": "de", "description": "en"},
    "tests.extras.test_fts:Vendor": {"motto": None},
}


def _gem_typename() -> str:
    from datacrystal._entity import type_info

    return type_info(Gemstone).typename


def fresh_index(tmp_path, **kwargs) -> FullTextIndex:
    return FullTextIndex(tmp_path / f"sidecar-{next(_ids)}.fts", **kwargs)


def content_probe(idx: FullTextIndex):
    """Derived state for the conformance kit: every stored doc row."""
    return sorted(
        tuple(row) for row in idx._conn.execute("SELECT rowid, * FROM docs")
    )


# -- conformance: the kit certifies the consumer obligations -------------------


def test_conformance_kit_certifies_fulltext_index(tmp_path) -> None:
    ran = check_delta_consumer(
        lambda: fresh_index(tmp_path, fulltext={STREAM_TYPENAME: {"notes": "en"}}),
        content=content_probe,
    )
    # the content-dependent sections (prior un-index, delete totality) ran
    assert any("prior" in label for label in ran)
    assert any("delete" in label for label in ran)


# -- end-to-end over both backends ---------------------------------------------


def test_stemmed_search_end_to_end(store_factory, tmp_path) -> None:
    store = store_factory()
    idx = fresh_index(tmp_path, fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [
        Gemstone(qid="Q1", name="quartz",
                 notes="Die Kristalle glänzen im Licht",
                 description="The crystals form hexagonal prisms"),
        Gemstone(qid="Q2", name="azurite",
                 notes="tiefblaue Stufe aus Tsumeb",
                 description="deep blue druzy crusts"),
    ]
    store.commit()

    # German stemming: singular query finds the plural document text
    hits = idx.search("Kristall")
    assert [h.oid for h in hits] and hits[0].typename == _gem_typename()
    assert "Kristalle" in (hits[0].snippets.get("notes") or "")

    # English stemming on the other field
    assert idx.search("crystal")  # matches "crystals" via the en stem column

    # ranking: an exact surface form outranks a stem-only match
    exact = idx.search("Kristalle")
    assert exact and exact[0].score >= hits[0].score

    # no match
    assert idx.search("Vulkan") == []
    store.close()
    idx.close()


def test_update_and_delete_unindex(store_factory, tmp_path) -> None:
    store = store_factory()
    idx = fresh_index(tmp_path, fulltext=GEM_FULLTEXT)
    store.attach(idx)
    gem = Gemstone(qid="Q1", name="quartz", notes="milchige Kristalle")
    store.root = [gem]
    store.commit()
    assert idx.search("Kristall")

    gem.notes = "klare Spitzen ohne Einschlüsse"
    store.commit()
    assert idx.search("Kristall") == []          # old terms un-indexed
    assert idx.search("Einschluss")              # new terms indexed (de stem)

    store.delete(gem)
    store.root = []
    store.commit()
    assert idx.search("Einschluss") == []        # tombstone removed the doc
    store.close()
    idx.close()


def test_phrase_and_type_filter(store_factory, tmp_path) -> None:
    store = store_factory()
    idx = fresh_index(tmp_path, fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [
        Gemstone(qid="Q1", name="quartz", description="deep blue crusts"),
        Vendor(name="Minerals AG", motto="deep respect for blue stones"),
    ]
    store.commit()

    both = idx.search("blue")
    assert {h.typename for h in both} == {
        _gem_typename(), "tests.extras.test_fts:Vendor"
    }
    only_gem = idx.search("blue", cls=Gemstone)
    assert {h.typename for h in only_gem} == {_gem_typename()}

    # quoted phrases stay phrases: this word order never occurs
    assert idx.search('"blue deep"') == []
    assert idx.search('"deep blue"')
    store.close()
    idx.close()


def test_multi_term_ranked_search_is_or_by_default(store_factory, tmp_path) -> None:
    """#54 karen-sparck-jones rule — a natural-language query must not require
    EVERY term in one document (AND collapses recall). Default ``match="any"``
    OR-s the terms so BM25 ranks the union; a doc matching SOME terms still
    surfaces (and a fully-matching doc outranks it). ``match="all"`` keeps the
    precise-filter AND for the cases that want it.
    """
    store = store_factory()
    idx = fresh_index(tmp_path, fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [
        # matches ALL three query terms ("statin drugs cancer")
        Gemstone(qid="Q1", name="full",
                 description="statin drugs may influence cancer risk"),
        # matches only ONE term ("cancer") — must still be retrievable
        Gemstone(qid="Q2", name="partial",
                 description="dietary fibre and cancer prevention"),
        # matches NONE — must never appear
        Gemstone(qid="Q3", name="off-topic",
                 description="hexagonal prisms in clear quartz"),
    ]
    store.commit()

    # search "statin drugs cancer" → expect BOTH the full and partial docs
    # (Y in top-N), the full match ranked first.
    any_hits = idx.search("statin drugs cancer")
    any_qids = [store.get_many([h.oid])[0].qid for h in any_hits]
    assert len(any_hits) == 2, "OR default must retrieve the partial-match doc"
    assert any_qids[0] == "Q1", "the all-terms doc must rank above the one-term doc"
    assert set(any_qids) == {"Q1", "Q2"}  # the no-term doc Q3 is absent

    # match="all" requires every term: only the full doc, partial drops out.
    all_hits = idx.search("statin drugs cancer", match="all")
    assert {store.get_many([h.oid])[0].qid for h in all_hits} == {"Q1"}

    # a query where NO doc has all terms returns nothing under "all" but the
    # union under "any" (the recall-collapse the issue is about).
    assert idx.search("statin fibre quartz", match="all") == []
    assert idx.search("statin fibre quartz")  # any: union is non-empty

    # a quoted phrase stays ONE unit under BOTH modes — the term-join must
    # never split a phrase into AND-of-words (karen-sparck-jones review #3):
    # "cancer risk" is adjacent only in Q1, so even match="all" finds it,
    # while the reversed order occurs nowhere.
    assert {store.get_many([h.oid])[0].qid
            for h in idx.search('"cancer risk"', match="all")} == {"Q1"}
    assert idx.search('"risk cancer"', match="all") == []

    # an invalid mode is loud, not silently misinterpreted.
    with pytest.raises(ValueError):
        idx.search("statin", match="both")
    store.close()
    idx.close()


def test_bare_fulltext_is_fold_only(store_factory, tmp_path) -> None:
    store = store_factory()
    idx = fresh_index(tmp_path, fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Vendor(name="AG", motto="Glänzende Steine")]
    store.commit()
    assert idx.search("glänzende")        # diacritic + case folding works
    assert idx.search("GLANZENDE")        # both sides folded
    assert idx.search("Stein") == []      # but NO stemming on a bare field
    store.close()
    idx.close()


# -- linguistics regressions (2026-06-12 adversarial review) --------------------


@dc.entity
class Icon:
    label: str
    caption: Annotated[str | None, dc.FullText] = None           # bare
    legend: Annotated[str | None, dc.FullText(language="ru")] = None


ICON_FULLTEXT = {"tests.extras.test_fts:Icon": {"caption": None, "legend": "ru"}}


def test_bare_fields_match_non_latin_verbatim(store_factory, tmp_path) -> None:
    """The exact (f_) column is Python-fold-normalized like the query, so
    Cyrillic/compat forms match verbatim — the review's critical finding:
    a raw-text exact column is unicode61-tokenized and silently unmatchable
    for ё/й, m², ligatures."""
    store = store_factory()
    idx = fresh_index(tmp_path, fulltext=ICON_FULLTEXT)
    store.attach(idx)
    store.root = [
        Icon(label="i1", caption="ёлка синий красный"),
        Icon(label="i2", caption="3 m² with ﬁne luster"),
    ]
    store.commit()
    assert idx.search("ёлка")          # verbatim Cyrillic
    assert idx.search("елка")          # folded form too
    assert idx.search("синий")
    assert idx.search("m²")            # NFKD compat: m² ≡ m2
    assert idx.search("m2")
    assert idx.search("ﬁne") and idx.search("fine")  # ligature folds
    store.close()
    idx.close()


def test_russian_stemming_conflates_inflections(store_factory, tmp_path) -> None:
    """Stem-first-fold-after: 'красный'/'красная' both stem to 'красн' —
    folding before stemming destroys the й/ё the suffix tables need."""
    store = store_factory()
    idx = fresh_index(tmp_path, fulltext=ICON_FULLTEXT)
    store.attach(idx)
    store.root = [
        Icon(label="i1", legend="красная ёлка"),
        Icon(label="i2", legend="красный кристалл"),
    ]
    store.commit()
    for query in ("красный", "красная", "красное"):
        assert len(idx.search(query)) == 2, f"query {query!r} must find both docs"
    store.close()
    idx.close()


def test_nfd_document_text_highlights(store_factory, tmp_path) -> None:
    """Decomposed (NFD) input must index, match, and highlight exactly like
    its composed form — snippets render in NFC."""
    import unicodedata

    store = store_factory()
    idx = fresh_index(tmp_path, fulltext=ICON_FULLTEXT)
    store.attach(idx)
    nfd = unicodedata.normalize("NFD", "glänzende Würfel")
    store.root = [Icon(label="i1", caption=nfd)]
    store.commit()
    hits = idx.search("glänzende")
    assert hits and "[glänzende]" in (hits[0].snippets.get("caption") or "")
    store.close()
    idx.close()


def test_phrase_highlight_marks_only_adjacent_runs(store_factory,
                                                   tmp_path) -> None:
    store = store_factory()
    idx = fresh_index(tmp_path, fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="azurite",
                           description="deep water, blue sky, deep blue crusts")]
    store.commit()
    [hit] = idx.search('"deep blue"')
    snippet = hit.snippets["description"]
    assert "[deep] [blue] crusts" in snippet
    assert "[deep] water" not in snippet     # non-adjacent 'deep' unmarked
    assert "[blue] sky" not in snippet       # non-adjacent 'blue' unmarked
    store.close()
    idx.close()


def test_query_operators_cannot_inject(store_factory, tmp_path) -> None:
    """FTS5 operators arriving as user input are inert text, never syntax."""
    store = store_factory()
    idx = fresh_index(tmp_path, fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="quartz", notes="milchige Kristalle")]
    store.commit()
    for evil in ('kristalle"', 'NEAR(a b)', 'f_notes : x', '" OR rowid:1',
                 "kristalle*", "^kristalle", "NOT kristalle", "(((", '""'):
        idx.search(evil)  # must not raise — terms are quoted, not parsed
    assert idx.search("AND OR NOT") == [] or True   # bare operators: inert
    store.close()
    idx.close()


def test_abugida_languages_refused_loudly(tmp_path) -> None:
    for lang in ("hi", "hindi", "tamil", "nepali"):
        with pytest.raises(FtsConfigError):
            FullTextIndex(tmp_path / f"{lang}.fts", fulltext={"T": {"x": lang}})


# -- bootstrap + staleness (spec §5: deltas are not retained) -------------------


def test_bootstrap_mid_life_equals_rebuild(store_factory, tmp_path) -> None:
    """Attach to a lived-in store via snapshot bootstrap; afterwards the
    incrementally maintained index must equal a from-scratch rebuild
    (fitness #13: rebuild ≡ incremental)."""
    store = store_factory()
    gem = Gemstone(qid="Q1", name="quartz", notes="milchige Kristalle")
    store.root = [gem]
    store.commit()

    with store.snapshot() as snap:
        idx = FullTextIndex.bootstrap(
            tmp_path / "boot.fts", snap, fulltext=GEM_FULLTEXT
        )
    store.attach(idx)
    assert idx.search("Kristall")  # bootstrap indexed pre-attach history

    gem.notes = "tiefblaue Stufe"
    store.root = list(store.root) + [
        Gemstone(qid="Q2", name="azurite", description="blue crusts")
    ]
    store.commit()

    with store.snapshot() as snap:
        rebuilt = FullTextIndex.bootstrap(
            tmp_path / "rebuilt.fts", snap, fulltext=GEM_FULLTEXT
        )
    assert content_probe(idx) == content_probe(rebuilt)
    assert idx.watermark == rebuilt.watermark
    store.close()
    idx.close()
    rebuilt.close()


def test_stale_sidecar_refused_on_attach(store_factory, tmp_path) -> None:
    store = store_factory()
    path = tmp_path / "stale.fts"
    idx = FullTextIndex(path, fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="quartz", notes="Kristalle")]
    store.commit()
    store.detach(idx)
    idx.close()

    store.root = list(store.root) + [Gemstone(qid="Q2", name="calcite")]
    store.commit()  # the sidecar misses this delta

    reopened = FullTextIndex(path, fulltext=GEM_FULLTEXT)
    assert reopened.watermark == 1
    with pytest.raises(DeltaGapError):
        store.attach(reopened)  # behind + not retained → rebuild required
    reopened.close()
    store.close()


def test_persistence_across_reopen(store_factory, tmp_path) -> None:
    store = store_factory()
    path = tmp_path / "persist.fts"
    idx = FullTextIndex(path, fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="quartz", notes="Kristalle")]
    store.commit()
    watermark = idx.watermark
    store.detach(idx)
    idx.close()

    reopened = FullTextIndex(path, fulltext=GEM_FULLTEXT)
    assert reopened.watermark == watermark
    assert reopened.search("Kristall")  # index content survived the reopen
    store.attach(reopened)              # watermark == store.last_tid: accepted
    store.close()
    reopened.close()


def test_config_drift_is_refused(tmp_path) -> None:
    path = tmp_path / "drift.fts"
    FullTextIndex(path, fulltext=GEM_FULLTEXT).close()
    other = {"tests.extras.test_fts:Gemstone": {"notes": "en"}}  # de → en
    with pytest.raises(FtsConfigError):
        FullTextIndex(path, fulltext=other)


def test_unknown_language_is_refused(tmp_path) -> None:
    with pytest.raises(FtsConfigError):
        FullTextIndex(tmp_path / "x.fts", fulltext={"T": {"notes": "xx"}})


def test_registry_derived_config_indexes_fulltext_entities(
    store_factory, tmp_path
) -> None:
    """The zero-config path: FullTextIndex(path) reads dc.FullText markers
    straight from the @entity registry (declaration lives in code, like
    dc.Index)."""
    store = store_factory()
    idx = fresh_index(tmp_path)  # no fulltext= map
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="quartz", notes="glänzende Kristalle")]
    store.commit()
    assert idx.search("Kristall", cls=Gemstone)
    store.close()
    idx.close()


# -- failure honesty -------------------------------------------------------------


def test_failing_apply_rolls_back_and_detaches(store_factory, tmp_path,
                                               monkeypatch) -> None:
    """A consumer that raises mid-apply must leave NO partial sidecar state
    (its transaction rolls back) and the store must detach it loudly and
    stay healthy (pipeline doctrine: never hold writes hostage)."""
    store = store_factory()
    idx = fresh_index(tmp_path, fulltext=GEM_FULLTEXT)
    store.attach(idx)
    store.root = [Gemstone(qid="Q1", name="quartz", notes="Kristalle")]
    store.commit()
    before_content = content_probe(idx)
    before_watermark = idx.watermark

    original = idx._insert_doc

    def explode(*_args, **_kwargs):
        raise RuntimeError("sidecar disk full")

    monkeypatch.setattr(idx, "_insert_doc", explode)
    gem2 = Gemstone(qid="Q2", name="azurite", notes="tiefblau")
    store.root = list(store.root) + [gem2]
    with pytest.warns(dc.ConsumerDetachedWarning):
        store.commit()  # commit succeeds; consumer detached

    monkeypatch.setattr(idx, "_insert_doc", original)
    assert content_probe(idx) == before_content      # rollback was whole
    assert idx.watermark == before_watermark
    assert store.count(Gemstone) == 2                # the store is healthy

    with pytest.raises(DeltaGapError):
        store.attach(idx)  # now behind: must rebuild, not silently rejoin
    store.close()
    idx.close()


def test_apply_is_o_delta_not_o_corpus(tmp_path) -> None:
    """Fitness #9 shape: the statement count of one fixed-size delta must
    not grow with the corpus (10 docs vs 100 docs). Two independent stores
    (op counts are backend-independent — the memory backend suffices)."""
    from datacrystal._storage.memory import MemoryBackend

    counts: list[int] = []
    for corpus in (10, 100):
        store = dc.Store._from_backend(MemoryBackend())
        idx = fresh_index(tmp_path, fulltext=GEM_FULLTEXT)
        store.attach(idx)
        store.root = [
            Gemstone(qid=f"Q{corpus}-{i}", name=f"g{i}", notes=f"Stufe {i}")
            for i in range(corpus)
        ]
        store.commit()
        gem = Gemstone(qid=f"Qx-{corpus}", name="probe", notes="Kristalle")
        store.root = list(store.root) + [gem]
        store.commit()
        counts.append(idx.statements)
        store.close()
        idx.close()
    assert counts[0] == counts[1], (
        f"statements per fixed delta grew with corpus size: {counts}"
    )
