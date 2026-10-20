"""W3-2: filtering ``query``/``query_iter``/``count``/``pluck``/``explain``
(ADR-008, epic #168, issue #173).

These are DISCOVERY surfaces (R12): the readable intersect
(``candidate & readable_bitmap(principal, ci)``) runs BEFORE any
window/order/hydration/decode, so a denied row never reaches the result,
never pins a page, and is never even decoded. ``explain()`` is the odd one
out — it never filters rows, it post-filters the reported NUMBERS only
(R12/D8): the plan's ``condition``/``residual``/``indexed`` strings must stay
byte-identical to what an unfenced read would report (the planner itself
never learns about permissions). ``query_iter`` additionally captures the
session principal at CALL time, not iteration time — the ADR-008-mandated
guard against a stream outliving its ``acting_as`` scope.

``get``/``get_many``/``incoming`` are W3-3's own file
(``test_read_fence_discovery.py``); this file is the query-family matrix.
"""

from __future__ import annotations

from dataclasses import field
from typing import Annotated, Any

import pytest

import datacrystal as dc
from datacrystal._conditions import Condition

TEAM = 5
OTHER_TEAM = 6

OWNER = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
MEMBER_AT_FLOOR = dc.Principal(uid=3, memberships={TEAM: dc.AGENT})
MEMBER_BELOW_FLOOR = dc.Principal(uid=8, memberships={TEAM: dc.VIEWER})
OUTSIDER = dc.Principal(uid=4, memberships={OTHER_TEAM: dc.CURATOR})
ANON = dc.Principal(uid=0)
ROOT = dc.Principal(uid=99, memberships={dc.WORLD: dc.EXECUTIVE})

PRINCIPALS: dict[str, dc.Principal] = {
    "owner": OWNER,
    "member-at-floor": MEMBER_AT_FLOOR,
    "member-below-floor": MEMBER_BELOW_FLOOR,
    "outsider": OUTSIDER,
    "anonymous": ANON,
    "root": ROOT,
}
PRINCIPAL_NAMES = list(PRINCIPALS)


@dc.entity(protected=True)
class Specimen:
    """A protected specimen with a plain, a sorted, a multivalued and a residual-only field."""

    label: Annotated[str, dc.Unique]
    category: Annotated[str, dc.Index] = "misc"  # plain bitmap index
    mass_g: Annotated[float, dc.SortedIndex] = 0.0  # ADR-004 range field
    tags: Annotated[list[str], dc.Index] = field(default_factory=list)  # #13 multivalued
    note: str = ""  # residual-only field


@dc.entity
class SpecimenControl:
    """The unprotected twin: same shape and field names, no permission columns.

    The D8 regression pin's control: a query shaped identically to a ``Specimen`` query must
    classify (indexed/residual) the same way and, under root (no filtering), count the same.
    """

    label: Annotated[str, dc.Unique]
    category: Annotated[str, dc.Index] = "misc"
    mass_g: Annotated[float, dc.SortedIndex] = 0.0
    tags: Annotated[list[str], dc.Index] = field(default_factory=list)
    note: str = ""


# A 10-row corpus with independently-varying (owner, groups, read_floor) labels
# and (category, mass_g, tags, note) data — so permission filtering and query
# conditions are two genuinely orthogonal axes. Committed under ROOT, which
# bypasses the W2-5 write-gate ceiling entirely (the property-oracle-test
# precedent), so arbitrary label combinations are reachable in one commit.
ROWS: list[dict[str, Any]] = [
    {"label": "S0", "owner": OWNER.uid, "groups": [TEAM], "floor": dc.AGENT,
        "category": "gem", "mass_g": 0.0, "tags": ["common"], "note": "plain"},
    {"label": "S1", "owner": OWNER.uid, "groups": [], "floor": dc.VIEWER,
        "category": "rock", "mass_g": 10.0, "tags": ["rare"], "note": "flag"},
    {"label": "S2", "owner": OUTSIDER.uid, "groups": [TEAM], "floor": dc.VIEWER,
        "category": "mineral", "mass_g": 20.0, "tags": ["common"], "note": "plain"},
    {"label": "S3", "owner": 0, "groups": [dc.WORLD], "floor": dc.VIEWER,
        "category": "gem", "mass_g": 30.0, "tags": ["rare"], "note": "plain"},
    {"label": "S4", "owner": OWNER.uid, "groups": [TEAM], "floor": dc.CURATOR,
        "category": "rock", "mass_g": 40.0, "tags": ["common"], "note": "flag"},
    {"label": "S5", "owner": OUTSIDER.uid, "groups": [OTHER_TEAM], "floor": dc.CURATOR,
        "category": "mineral", "mass_g": 50.0, "tags": ["rare"], "note": "plain"},
    {"label": "S6", "owner": 0, "groups": [TEAM], "floor": dc.AGENT,
        "category": "gem", "mass_g": 60.0, "tags": ["common"], "note": "plain"},
    {"label": "S7", "owner": 0, "groups": [TEAM, OTHER_TEAM], "floor": dc.CURATOR,
        "category": "rock", "mass_g": 70.0, "tags": ["rare"], "note": "flag"},
    {"label": "S8", "owner": 0, "groups": [], "floor": dc.VIEWER,
        "category": "mineral", "mass_g": 80.0, "tags": ["common"], "note": "plain"},
    {"label": "S9", "owner": 0, "groups": [dc.WORLD], "floor": dc.CURATOR,
        "category": "gem", "mass_g": 90.0, "tags": ["rare"], "note": "plain"},
]


def _seed(store: dc.Store) -> None:
    with store.acting_as(ROOT):
        for row in ROWS:
            record = Specimen(
                label=row["label"],
                category=row["category"],
                mass_g=row["mass_g"],
                tags=list(row["tags"]),
                note=row["note"],
            )
            store.store(record)
            record._dc_owner = row["owner"]  # pyrefly: ignore[missing-attribute]
            for group in row["groups"]:
                record._dc_groups.append(group)  # pyrefly: ignore[missing-attribute]
            record._dc_read_floor = row["floor"]  # pyrefly: ignore[missing-attribute]
        for row in ROWS:
            store.store(
                SpecimenControl(
                    label=row["label"],
                    category=row["category"],
                    mass_g=row["mass_g"],
                    tags=list(row["tags"]),
                    note=row["note"],
                )
            )
        store.commit()


@pytest.fixture
def seeded(store: dc.Store) -> dc.Store:
    """Return the store with the ten-row corpus committed."""
    _seed(store)
    return store


# --- the seven condition shapes, built for a given entity class --------------


def _bare_class(cls: type) -> type:
    return cls


def _indexed_eq(cls: type) -> Condition:
    return dc.fields(cls).category == "gem"


def _indexed_in(cls: type) -> Condition:
    return dc.fields(cls).category.in_(["gem", "rock"])


def _sorted_ge(cls: type) -> Condition:
    return dc.fields(cls).mass_g >= 50.0


def _sorted_between(cls: type) -> Condition:
    return (dc.fields(cls).mass_g >= 20.0) & (dc.fields(cls).mass_g <= 60.0)


def _residual(cls: type) -> Condition:
    """Build the one shape no bitmap answers: ``note`` carries no Index/SortedIndex marker."""
    return dc.fields(cls).note == "flag"


def _contains(cls: type) -> Condition:
    return dc.fields(cls).tags.contains("rare")


CONDITION_BUILDERS = {
    "bare-class": _bare_class,
    "indexed-eq": _indexed_eq,
    "indexed-in": _indexed_in,
    "sorted-ge": _sorted_ge,
    "sorted-between": _sorted_between,
    "residual": _residual,
    "contains": _contains,
}
CONDITION_NAMES = list(CONDITION_BUILDERS)


def _target_for(condition_name: str, cls: type) -> type | Condition:
    return CONDITION_BUILDERS[condition_name](cls)


# --- the frozen oracle: who may read what, per condition --------------------
# Each row is (condition, principal, the labels the principal gets): the rows whose data
# match the condition AND that the principal can read (ADR-008); root gets the unfiltered
# condition result. Frozen from the former computed oracle (``can_read_row`` over ROWS), so a
# changed fence shows as a changed literal here.

READABLE_LABELS = [
    ("bare-class", "owner", "S0 S1 S2 S3 S4 S6 S7"),
    ("bare-class", "member-at-floor", "S0 S2 S3 S6"),
    ("bare-class", "member-below-floor", "S2 S3"),
    ("bare-class", "outsider", "S2 S3 S5 S7"),
    ("bare-class", "anonymous", "S3"),
    ("bare-class", "root", "S0 S1 S2 S3 S4 S5 S6 S7 S8 S9"),
    ("indexed-eq", "owner", "S0 S3 S6"),
    ("indexed-eq", "member-at-floor", "S0 S3 S6"),
    ("indexed-eq", "member-below-floor", "S3"),
    ("indexed-eq", "outsider", "S3"),
    ("indexed-eq", "anonymous", "S3"),
    ("indexed-eq", "root", "S0 S3 S6 S9"),
    ("indexed-in", "owner", "S0 S1 S3 S4 S6 S7"),
    ("indexed-in", "member-at-floor", "S0 S3 S6"),
    ("indexed-in", "member-below-floor", "S3"),
    ("indexed-in", "outsider", "S3 S7"),
    ("indexed-in", "anonymous", "S3"),
    ("indexed-in", "root", "S0 S1 S3 S4 S6 S7 S9"),
    ("sorted-ge", "owner", "S6 S7"),
    ("sorted-ge", "member-at-floor", "S6"),
    ("sorted-ge", "member-below-floor", ""),
    ("sorted-ge", "outsider", "S5 S7"),
    ("sorted-ge", "anonymous", ""),
    ("sorted-ge", "root", "S5 S6 S7 S8 S9"),
    ("sorted-between", "owner", "S2 S3 S4 S6"),
    ("sorted-between", "member-at-floor", "S2 S3 S6"),
    ("sorted-between", "member-below-floor", "S2 S3"),
    ("sorted-between", "outsider", "S2 S3 S5"),
    ("sorted-between", "anonymous", "S3"),
    ("sorted-between", "root", "S2 S3 S4 S5 S6"),
    ("residual", "owner", "S1 S4 S7"),
    ("residual", "member-at-floor", ""),
    ("residual", "member-below-floor", ""),
    ("residual", "outsider", "S7"),
    ("residual", "anonymous", ""),
    ("residual", "root", "S1 S4 S7"),
    ("contains", "owner", "S1 S3 S7"),
    ("contains", "member-at-floor", "S3"),
    ("contains", "member-below-floor", "S3"),
    ("contains", "outsider", "S3 S5 S7"),
    ("contains", "anonymous", "S3"),
    ("contains", "root", "S1 S3 S5 S7 S9"),
]

# The same rows with the number of labels the principal gets.

READABLE_COUNTS = [
    ("bare-class", "owner", 7),
    ("bare-class", "member-at-floor", 4),
    ("bare-class", "member-below-floor", 2),
    ("bare-class", "outsider", 4),
    ("bare-class", "anonymous", 1),
    ("bare-class", "root", 10),
    ("indexed-eq", "owner", 3),
    ("indexed-eq", "member-at-floor", 3),
    ("indexed-eq", "member-below-floor", 1),
    ("indexed-eq", "outsider", 1),
    ("indexed-eq", "anonymous", 1),
    ("indexed-eq", "root", 4),
    ("indexed-in", "owner", 6),
    ("indexed-in", "member-at-floor", 3),
    ("indexed-in", "member-below-floor", 1),
    ("indexed-in", "outsider", 2),
    ("indexed-in", "anonymous", 1),
    ("indexed-in", "root", 7),
    ("sorted-ge", "owner", 2),
    ("sorted-ge", "member-at-floor", 1),
    ("sorted-ge", "member-below-floor", 0),
    ("sorted-ge", "outsider", 2),
    ("sorted-ge", "anonymous", 0),
    ("sorted-ge", "root", 5),
    ("sorted-between", "owner", 4),
    ("sorted-between", "member-at-floor", 3),
    ("sorted-between", "member-below-floor", 2),
    ("sorted-between", "outsider", 3),
    ("sorted-between", "anonymous", 1),
    ("sorted-between", "root", 5),
    ("residual", "owner", 3),
    ("residual", "member-at-floor", 0),
    ("residual", "member-below-floor", 0),
    ("residual", "outsider", 1),
    ("residual", "anonymous", 0),
    ("residual", "root", 3),
    ("contains", "owner", 3),
    ("contains", "member-at-floor", 1),
    ("contains", "member-below-floor", 1),
    ("contains", "outsider", 3),
    ("contains", "anonymous", 1),
    ("contains", "root", 5),
]

# Each row is (condition, principal, plan.candidates, plan.extent). A residual predicate
# cannot be pre-filtered by a bitmap: candidates is the READABLE EXTENT scanned there, not
# the post-residual match count.

PLAN_NUMBERS = [
    ("bare-class", "owner", 7, 7),
    ("bare-class", "member-at-floor", 4, 4),
    ("bare-class", "member-below-floor", 2, 2),
    ("bare-class", "outsider", 4, 4),
    ("bare-class", "anonymous", 1, 1),
    ("bare-class", "root", 10, 10),
    ("indexed-eq", "owner", 3, 7),
    ("indexed-eq", "member-at-floor", 3, 4),
    ("indexed-eq", "member-below-floor", 1, 2),
    ("indexed-eq", "outsider", 1, 4),
    ("indexed-eq", "anonymous", 1, 1),
    ("indexed-eq", "root", 4, 10),
    ("indexed-in", "owner", 6, 7),
    ("indexed-in", "member-at-floor", 3, 4),
    ("indexed-in", "member-below-floor", 1, 2),
    ("indexed-in", "outsider", 2, 4),
    ("indexed-in", "anonymous", 1, 1),
    ("indexed-in", "root", 7, 10),
    ("sorted-ge", "owner", 2, 7),
    ("sorted-ge", "member-at-floor", 1, 4),
    ("sorted-ge", "member-below-floor", 0, 2),
    ("sorted-ge", "outsider", 2, 4),
    ("sorted-ge", "anonymous", 0, 1),
    ("sorted-ge", "root", 5, 10),
    ("sorted-between", "owner", 4, 7),
    ("sorted-between", "member-at-floor", 3, 4),
    ("sorted-between", "member-below-floor", 2, 2),
    ("sorted-between", "outsider", 3, 4),
    ("sorted-between", "anonymous", 1, 1),
    ("sorted-between", "root", 5, 10),
    ("residual", "owner", 7, 7),
    ("residual", "member-at-floor", 4, 4),
    ("residual", "member-below-floor", 2, 2),
    ("residual", "outsider", 4, 4),
    ("residual", "anonymous", 1, 1),
    ("residual", "root", 10, 10),
    ("contains", "owner", 3, 7),
    ("contains", "member-at-floor", 1, 4),
    ("contains", "member-below-floor", 1, 2),
    ("contains", "outsider", 3, 4),
    ("contains", "anonymous", 1, 1),
    ("contains", "root", 5, 10),
]

# Each row is (order field, principal, the labels the principal gets from the whole extent).

ORDERED_READABLE_LABELS = [
    ("category", "owner", "S0 S1 S2 S3 S4 S6 S7"),
    ("category", "member-at-floor", "S0 S2 S3 S6"),
    ("category", "member-below-floor", "S2 S3"),
    ("category", "outsider", "S2 S3 S5 S7"),
    ("category", "anonymous", "S3"),
    ("category", "root", "S0 S1 S2 S3 S4 S5 S6 S7 S8 S9"),
    ("note", "owner", "S0 S1 S2 S3 S4 S6 S7"),
    ("note", "member-at-floor", "S0 S2 S3 S6"),
    ("note", "member-below-floor", "S2 S3"),
    ("note", "outsider", "S2 S3 S5 S7"),
    ("note", "anonymous", "S3"),
    ("note", "root", "S0 S1 S2 S3 S4 S5 S6 S7 S8 S9"),
]


def _labels_of(found: list[Any]) -> str:
    """Join the labels of found entities in label order, as the tables above write them."""
    return " ".join(sorted(entity.label for entity in found))


# --- query / query_iter / count / pluck / explain: the oracle matrix ---------


@pytest.mark.parametrize(("condition_name", "principal_name", "expected_labels"), READABLE_LABELS)
def test_query_returns_the_readable_matches(
    seeded: dc.Store, condition_name: str, principal_name: str, expected_labels: str
) -> None:
    """Given the ten-row corpus with independently varying labels and data
    When a principal queries a condition
    Then the labels returned are the matching rows that principal can read
    """
    target = _target_for(condition_name, Specimen)
    with seeded.acting_as(PRINCIPALS[principal_name]):
        got = seeded.query(target)
    assert _labels_of(got) == expected_labels


@pytest.mark.parametrize(("condition_name", "principal_name", "expected_labels"), READABLE_LABELS)
def test_query_iter_streams_the_readable_matches(
    seeded: dc.Store, condition_name: str, principal_name: str, expected_labels: str
) -> None:
    """Given the ten-row corpus with independently varying labels and data
    When a principal streams a condition with query_iter
    Then the labels streamed are the matching rows that principal can read
    """
    target = _target_for(condition_name, Specimen)
    with seeded.acting_as(PRINCIPALS[principal_name]):
        got = list(seeded.query_iter(target))
    assert _labels_of(got) == expected_labels


@pytest.mark.parametrize(("condition_name", "principal_name", "expected_count"), READABLE_COUNTS)
def test_count_covers_the_readable_matches(
    seeded: dc.Store, condition_name: str, principal_name: str, expected_count: int
) -> None:
    """Given the ten-row corpus with independently varying labels and data
    When a principal counts a condition
    Then the count is the number of matching rows that principal can read
    """
    target = _target_for(condition_name, Specimen)
    with seeded.acting_as(PRINCIPALS[principal_name]):
        counted = seeded.count(target)
    assert counted == expected_count


@pytest.mark.parametrize(("condition_name", "principal_name", "expected_labels"), READABLE_LABELS)
def test_pluck_projects_the_readable_matches(
    seeded: dc.Store, condition_name: str, principal_name: str, expected_labels: str
) -> None:
    """Given the ten-row corpus with independently varying labels and data
    When a principal plucks the label of a condition
    Then the labels plucked are the matching rows that principal can read
    """
    target = _target_for(condition_name, Specimen)
    with seeded.acting_as(PRINCIPALS[principal_name]):
        plucked = seeded.pluck(target, "label")
    assert " ".join(sorted(plucked)) == expected_labels


@pytest.mark.parametrize(
    ("condition_name", "principal_name", "expected_candidates", "expected_extent"), PLAN_NUMBERS
)
def test_explain_reports_the_readable_numbers(
    seeded: dc.Store,
    condition_name: str,
    principal_name: str,
    expected_candidates: int,
    expected_extent: int,
) -> None:
    """Given the ten-row corpus with independently varying labels and data
    When a principal explains a condition
    Then the plan's candidates are the readable candidates
    And the plan's extent is the readable extent
    """
    target = _target_for(condition_name, Specimen)
    with seeded.acting_as(PRINCIPALS[principal_name]):
        plan = seeded.explain(target)
    assert plan.candidates == expected_candidates
    assert plan.extent == expected_extent


# --- explain(): the D8 regression pin — strings never change ------------------


@pytest.mark.parametrize("principal_name", PRINCIPAL_NAMES)
@pytest.mark.parametrize("condition_name", CONDITION_NAMES)
def test_explain_strings_are_principal_invariant(
    seeded: dc.Store, condition_name: str, principal_name: str
) -> None:
    """Given the ten-row corpus and the plan root gets for a condition
    When another principal explains the same condition
    Then its condition, residual, indexed flag and typename equal root's
    """
    target = _target_for(condition_name, Specimen)
    with seeded.acting_as(ROOT):
        baseline = seeded.explain(target)
    with seeded.acting_as(PRINCIPALS[principal_name]):
        plan = seeded.explain(target)
    assert plan.condition == baseline.condition
    assert plan.residual == baseline.residual
    assert plan.indexed == baseline.indexed
    assert plan.typename == baseline.typename


def _normalize(text: str | None, class_name: str) -> str | None:
    return None if text is None else text.replace(class_name, "•")


@pytest.mark.parametrize("condition_name", CONDITION_NAMES)
def test_explain_strings_match_the_unprotected_twin(
    store: dc.Store, condition_name: str
) -> None:
    """Given the same corpus in a protected class and in its unprotected twin
    When root explains a condition on the protected class and the twin is explained
    Then the class-name-normalized condition, residual and indexed flag are the twin's
    """
    _seed(store)
    protected_target = _target_for(condition_name, Specimen)
    control_target = _target_for(condition_name, SpecimenControl)
    with store.acting_as(ROOT):
        protected_plan = store.explain(protected_target)
    control_plan = store.explain(control_target)
    assert _normalize(protected_plan.condition, "Specimen") == _normalize(
        control_plan.condition, "SpecimenControl"
    )
    assert _normalize(protected_plan.residual, "Specimen") == _normalize(
        control_plan.residual, "SpecimenControl"
    )
    assert protected_plan.indexed == control_plan.indexed


@pytest.mark.parametrize("condition_name", CONDITION_NAMES)
def test_explain_numbers_under_root_match_the_unprotected_twin(
    store: dc.Store, condition_name: str
) -> None:
    """Given the same corpus in a protected class and in its unprotected twin
    When root explains a condition on the protected class and the twin is explained
    Then root's candidates and extent are the twin's true numbers
    """
    _seed(store)
    protected_target = _target_for(condition_name, Specimen)
    control_target = _target_for(condition_name, SpecimenControl)
    with store.acting_as(ROOT):
        protected_plan = store.explain(protected_target)
    control_plan = store.explain(control_target)
    assert protected_plan.candidates == control_plan.candidates
    assert protected_plan.extent == control_plan.extent


# --- order_by: indexed AND un-indexed sort fields, filtered first -------------


@pytest.mark.parametrize(
    ("order_field", "principal_name", "expected_labels"), ORDERED_READABLE_LABELS
)
def test_ordered_query_returns_only_the_readable_rows(
    seeded: dc.Store, order_field: str, principal_name: str, expected_labels: str
) -> None:
    """Given the ten-row corpus
    When a principal queries the whole extent ordered by an indexed or an unindexed field
    Then the rows returned are exactly the ones that principal can read
    """
    with seeded.acting_as(PRINCIPALS[principal_name]):
        got = seeded.query(Specimen, order_by=order_field)
    assert _labels_of(got) == expected_labels


@pytest.mark.parametrize(
    ("order_field", "principal_name", "expected_labels"), ORDERED_READABLE_LABELS
)
def test_ordered_pluck_returns_only_the_readable_rows(
    seeded: dc.Store, order_field: str, principal_name: str, expected_labels: str
) -> None:
    """Given the ten-row corpus
    When a principal plucks labels ordered by an indexed or an unindexed field
    Then the labels plucked are exactly the ones that principal can read
    """
    with seeded.acting_as(PRINCIPALS[principal_name]):
        plucked = seeded.pluck(Specimen, "label", order_by=order_field)
    assert " ".join(sorted(plucked)) == expected_labels


@pytest.mark.parametrize("principal_name", PRINCIPAL_NAMES)
@pytest.mark.parametrize("order_field", ["category", "note"])
def test_ordered_values_are_sorted_with_no_denied_row_in_the_ordering(
    seeded: dc.Store, order_field: str, principal_name: str
) -> None:
    """Given the ten-row corpus
    When a principal plucks the order field ordered by that same field
    Then the values come back sorted, denied rows never sneaking into the ordering
    """
    with seeded.acting_as(PRINCIPALS[principal_name]):
        values = seeded.pluck(Specimen, order_field, order_by=order_field)
    assert values == sorted(values)


# --- paging determinism: filtered set is exactly partitioned -----------------
# OWNER reads 7 of 10 rows (S0 S1 S2 S3 S4 S6 S7): three limit=3 pages of 3, 3 and 1.


@pytest.mark.parametrize("order_by", [None, "category"])
def test_paging_spans_three_pages_of_the_filtered_result(
    seeded: dc.Store, order_by: str | None
) -> None:
    """Given the ten-row corpus of which the owner reads seven rows
    When the owner pages the whole extent with limit 3
    Then the pages hold 3, 3 and 1 rows
    """
    with seeded.acting_as(OWNER):
        first = seeded.query(Specimen, limit=3, offset=0, order_by=order_by)
        second = seeded.query(Specimen, limit=3, offset=3, order_by=order_by)
        third = seeded.query(Specimen, limit=3, offset=6, order_by=order_by)
    assert [len(first), len(second), len(third)] == [3, 3, 1]


@pytest.mark.parametrize("order_by", [None, "category"])
def test_paging_repeats_no_row_across_pages(seeded: dc.Store, order_by: str | None) -> None:
    """Given the ten-row corpus of which the owner reads seven rows
    When the owner pages the whole extent with limit 3
    Then no label appears on two pages
    """
    with seeded.acting_as(OWNER):
        first = seeded.query(Specimen, limit=3, offset=0, order_by=order_by)
        second = seeded.query(Specimen, limit=3, offset=3, order_by=order_by)
        third = seeded.query(Specimen, limit=3, offset=6, order_by=order_by)
    collected = [*first, *second, *third]
    assert len(collected) == len({entity.label for entity in collected})


@pytest.mark.parametrize("order_by", [None, "category"])
def test_paging_leaves_no_gap_in_the_filtered_result(
    seeded: dc.Store, order_by: str | None
) -> None:
    """Given the ten-row corpus of which the owner reads seven rows
    When the owner pages the whole extent with limit 3
    Then the pages together hold exactly the seven readable rows
    """
    with seeded.acting_as(OWNER):
        first = seeded.query(Specimen, limit=3, offset=0, order_by=order_by)
        second = seeded.query(Specimen, limit=3, offset=3, order_by=order_by)
        third = seeded.query(Specimen, limit=3, offset=6, order_by=order_by)
    assert _labels_of([*first, *second, *third]) == "S0 S1 S2 S3 S4 S6 S7"


# --- query_iter: the principal is captured at CALL time, not iteration time --


def test_query_iter_captures_the_principal_at_call_time(seeded: dc.Store) -> None:
    """Given a stream opened under the member-at-floor principal whose acting_as scope has ended
    When the stream is iterated outside that scope
    Then it yields the member-at-floor's readable rows
    """
    with seeded.acting_as(MEMBER_AT_FLOOR):
        stream = seeded.query_iter(Specimen)
    streamed = list(stream)
    assert _labels_of(streamed) == "S0 S2 S3 S6"


def test_query_iter_stream_is_not_the_ambient_view_after_the_scope_ends(
    seeded: dc.Store,
) -> None:
    """Given a stream opened under the member-at-floor principal whose acting_as scope has ended
    When the stream is iterated outside that scope
    Then it does not yield the anonymous view, which is only S3
    """
    with seeded.acting_as(MEMBER_AT_FLOOR):
        stream = seeded.query_iter(Specimen)
    streamed = list(stream)
    assert _labels_of(streamed) != "S3"


# --- root: unfiltered on every surface ----------------------------------------


def test_root_queries_the_full_extent(seeded: dc.Store) -> None:
    """Given the ten-row corpus
    When root queries the class
    Then all ten rows come back
    """
    with seeded.acting_as(ROOT):
        got = seeded.query(Specimen)
    assert _labels_of(got) == "S0 S1 S2 S3 S4 S5 S6 S7 S8 S9"


def test_root_counts_the_full_extent(seeded: dc.Store) -> None:
    """Given the ten-row corpus
    When root counts the class
    Then the count is ten
    """
    with seeded.acting_as(ROOT):
        counted = seeded.count(Specimen)
    assert counted == 10


def test_root_plucks_the_full_extent(seeded: dc.Store) -> None:
    """Given the ten-row corpus
    When root plucks the label of the class
    Then all ten labels come back
    """
    with seeded.acting_as(ROOT):
        plucked = seeded.pluck(Specimen, "label")
    assert " ".join(sorted(plucked)) == "S0 S1 S2 S3 S4 S5 S6 S7 S8 S9"


def test_root_streams_the_full_extent(seeded: dc.Store) -> None:
    """Given the ten-row corpus
    When root streams the class with query_iter
    Then all ten rows stream
    """
    with seeded.acting_as(ROOT):
        streamed = list(seeded.query_iter(Specimen))
    assert _labels_of(streamed) == "S0 S1 S2 S3 S4 S5 S6 S7 S8 S9"


def test_root_explains_the_full_extent(seeded: dc.Store) -> None:
    """Given the ten-row corpus
    When root explains the class
    Then the plan's extent is ten
    """
    with seeded.acting_as(ROOT):
        extent = seeded.explain(Specimen).extent
    assert extent == 10
