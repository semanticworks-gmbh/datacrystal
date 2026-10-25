"""order_by= on query()/pluck() and the snapshot surface (#25).

Frozen-api contract, pinned against a brute-force oracle:
``order_by=(field, 'asc'|'desc')`` (or a bare field = ascending) sorts the whole
match set before the window, **NULLs last**, with a **stable ascending-OID
tiebreak** (deterministic offset paging). Indexed fields order straight from the
index (a ``SortedIndex`` field is the cheap path); un-indexed fields decode the
sort field for every match. Both paths must produce the identical order.
"""

from __future__ import annotations

from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import type_info


@dc.entity
class Item:
    """A mineral-cabinet row with sorted, plain-indexed and un-indexed sort fields."""

    code: Annotated[str, dc.Unique]
    grade: Annotated[float | None, dc.SortedIndex] = None  # sorted (cheap order path)
    category: Annotated[str | None, dc.Index] = None  # plain index
    label: str = ""  # un-indexed (decode path)
    note: str | None = None  # un-indexed, nullable
    tags: Annotated[list[str], dc.Index] = ()  # type: ignore[assignment]


# (code, grade, category, label) — insertion order == OID-ascending order, so a
# stable sort's tiebreak is this order. Duplicates on grade (a/d, b/f) and on
# category exercise the tiebreak; None grades (c, g) exercise NULLs-last.
_DATA = [
    ("a", 3.0, "ig", "Apatite"),
    ("b", 7.0, "ig", "Quartz"),
    ("c", None, "se", "Halite"),
    ("d", 3.0, "me", "Talc"),
    ("e", 5.5, "se", "Beryl"),
    ("f", 7.0, "me", "Topaz"),
    ("g", None, "ig", "Gypsum"),
    ("h", 1.0, "se", "Diamond"),
]


F = dc.fields(Item)


@pytest.fixture
def items_store(store: dc.Store) -> dc.Store:
    """Commit the eight items of _DATA."""
    for code, grade, category, label in _DATA:
        store.store(Item(code=code, grade=grade, category=category, label=label))
    store.commit()
    return store


# --- the core contract, every sort field, both directions --------------------


@pytest.mark.parametrize(
    ("direction", "expected_codes"),
    [("asc", "h,a,d,e,b,f,c,g"), ("desc", "b,f,e,a,d,h,c,g")],
)
def test_order_by_sorted_index_field(
    items_store: dc.Store, direction: str, expected_codes: str
) -> None:
    """The None grades (c, g) come last and equal grades keep ascending-OID order.

    Given eight items, two of them with grade None
    When they are queried ordered by the SortedIndex field grade
    Then the codes follow the grade order with NULLs last
    """
    found = items_store.query(Item, order_by=(F.grade, direction))

    assert [it.code for it in found] == expected_codes.split(",")


@pytest.mark.parametrize(
    ("direction", "expected_codes"),
    [("asc", "a,b,g,d,f,c,e,h"), ("desc", "c,e,h,d,f,a,b,g")],
)
def test_order_by_plain_index_field(
    items_store: dc.Store, direction: str, expected_codes: str
) -> None:
    """Given eight items over the categories ig, me and se
    When they are queried ordered by the plain-index field category
    Then the codes follow the category order, ties in ascending-OID order
    """
    found = items_store.query(Item, order_by=(F.category, direction))

    assert [it.code for it in found] == expected_codes.split(",")


@pytest.mark.parametrize(
    ("direction", "expected_codes"),
    [("asc", "a,e,h,g,c,b,d,f"), ("desc", "f,d,b,c,g,h,e,a")],
)
def test_order_by_unindexed_field_decode_path(
    items_store: dc.Store, direction: str, expected_codes: str
) -> None:
    """Given eight items with distinct labels from Apatite to Topaz
    When they are queried ordered by the un-indexed field label
    Then the codes follow the label order
    """
    found = items_store.query(Item, order_by=(F.label, direction))

    assert [it.code for it in found] == expected_codes.split(",")


def test_bare_field_is_ascending(items_store: dc.Store) -> None:
    """Given eight items
    When they are queried ordered by the bare field grade
    Then the codes are in ascending grade order
    """
    found = items_store.query(Item, order_by=F.grade)

    assert [it.code for it in found] == ["h", "a", "d", "e", "b", "f", "c", "g"]


def test_class_attribute_expression_orders_like_the_fields_proxy(items_store: dc.Store) -> None:
    """Given eight items
    When they are queried ordered by the magic class attribute Item.grade, descending
    Then the codes are b, f, e, a, d, h, c, g
    """
    found = items_store.query(Item, order_by=(Item.grade, "desc"))

    assert [it.code for it in found] == ["b", "f", "e", "a", "d", "h", "c", "g"]


def test_bare_field_equals_the_explicit_ascending_tuple(items_store: dc.Store) -> None:
    """Given eight items
    When they are queried ordered by the bare grade and by (grade, "asc")
    Then both orders are identical
    """
    bare = items_store.query(Item, order_by=F.grade)
    explicit = items_store.query(Item, order_by=(F.grade, "asc"))

    assert [it.code for it in bare] == [it.code for it in explicit]


def test_string_field_name_accepted(items_store: dc.Store) -> None:
    """Given eight items
    When they are queried ordered by the field name "grade" descending
    Then the codes are in descending grade order
    """
    found = items_store.query(Item, order_by=("grade", "desc"))

    assert [it.code for it in found] == ["b", "f", "e", "a", "d", "h", "c", "g"]


def test_nulls_sort_last_ascending(items_store: dc.Store) -> None:
    """Given eight items where c and g have grade None
    When they are queried ordered by grade ascending
    Then the last two codes are c and g, the None grades in OID order
    """
    found = items_store.query(Item, order_by=(F.grade, "asc"))

    assert [it.code for it in found][-2:] == ["c", "g"]


def test_nulls_sort_last_descending(items_store: dc.Store) -> None:
    """Given eight items where c and g have grade None
    When they are queried ordered by grade descending
    Then the last two codes are c and g, the None grades in OID order
    """
    found = items_store.query(Item, order_by=(F.grade, "desc"))

    assert [it.code for it in found][-2:] == ["c", "g"]


def test_equal_grades_keep_ascending_oid_order_ascending(items_store: dc.Store) -> None:
    """Given a and d at grade 3.0 and b and f at grade 7.0, inserted in that order
    When they are queried ordered by grade ascending
    Then a precedes d
    And b precedes f
    """
    found = items_store.query(Item, order_by=(F.grade, "asc"))

    assert [it.code for it in found].index("a") < [it.code for it in found].index("d")
    assert [it.code for it in found].index("b") < [it.code for it in found].index("f")


def test_equal_grades_keep_ascending_oid_order_descending(items_store: dc.Store) -> None:
    """Given a and d at grade 3.0 and b and f at grade 7.0, inserted in that order
    When they are queried ordered by grade descending
    Then a still precedes d
    And b still precedes f
    """
    found = items_store.query(Item, order_by=(F.grade, "desc"))

    assert [it.code for it in found].index("a") < [it.code for it in found].index("d")
    assert [it.code for it in found].index("b") < [it.code for it in found].index("f")


# --- order_by composes with a condition (residual path) and with the window --


def test_order_by_with_a_condition(items_store: dc.Store) -> None:
    """Given eight items of which c, e and h are in category se
    When category == "se" is queried ordered by grade ascending
    Then the codes are h, e and c
    """
    found = items_store.query(F.category == "se", order_by=(F.grade, "asc"))

    assert [it.code for it in found] == ["h", "e", "c"]


def test_first_page_of_an_ordered_window(items_store: dc.Store) -> None:
    """Given eight items ordered by grade ascending as h, a, d, e, b, f, c, g
    When the first page of 3 is queried
    Then the codes are h, a and d
    """
    page = items_store.query(Item, order_by=(F.grade, "asc"), limit=3, offset=0)

    assert [it.code for it in page] == ["h", "a", "d"]


def test_second_page_of_an_ordered_window(items_store: dc.Store) -> None:
    """Given eight items ordered by grade ascending as h, a, d, e, b, f, c, g
    When the second page of 3 is queried
    Then the codes are e, b and f
    """
    page = items_store.query(Item, order_by=(F.grade, "asc"), limit=3, offset=3)

    assert [it.code for it in page] == ["e", "b", "f"]


def test_index_path_orders_by_grade_descending(items_store: dc.Store) -> None:
    """Given eight items
    When they are queried ordered by the SortedIndex grade descending, the index path
    Then the codes are b, f, e, a, d, h, c, g
    """
    via_index = items_store.query(Item, order_by=(F.grade, "desc"))

    assert [it.code for it in via_index] == ["b", "f", "e", "a", "d", "h", "c", "g"]


def test_decode_path_orders_by_grade_descending(items_store: dc.Store) -> None:
    """An un-indexed residual that matches everything forces the decode/object path.

    Given eight items
    When label != "__never__" is queried ordered by grade descending
    Then the codes are b, f, e, a, d, h, c, g, identical to the index path
    """
    via_decode = items_store.query(F.label != "__never__", order_by=(F.grade, "desc"))

    assert [it.code for it in via_decode] == ["b", "f", "e", "a", "d", "h", "c", "g"]


# --- pluck ------------------------------------------------------------------


def test_pluck_order_by_single(items_store: dc.Store) -> None:
    """Given eight items
    When their codes are plucked ordered by grade ascending
    Then the codes are h, a, d, e, b, f, c, g
    """
    codes = items_store.pluck(Item, "code", order_by=(F.grade, "asc"))

    assert codes == ["h", "a", "d", "e", "b", "f", "c", "g"]


def test_pluck_sort_field_need_not_be_projected(items_store: dc.Store) -> None:
    """Given eight items
    When their labels are plucked ordered by grade descending, grade not projected
    Then the labels follow the descending grade order
    """
    labels = items_store.pluck(Item, "label", order_by=(F.grade, "desc"))

    assert labels == [
        "Quartz",
        "Topaz",
        "Beryl",
        "Apatite",
        "Talc",
        "Diamond",
        "Halite",
        "Gypsum",
    ]


def test_pluck_order_by_unindexed_with_window(items_store: dc.Store) -> None:
    """Given eight items
    When 4 codes are plucked ordered by the un-indexed label ascending
    Then the codes are a, e, h and g
    """
    codes = items_store.pluck(Item, "code", order_by=(F.label, "asc"), limit=4)

    assert codes == ["a", "e", "h", "g"]


# --- snapshot symmetry ------------------------------------------------------


def test_snapshot_query_order_by(items_store: dc.Store) -> None:
    """Given eight items and a snapshot
    When the snapshot is queried ordered by grade descending
    Then the codes are b, f, e, a, d, h, c, g
    """
    snap = items_store.snapshot()

    found = snap.query(Item, order_by=(F.grade, "desc"))

    assert [v.code for v in found] == ["b", "f", "e", "a", "d", "h", "c", "g"]


def test_snapshot_query_order_by_unindexed(items_store: dc.Store) -> None:
    """Given eight items and a snapshot
    When the snapshot is queried ordered by label ascending
    Then the codes are a, e, h, g, c, b, d, f
    """
    snap = items_store.snapshot()

    found = snap.query(Item, order_by=(F.label, "asc"))

    assert [v.code for v in found] == ["a", "e", "h", "g", "c", "b", "d", "f"]


def test_snapshot_all_order_by(items_store: dc.Store) -> None:
    """Given eight items and a snapshot
    When all Items are read ordered by grade ascending
    Then the codes are h, a, d, e, b, f, c, g
    """
    snap = items_store.snapshot()

    found = snap.all(Item, order_by=(F.grade, "asc"))

    assert [v.code for v in found] == ["h", "a", "d", "e", "b", "f", "c", "g"]


# --- the frozen contract's loud edges ---------------------------------------


def test_bad_direction_raises(items_store: dc.Store) -> None:
    """Given eight items
    When they are queried with the direction "sideways"
    Then QueryError names the allowed directions
    """
    with pytest.raises(dc.QueryError, match=r"asc.*desc|direction"):
        items_store.query(Item, order_by=(F.grade, "sideways"))


def test_unknown_order_field_raises(items_store: dc.Store) -> None:
    """Given eight items
    When they are queried ordered by the field "nonexistent"
    Then QueryError says there is no persisted field
    """
    with pytest.raises(dc.QueryError, match="no persisted field"):
        items_store.query(Item, order_by="nonexistent")


def test_multivalued_order_field_rejected(items_store: dc.Store) -> None:
    """Given eight items
    When they are queried ordered by the multi-valued field tags
    Then QueryError says it is multi-valued
    """
    with pytest.raises(dc.QueryError, match="multi-valued"):
        items_store.query(Item, order_by=F.tags)


class _CountingPostings(dict):
    """A postings map that counts ``__getitem__``, the distinct keys the order_by walk touches.

    Proves the #66 short-circuit visits O(offset+limit) keys, not O(extent).
    """

    def __init__(self, base: dict) -> None:
        super().__init__(base)
        self.gets = 0

    def __getitem__(self, key: object) -> object:
        self.gets += 1
        return super().__getitem__(key)


@pytest.fixture
def counted_grades(store: dc.Store) -> _CountingPostings:
    """Commit 300 items with distinct grades 0.0 to 299.0 and count the grade postings reads.

    Each key contributes exactly one OID, so "keys touched" equals "OIDs collected".
    """
    for i in range(300):
        store.store(Item(code=f"i{i:03d}", grade=float(i)))
    store.commit()
    column = store._index.ensure(type_info(Item))
    counter = _CountingPostings(column.eq["grade"])
    column.eq["grade"] = counter
    return counter


def test_limit_returns_the_lowest_grades(store: dc.Store, counted_grades: _CountingPostings) -> None:
    """Given 300 items with distinct grades
    When 5 are queried ordered by grade ascending
    Then the codes are i000 to i004
    """
    found = store.query(Item, order_by=(F.grade, "asc"), limit=5)

    assert [r.code for r in found] == ["i000", "i001", "i002", "i003", "i004"]


def test_limit_touches_about_limit_keys_not_the_extent(
    store: dc.Store, counted_grades: _CountingPostings
) -> None:
    """Given 300 items with distinct grades
    When 5 are queried ordered by grade ascending
    Then at most 8 keys of the grade postings were touched
    """
    store.query(Item, order_by=(F.grade, "asc"), limit=5)

    assert counted_grades.gets <= 8


def test_offset_page_returns_the_next_grades(
    store: dc.Store, counted_grades: _CountingPostings
) -> None:
    """Given 300 items with distinct grades
    When 5 are queried ordered by grade ascending from offset 5
    Then the codes are i005 to i009
    """
    page = store.query(Item, order_by=(F.grade, "asc"), limit=5, offset=5)

    assert [r.code for r in page] == ["i005", "i006", "i007", "i008", "i009"]


def test_offset_page_touches_about_offset_plus_limit_keys(
    store: dc.Store, counted_grades: _CountingPostings
) -> None:
    """Given 300 items with distinct grades
    When 5 are queried ordered by grade ascending from offset 5
    Then at most 12 keys of the grade postings were touched
    """
    store.query(Item, order_by=(F.grade, "asc"), limit=5, offset=5)

    assert counted_grades.gets <= 12


def test_descending_limit_returns_the_highest_grades(
    store: dc.Store, counted_grades: _CountingPostings
) -> None:
    """Given 300 items with distinct grades
    When 3 are queried ordered by grade descending
    Then the codes are i299, i298 and i297
    """
    found = store.query(Item, order_by=(F.grade, "desc"), limit=3)

    assert [r.code for r in found] == ["i299", "i298", "i297"]


def test_descending_limit_walks_from_the_top(
    store: dc.Store, counted_grades: _CountingPostings
) -> None:
    """Given 300 items with distinct grades
    When 3 are queried ordered by grade descending
    Then at most 6 keys of the grade postings were touched
    """
    store.query(Item, order_by=(F.grade, "desc"), limit=3)

    assert counted_grades.gets <= 6


def test_unlimited_order_by_returns_every_item(
    store: dc.Store, counted_grades: _CountingPostings
) -> None:
    """Given 300 items with distinct grades
    When all are queried ordered by grade ascending without a limit
    Then 300 items come back
    """
    full = store.query(Item, order_by=(F.grade, "asc"))

    assert len(full) == 300


def test_unlimited_order_by_visits_every_key(
    store: dc.Store, counted_grades: _CountingPostings
) -> None:
    """The honest contrast: this proves the limit gates above are real, not a vacuous bound.

    Given 300 items with distinct grades
    When all are queried ordered by grade ascending without a limit
    Then at least 300 keys of the grade postings were touched
    """
    store.query(Item, order_by=(F.grade, "asc"))

    assert counted_grades.gets >= 300


def test_without_order_by_items_come_back_in_oid_order(items_store: dc.Store) -> None:
    """The surface stays frozen: no order_by behaves exactly as before.

    Given eight items inserted as a to h
    When they are queried without order_by
    Then the codes are a to h, ascending OID order
    """
    found = items_store.query(Item)

    assert [it.code for it in found] == ["a", "b", "c", "d", "e", "f", "g", "h"]
