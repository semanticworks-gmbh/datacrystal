"""W3-1 oracle: the read fence's two label sources vs a literal, independent transcription of
the ADR-008 access predicate (epic #168, issue #173).

Hypothesis builds a labeled corpus — a mix of freshly protected rows with
explicit (owner, groups, read_floor) labels and LEGACY rows (committed before the class
turned ``protected=True``, decoding under the ADR-008 R7 fill) — plus principals spanning
every ADR-008 category (anonymous, uid-only, single/multi-membership, owner-of-a-row, root).
The corpus, the principals and the oracle live in ``_labeled_corpus.py``. For a principal
and a row, three sources must agree, all read through the public API:

1. discovery: the rows ``store.query`` returns under ``acting_as(principal)``, which read the
   index postings
2. dereference: the rows ``store.get_many([oid])`` returns as the real record rather than a
   ``dc.Redacted`` twin, which read the record labels
3. ``oracle_can_read`` — a LITERAL transcription of the ADR-008 Context predicate block,
   written independently of ``_permissions.py`` so a shared bug in the library code cannot
   cancel out against the oracle.

Discovery and dereference must also agree after an on-disk index-cache reopen (the deferred
``_rebuild_last_values`` / #12 path); the cache-provenance guard reads the class index itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import datacrystal as dc
from datacrystal._entity import type_info
from datacrystal._indexes import ClassIndexes
from tests.property._labeled_corpus import (
    ANONYMOUS,
    OUTSIDE_ROOT,
    ROOT,
    Corpus,
    Labels,
    labeled_corpus,
    labeled_rows,
    names_dereferenced,
    names_of,
    owned_rows,
    principals,
    protected_rock,
    readable_names_by_oracle,
    seed_labels,
)

corpora = {
    "n_legacy": st.integers(min_value=0, max_value=3),
    "labeled": st.lists(labeled_rows(), min_size=1, max_size=6),
}


# --- discovery: the index postings, read through query and count ---------------------


@settings(deadline=None, max_examples=40)
@given(**corpora)
def test_corpus_extent_holds_exactly_the_stored_rows(
    n_legacy: int, labeled: list[dict[str, Any]]
) -> None:
    """Given a corpus of legacy rows and rows with explicit labels
    When root counts the rock rows
    Then it counts exactly the rows the corpus stored
    """
    with labeled_corpus(n_legacy, labeled) as corpus, corpus.store.acting_as(ROOT):
        counted = corpus.store.count(corpus.rock)

        assert counted == len(corpus.oracle)


@settings(deadline=None, max_examples=40)
@given(principal=principals, **corpora)
def test_query_discovers_exactly_the_rows_the_literal_oracle_allows(
    n_legacy: int, labeled: list[dict[str, Any]], principal: dc.Principal
) -> None:
    """Given a corpus of legacy rows and rows with explicit labels, and any principal
    When the principal queries the rock rows
    Then it discovers the rows the ADR-008 oracle lets the principal read
    """
    with labeled_corpus(n_legacy, labeled) as corpus:
        with corpus.store.acting_as(principal):
            rows = corpus.store.query(corpus.rock)

        assert names_of(rows) == readable_names_by_oracle(corpus, principal)


@settings(deadline=None, max_examples=40)
@given(principal=principals, **corpora)
def test_count_matches_the_rows_the_literal_oracle_allows(
    n_legacy: int, labeled: list[dict[str, Any]], principal: dc.Principal
) -> None:
    """Given a corpus of legacy rows and rows with explicit labels, and any principal
    When the principal counts the rock rows
    Then it counts the rows the ADR-008 oracle lets the principal read
    """
    with labeled_corpus(n_legacy, labeled) as corpus:
        with corpus.store.acting_as(principal):
            counted = corpus.store.count(corpus.rock)

        assert counted == len(readable_names_by_oracle(corpus, principal))


@settings(deadline=None, max_examples=40)
@given(row=owned_rows())
def test_the_owner_discovers_their_row(row: dict[str, Any]) -> None:
    """Given a corpus of one row with an owner, and its owner as a principal with no memberships
    When the owner queries the rock rows
    Then the owner's row is among them
    """
    with labeled_corpus(0, [row]) as corpus:
        owner = dc.Principal(uid=row["owner"], memberships={})

        with corpus.store.acting_as(owner):
            rows = corpus.store.query(corpus.rock)

        assert names_of(rows) == {"post-0"}


# --- dereference: the record labels, read through get_many ---------------------------


@settings(deadline=None, max_examples=40)
@given(principal=principals, **corpora)
def test_dereference_returns_the_real_row_exactly_where_the_literal_oracle_allows(
    n_legacy: int, labeled: list[dict[str, Any]], principal: dc.Principal
) -> None:
    """Given a corpus of legacy rows and rows with explicit labels, and any principal
    When the principal dereferences every row's OID with get_many
    Then it gets the real row where the ADR-008 oracle allows and a redacted twin elsewhere
    """
    with labeled_corpus(n_legacy, labeled) as corpus:
        with corpus.store.acting_as(principal):
            slots = corpus.store.get_many(list(corpus.oids.values()))

        assert names_dereferenced(corpus, slots) == readable_names_by_oracle(corpus, principal)


@settings(deadline=None, max_examples=40)
@given(row=owned_rows())
def test_the_owner_of_a_row_dereferences_it(row: dict[str, Any]) -> None:
    """Given a row with an owner and its owner as a principal with no memberships
    When the owner dereferences the row's OID
    Then the owner gets the real row
    """
    with labeled_corpus(0, [row]) as corpus:
        owner = dc.Principal(uid=row["owner"], memberships={})

        with corpus.store.acting_as(owner):
            slots = corpus.store.get_many(list(corpus.oids.values()))

        assert names_dereferenced(corpus, slots) == {"post-0"}


@settings(deadline=None, max_examples=40)
@given(**corpora)
def test_root_dereferences_every_row(n_legacy: int, labeled: list[dict[str, Any]]) -> None:
    """Given a corpus of rows and a root principal outside every group
    When root dereferences every row's OID
    Then root gets every real row
    """
    with labeled_corpus(n_legacy, labeled) as corpus:
        with corpus.store.acting_as(OUTSIDE_ROOT):
            slots = corpus.store.get_many(list(corpus.oids.values()))

        assert names_dereferenced(corpus, slots) == set(corpus.oracle)


@settings(deadline=None, max_examples=40)
@given(**corpora)
def test_anonymous_dereferences_as_the_literal_oracle_does(
    n_legacy: int, labeled: list[dict[str, Any]]
) -> None:
    """Given a corpus of rows and the anonymous principal
    When it dereferences every row's OID
    Then it gets the real rows the ADR-008 oracle lets it read
    """
    with labeled_corpus(n_legacy, labeled) as corpus:
        with corpus.store.acting_as(ANONYMOUS):
            slots = corpus.store.get_many(list(corpus.oids.values()))

        assert names_dereferenced(corpus, slots) == readable_names_by_oracle(corpus, ANONYMOUS)


# --- D1: the two label sources agree ---------------------------------------------------


@settings(deadline=None, max_examples=40)
@given(principal=principals, **corpora)
def test_discovery_and_dereference_agree(
    n_legacy: int, labeled: list[dict[str, Any]], principal: dc.Principal
) -> None:
    """Given a corpus of legacy rows and rows with explicit labels, and any principal
    When the principal queries the rock rows and dereferences every row's OID
    Then the rows it discovers are the rows it dereferences as real
    """
    with labeled_corpus(n_legacy, labeled) as corpus:
        with corpus.store.acting_as(principal):
            rows = corpus.store.query(corpus.rock)
            slots = corpus.store.get_many(list(corpus.oids.values()))

        assert names_of(rows) == names_dereferenced(corpus, slots)


# --- the fixed corpus (deterministic examples) --------------------------------------

MIXED_ROWS = [
    {"owner": 2, "groups": [], "read_floor": dc.VIEWER},
    {"owner": 3, "groups": [7], "read_floor": dc.VIEWER},
    {"owner": 3, "groups": [8], "read_floor": dc.VIEWER},
]


@pytest.fixture
def mixed_corpus() -> Iterator[Corpus]:
    """Yield rows A (post-0, owner 2), B (post-1, owner 3, group 7) and C (post-2, group 8)."""
    with labeled_corpus(0, MIXED_ROWS) as corpus:
        yield corpus


def test_an_owner_with_a_membership_grant_reads_the_owned_and_the_granted_rows(
    mixed_corpus: Corpus,
) -> None:
    """Given rows A (owner 2), B (group 7) and C (group 8)
    When uid 2 holding a viewer membership of group 7 queries the rows
    Then they read A and B and not C
    """
    owner_with_grant = dc.Principal(uid=2, memberships={7: dc.VIEWER})

    with mixed_corpus.store.acting_as(owner_with_grant):
        rows = mixed_corpus.store.query(mixed_corpus.rock)

    assert names_of(rows) == {"post-0", "post-1"}


def test_a_grant_does_not_widen_what_the_owner_reads_afterwards(mixed_corpus: Corpus) -> None:
    """Given uid 2 holding a viewer membership of group 7 queried once
    When uid 2 queries again, now without memberships
    Then uid 2 reads only row A, the row it owns
    """
    with mixed_corpus.store.acting_as(dc.Principal(uid=2, memberships={7: dc.VIEWER})):
        mixed_corpus.store.query(mixed_corpus.rock)

    with mixed_corpus.store.acting_as(dc.Principal(uid=2, memberships={})):
        rows = mixed_corpus.store.query(mixed_corpus.rock)

    assert names_of(rows) == {"post-0"}


def test_an_owner_without_memberships_reads_only_the_rows_they_own(mixed_corpus: Corpus) -> None:
    """Given rows A (owner 2), B (owner 3, group 7) and C (owner 3, group 8)
    When uid 3 without memberships queries the rows
    Then uid 3 reads B and C and not A
    """
    with mixed_corpus.store.acting_as(dc.Principal(uid=3, memberships={})):
        rows = mixed_corpus.store.query(mixed_corpus.rock)

    assert names_of(rows) == {"post-1", "post-2"}


def test_a_root_outside_every_group_reads_every_row(mixed_corpus: Corpus) -> None:
    """Given rows A, B and C
    When a root principal outside every group queries the rows
    Then it reads all three
    """
    with mixed_corpus.store.acting_as(OUTSIDE_ROOT):
        rows = mixed_corpus.store.query(mixed_corpus.rock)

    assert names_of(rows) == {"post-0", "post-1", "post-2"}


# --- discovery and dereference across an index-cache reopen (D4, ADR-005 / #12) --------
#
# The deferred #12 ``_last_values`` rebuild (ADR-005: a cache load never pays the O(extent)
# reconstruction on a read-only reopen) must still make the index postings that discovery reads
# agree with the record labels that dereference reads on the far side, exercised with a REAL
# on-disk cache (``cache_index=True``), not just the in-memory backend the fuzz tests above use.

CACHED_ROCK_LABELS: list[Labels] = [
    (0, [], dc.VIEWER),
    (2, [dc.WORLD], dc.AGENT),
    (3, [7, 8], dc.CURATOR),
    (0, [dc.WORLD, 9], dc.EXECUTIVE),
]


@dataclass
class ReopenedCache:
    """A store reopened over an on-disk index cache, the rocks committed before and their labels."""

    store: dc.Store
    rock: type[Any]
    index: ClassIndexes
    corpus: Corpus


@pytest.fixture
def reopened_cache(tmp_path: Path) -> Iterator[ReopenedCache]:
    """Yield a store reopened over the sidecar cache a closed predecessor wrote."""
    path = tmp_path / "store"
    rock_class = protected_rock()
    first = dc.Store.open(path, cache_index=True)
    oracle: dict[str, Labels] = {}
    oids: dict[str, int] = {}
    with first.acting_as(ROOT):
        for i, (owner, groups, floor) in enumerate(CACHED_ROCK_LABELS):
            rec = rock_class(name=f"r{i}")
            oids[f"r{i}"] = first.store(rec)
            seed_labels(rec, owner, groups, floor)
            oracle[f"r{i}"] = (owner, groups, floor)
        first.commit()
    # force an index build so the sidecar cache actually captures this class
    # (apply() only folds into ALREADY-built indexes; an unbuilt class is never dumped at close()).
    list(first.query(rock_class))
    first.close()

    store = dc.Store.open(path, cache_index=True)
    index = store._index.ensure(type_info(rock_class))
    yield ReopenedCache(store, rock_class, index, Corpus(store, rock_class, oracle, oids))
    store.close()


def test_cache_reopen_defers_the_committed_labels_rebuild(reopened_cache: ReopenedCache) -> None:
    """Given a store reopened over an on-disk index cache
    When nothing has read a committed label yet
    Then the class index still owes the deferred rebuild, so it came from the cache not a scan
    """
    assert reopened_cache.index._needs_lv_rebuild is True


@settings(
    deadline=None, max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(principal=principals)
def test_discovery_and_dereference_agree_with_the_oracle_after_cache_reopen(
    reopened_cache: ReopenedCache, principal: dc.Principal
) -> None:
    """Given a store reopened over an on-disk index cache
    When any principal queries the rocks and dereferences every rock's OID
    Then both read exactly the rocks the ADR-008 oracle lets the principal read
    """
    corpus = reopened_cache.corpus

    with corpus.store.acting_as(principal):
        rows = corpus.store.query(corpus.rock)
        slots = corpus.store.get_many(list(corpus.oids.values()))

    expected = readable_names_by_oracle(corpus, principal)
    assert names_of(rows) == expected
    assert names_dereferenced(corpus, slots) == expected


def test_reading_committed_labels_triggers_the_deferred_rebuild(
    reopened_cache: ReopenedCache,
) -> None:
    """Given a store reopened over an on-disk index cache
    When a committed label is read
    Then the class index no longer owes the deferred rebuild
    """
    first_oid = reopened_cache.corpus.oids["r0"]

    reopened_cache.index.committed_labels(first_oid)

    assert reopened_cache.index._needs_lv_rebuild is False
