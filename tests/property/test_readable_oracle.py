"""W3-1 oracle: readable_bitmap / can_read_row / committed_labels vs a
literal, independent transcription of the ADR-008 access predicate
(epic #168, issue #173).

Hypothesis builds a labeled corpus — a mix of freshly protected rows with
explicit (owner, groups, read_floor) labels and LEGACY rows (committed before the class
turned ``protected=True``, decoding under the ADR-008 R7 fill) — plus principals spanning
every ADR-008 category (anonymous, uid-only, single/multi-membership, owner-of-a-row, root).
The corpus, the principals and the oracle live in ``_labeled_corpus.py``. For a principal
and a row, three sources must agree:

1. :func:`datacrystal._permissions.can_read_row` (the decode-level twin)
2. ``oid in (readable_bitmap(p, ci) or ci.extent)`` (the bitmap compiler)
3. ``oracle_can_read`` — a LITERAL transcription of the ADR-008 Context predicate block,
   written independently of ``_permissions.py`` so a shared bug in the library code cannot
   cancel out against the oracle.

``ClassIndexes.committed_labels`` must also agree with ``Store._prior_labels`` (D1: the two
committed-label sources must never diverge) for every committed row, including after an
on-disk index-cache reopen (the deferred ``_rebuild_last_values`` / #12 path).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import datacrystal as dc
from datacrystal._entity import type_info
from datacrystal._indexes import ClassIndexes, readable_bitmap
from datacrystal._permissions import can_read_row
from tests.property._labeled_corpus import (
    ANONYMOUS,
    OUTSIDE_ROOT,
    ROOT,
    Labels,
    committed_labels_by_oid,
    labeled_corpus,
    labeled_rows,
    labels,
    oracle_can_read,
    owned_rows,
    principals,
    prior_labels_by_oid,
    protected_rock,
    readable_oids_by_oracle,
)

corpora = {
    "n_legacy": st.integers(min_value=0, max_value=3),
    "labeled": st.lists(labeled_rows(), min_size=1, max_size=6),
}


# --- can_read_row, the decode-level twin ----------------------------------------


@given(row_labels=labels, principal=principals)
def test_can_read_row_agrees_with_the_literal_oracle(
    row_labels: Labels, principal: dc.Principal
) -> None:
    """Given a row's labels, freshly protected or legacy, and any principal
    When can_read_row decides whether the principal reads the row
    Then it decides as the ADR-008 oracle does
    """
    decision = can_read_row(principal, *row_labels)

    assert decision == oracle_can_read(principal, *row_labels)


@given(row=owned_rows())
def test_the_owner_of_a_row_can_read_it(row: dict[str, Any]) -> None:
    """Given a row with an owner and its owner as a principal with no memberships
    When can_read_row decides
    Then the owner reads the row
    """
    owner = dc.Principal(uid=row["owner"], memberships={})

    decision = can_read_row(owner, row["owner"], row["groups"], row["read_floor"])

    assert decision is True


@given(row_labels=labels)
def test_root_can_read_any_row(row_labels: Labels) -> None:
    """Given a row's labels and a root principal outside every group
    When can_read_row decides
    Then root reads the row
    """
    decision = can_read_row(OUTSIDE_ROOT, *row_labels)

    assert decision is True


@given(row_labels=labels)
def test_anonymous_agrees_with_the_literal_oracle(row_labels: Labels) -> None:
    """Given a row's labels and the anonymous principal
    When can_read_row decides
    Then it decides as the ADR-008 oracle does
    """
    decision = can_read_row(ANONYMOUS, *row_labels)

    assert decision == oracle_can_read(ANONYMOUS, *row_labels)


# --- the bitmap compiler over a stored corpus -----------------------------------


@settings(deadline=None, max_examples=40)
@given(**corpora)
def test_corpus_extent_holds_exactly_the_stored_rows(
    n_legacy: int, labeled: list[dict[str, Any]]
) -> None:
    """Given a corpus of legacy rows and rows with explicit labels
    When the class index's extent is read
    Then it holds exactly the oids the corpus stored
    """
    with labeled_corpus(n_legacy, labeled) as corpus:
        assert set(corpus.index.extent) == set(corpus.oracle)


@settings(deadline=None, max_examples=40)
@given(principal=principals, **corpora)
def test_readable_bitmap_agrees_with_the_literal_oracle(
    n_legacy: int, labeled: list[dict[str, Any]], principal: dc.Principal
) -> None:
    """Given a corpus of legacy rows and rows with explicit labels, and any principal
    When readable_bitmap compiles the principal's readable rows
    Then they are the oids the ADR-008 oracle lets the principal read
    """
    with labeled_corpus(n_legacy, labeled) as corpus:
        bitmap = readable_bitmap(principal, corpus.index)

        readable = set(corpus.index.extent) if bitmap is None else set(bitmap)

        assert readable == readable_oids_by_oracle(corpus, principal)


@settings(deadline=None, max_examples=40)
@given(row=owned_rows())
def test_the_owner_sees_their_row_in_the_readable_bitmap(row: dict[str, Any]) -> None:
    """Given a corpus of one row with an owner, and its owner as a principal with no memberships
    When readable_bitmap compiles the owner's readable rows
    Then the owner's row is among them
    """
    with labeled_corpus(0, [row]) as corpus:
        owner = dc.Principal(uid=row["owner"], memberships={})
        (only_oid,) = corpus.oracle

        bitmap = readable_bitmap(owner, corpus.index)

        readable = set(corpus.index.extent) if bitmap is None else set(bitmap)
        assert only_oid in readable


# --- the two committed-label sources (D1/D4) ------------------------------------


@settings(deadline=None, max_examples=40)
@given(**corpora)
def test_committed_labels_agree_with_prior_labels(
    n_legacy: int, labeled: list[dict[str, Any]]
) -> None:
    """Given a corpus of legacy rows and rows with explicit labels
    When the index's committed labels and the store's prior labels are read for every row
    Then the two committed-label sources agree
    """
    with labeled_corpus(n_legacy, labeled) as corpus:
        assert committed_labels_by_oid(corpus) == prior_labels_by_oid(corpus)


# --- committed_labels across an index-cache reopen (D4, ADR-005 / #12) ----------
#
# The deferred #12 ``_last_values`` rebuild (ADR-005: a cache load never pays the O(extent)
# reconstruction on a read-only reopen) must still make ``committed_labels`` agree with
# ``Store._prior_labels`` on the far side, exercised with a REAL on-disk cache
# (``cache_index=True``), not just the in-memory backend the fuzz tests above use.
#
# Groups compare as SETS, not as ordered lists: ``_rebuild_last_values`` reconstructs a
# multivalued field's list by walking postings-map keys (dict-iteration order), not the
# record's original append order, a pre-existing #12 property of every multivalued Index
# field. ADR-008's predicate only ever asks for set membership, so it is not observable.

CACHED_ROCK_LABELS: list[Labels] = [
    (0, [], dc.VIEWER),
    (2, [dc.WORLD], dc.AGENT),
    (3, [7, 8], dc.CURATOR),
    (0, [dc.WORLD, 9], dc.EXECUTIVE),
]


@dataclass
class ReopenedCache:
    """A store reopened over an on-disk index cache, and the rocks committed before."""

    store: dc.Store
    index: ClassIndexes
    oids: list[int]


@pytest.fixture
def reopened_cache(tmp_path: Path) -> Iterator[ReopenedCache]:
    """Yield a store reopened over the sidecar cache a closed predecessor wrote."""
    path = tmp_path / "store"
    rock_class = protected_rock()
    first = dc.Store.open(path, cache_index=True)
    oids: list[int] = []
    with first.acting_as(ROOT):
        for i, (owner, groups, floor) in enumerate(CACHED_ROCK_LABELS):
            rec = rock_class(name=f"r{i}")
            oid = first.store(rec)
            rec._dc_owner = owner
            for g in groups:
                rec._dc_groups.append(g)
            rec._dc_read_floor = floor
            oids.append(oid)
        first.commit()
    # force an index build so the sidecar cache actually captures this class
    # (apply() only folds into ALREADY-built indexes; an unbuilt class is never dumped at close()).
    list(first.query(rock_class))
    first.close()

    store = dc.Store.open(path, cache_index=True)
    yield ReopenedCache(store, store._index.ensure(type_info(rock_class)), oids)
    store.close()


def _label_sets_of_index(cache: ReopenedCache) -> dict[int, tuple[Any, ...] | None]:
    labels_by_oid = {oid: cache.index.committed_labels(oid) for oid in cache.oids}
    return {
        oid: None if found is None else (found[0], set(found[1]), found[2])
        for oid, found in labels_by_oid.items()
    }


def _label_sets_of_store(cache: ReopenedCache) -> dict[int, tuple[Any, ...] | None]:
    prior = {
        oid: cache.store._prior_labels(cache.store._backend.load_many([oid])[oid])[:3]
        for oid in cache.oids
    }
    return {oid: (owner, set(groups), floor) for oid, (owner, groups, floor) in prior.items()}


def test_cache_reopen_defers_the_committed_labels_rebuild(reopened_cache: ReopenedCache) -> None:
    """Given a store reopened over an on-disk index cache
    When nothing has read a committed label yet
    Then the class index still owes the deferred rebuild, so it came from the cache not a scan
    """
    assert reopened_cache.index._needs_lv_rebuild is True


def test_committed_labels_agree_with_prior_labels_after_cache_reopen(
    reopened_cache: ReopenedCache,
) -> None:
    """Given a store reopened over an on-disk index cache
    When the index's committed labels and the store's prior labels are read for every rock
    Then they agree, groups compared as sets
    """
    assert _label_sets_of_index(reopened_cache) == _label_sets_of_store(reopened_cache)


def test_reading_committed_labels_triggers_the_deferred_rebuild(
    reopened_cache: ReopenedCache,
) -> None:
    """Given a store reopened over an on-disk index cache
    When a committed label is read
    Then the class index no longer owes the deferred rebuild
    """
    reopened_cache.index.committed_labels(reopened_cache.oids[0])

    assert reopened_cache.index._needs_lv_rebuild is False
