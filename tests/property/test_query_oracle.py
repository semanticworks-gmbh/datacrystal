"""The M4 exit gate: query vs a brute-force oracle (KICKOFF M4).

Hypothesis builds a random cabinet history — upserts, field updates,
deletions, interleaved commits — then a random Condition tree over indexed
and non-indexed fields (every operator, including the M4 contains/
startswith), and demands that the planner's answer equal a brute-force
``cond.evaluate`` sweep over plain rows. One oracle checks five surfaces:
``query()``, ``count()``, ``pluck()`` and the snapshot's bitmap
``query()``/``count()`` at the same watermark.

Backend: memory. The planner is backend-independent (indexes are built
from a type scan either way); memory-vs-sqlite equivalence is pinned by
the parametrized engine tests.

A script may delete every row; the loud-empty warning is correct there and pinned by its own
unit tests, so these tests filter it.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings

from datacrystal._conditions import Condition
from tests.property._cabinet_history import conditions, matching_sids, replayed, scripts

pytestmark = pytest.mark.filterwarnings("ignore::datacrystal.UnseenTypeWarning")


@settings(deadline=None)
@given(ops=scripts(), cond=conditions)
def test_query_returns_the_rows_the_oracle_matches(ops: list[tuple[Any, ...]], cond: Condition) -> None:
    """Given a random committed cabinet history and a random condition tree
    When the store queries the condition
    Then it returns the sids a brute-force sweep over plain rows matches
    """
    with replayed(ops) as history:
        found = history.store.query(cond)

        assert {e.sid for e in found} == matching_sids(history.rows, cond)


@settings(deadline=None)
@given(ops=scripts(), cond=conditions)
def test_count_equals_the_number_of_rows_the_oracle_matches(
    ops: list[tuple[Any, ...]], cond: Condition
) -> None:
    """Given a random committed cabinet history and a random condition tree
    When the store counts the condition
    Then the count equals the number of sids a brute-force sweep matches
    """
    with replayed(ops) as history:
        counted = history.store.count(cond)

        assert counted == len(matching_sids(history.rows, cond))


@settings(deadline=None)
@given(ops=scripts(), cond=conditions)
def test_pluck_returns_the_sids_the_oracle_matches(ops: list[tuple[Any, ...]], cond: Condition) -> None:
    """Given a random committed cabinet history and a random condition tree
    When the store plucks the sids of the condition
    Then it returns the sids a brute-force sweep matches
    """
    with replayed(ops) as history:
        plucked = history.store.pluck(cond, "sid")

        assert sorted(plucked) == sorted(matching_sids(history.rows, cond))


@settings(deadline=None)
@given(ops=scripts(), cond=conditions)
def test_snapshot_query_returns_the_rows_the_oracle_matches(
    ops: list[tuple[Any, ...]], cond: Condition
) -> None:
    """Given a random committed cabinet history, a snapshot of it and a random condition tree
    When the snapshot queries the condition
    Then it returns the sids a brute-force sweep matches
    """
    with replayed(ops) as history, history.store.snapshot() as snapshot:
        found = snapshot.query(cond)

        assert {v.sid for v in found} == matching_sids(history.rows, cond)


@settings(deadline=None)
@given(ops=scripts(), cond=conditions)
def test_snapshot_count_equals_the_number_of_rows_the_oracle_matches(
    ops: list[tuple[Any, ...]], cond: Condition
) -> None:
    """Given a random committed cabinet history, a snapshot of it and a random condition tree
    When the snapshot counts the condition
    Then the count equals the number of sids a brute-force sweep matches
    """
    with replayed(ops) as history, history.store.snapshot() as snapshot:
        counted = snapshot.count(cond)

        assert counted == len(matching_sids(history.rows, cond))
