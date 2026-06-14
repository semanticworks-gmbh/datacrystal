"""order_by= on query()/pluck() and the snapshot surface (#25).

Frozen-api contract, pinned against a brute-force oracle:
``order_by=(field, 'asc'|'desc')`` (or a bare field = ascending) sorts the whole
match set before the window, **NULLs last**, with a **stable ascending-OID
tiebreak** (deterministic offset paging). Indexed fields order straight from the
index (a ``SortedIndex`` field is the cheap path); un-indexed fields decode the
sort field for every match. Both paths must produce the identical order.
"""

from __future__ import annotations

from typing import Annotated, Any, Callable

import pytest

import datacrystal as dc
from datacrystal._errors import QueryError


@dc.entity
class Item:
    code: Annotated[str, dc.Unique]
    grade: Annotated[float | None, dc.SortedIndex] = None  # sorted (cheap order path)
    category: Annotated[str | None, dc.Index] = None       # plain index
    label: str = ""                                         # un-indexed (decode path)
    note: str | None = None                                # un-indexed, nullable
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


@pytest.fixture
def items_store(store):
    for code, grade, category, label in _DATA:
        store.store(Item(code=code, grade=grade, category=category, label=label))
    store.commit()
    return store


def _oracle(rows: list[tuple[str, Any]], descending: bool) -> list[str]:
    """Expected codes for ordering ``rows`` (``(code, value)`` in OID order) by
    ``value``: NULLs last, ascending-OID (== insertion) tiebreak."""
    present = [r for r in rows if r[1] is not None]
    absent = [r for r in rows if r[1] is None]
    present.sort(key=lambda r: r[1], reverse=descending)  # stable → OID tiebreak
    return [code for code, _ in present + absent]


def _rows(valuef: Callable[[tuple[str, float | None, str | None, str]], Any]
          ) -> list[tuple[str, Any]]:
    return [(code, valuef(row)) for row in _DATA for code in (row[0],)]


# --- the core contract, every sort field, both directions --------------------

@pytest.mark.parametrize("descending", [False, True])
def test_order_by_sorted_index_field(items_store, descending):
    direction = "desc" if descending else "asc"
    got = [it.code for it in items_store.query(Item, order_by=(Item.grade, direction))]
    assert got == _oracle(_rows(lambda r: r[1]), descending)


@pytest.mark.parametrize("descending", [False, True])
def test_order_by_plain_index_field(items_store, descending):
    direction = "desc" if descending else "asc"
    got = [it.code for it in items_store.query(Item, order_by=(Item.category, direction))]
    assert got == _oracle(_rows(lambda r: r[2]), descending)


@pytest.mark.parametrize("descending", [False, True])
def test_order_by_unindexed_field_decode_path(items_store, descending):
    direction = "desc" if descending else "asc"
    got = [it.code for it in items_store.query(Item, order_by=(Item.label, direction))]
    assert got == _oracle(_rows(lambda r: r[3]), descending)


def test_bare_field_is_ascending(items_store):
    bare = [it.code for it in items_store.query(Item, order_by=Item.grade)]
    tup = [it.code for it in items_store.query(Item, order_by=(Item.grade, "asc"))]
    assert bare == tup == _oracle(_rows(lambda r: r[1]), False)


def test_string_field_name_accepted(items_store):
    got = [it.code for it in items_store.query(Item, order_by=("grade", "desc"))]
    assert got == _oracle(_rows(lambda r: r[1]), True)


def test_nulls_sort_last_both_directions(items_store):
    asc = [it.code for it in items_store.query(Item, order_by=(Item.grade, "asc"))]
    desc = [it.code for it in items_store.query(Item, order_by=(Item.grade, "desc"))]
    assert asc[-2:] == ["c", "g"]   # the None grades, in OID order, last
    assert desc[-2:] == ["c", "g"]


def test_stable_oid_tiebreak(items_store):
    # equal grades (a/d at 3.0, b/f at 7.0) keep ascending-OID order in BOTH dirs
    asc = [it.code for it in items_store.query(Item, order_by=(Item.grade, "asc"))]
    desc = [it.code for it in items_store.query(Item, order_by=(Item.grade, "desc"))]
    assert asc.index("a") < asc.index("d")
    assert asc.index("b") < asc.index("f")
    assert desc.index("a") < desc.index("d")
    assert desc.index("b") < desc.index("f")


# --- order_by composes with a condition (residual path) and with the window --

def test_order_by_with_condition_residual(items_store):
    # a residual predicate (note is un-indexed → residual) + order_by
    cond = dc.fields(Item).category == "se"
    got = [it.code for it in items_store.query(cond, order_by=(Item.grade, "asc"))]
    rows = [(r[0], r[1]) for r in _DATA if r[2] == "se"]
    assert got == _oracle(rows, False)


def test_window_paging_is_deterministic(items_store):
    full = [it.code for it in items_store.query(Item, order_by=(Item.grade, "asc"))]
    page1 = [it.code for it in items_store.query(Item, order_by=(Item.grade, "asc"),
                                                 limit=3, offset=0)]
    page2 = [it.code for it in items_store.query(Item, order_by=(Item.grade, "asc"),
                                                 limit=3, offset=3)]
    assert page1 == full[:3]
    assert page2 == full[3:6]


def test_index_and_decode_paths_agree(items_store):
    # grade is a SortedIndex (index path); forcing it through a residual makes
    # the SAME query take the decode/object path — orders must be identical.
    via_index = [it.code for it in items_store.query(Item, order_by=(Item.grade, "desc"))]
    cond = dc.fields(Item).label != "__never__"   # un-indexed residual, matches all
    via_decode = [it.code for it in items_store.query(cond, order_by=(Item.grade, "desc"))]
    assert via_index == via_decode


# --- pluck ------------------------------------------------------------------

def test_pluck_order_by_single(items_store):
    got = items_store.pluck(Item, "code", order_by=(Item.grade, "asc"))
    assert got == _oracle(_rows(lambda r: r[1]), False)


def test_pluck_sort_field_need_not_be_projected(items_store):
    # project label, order by grade (not in the projection)
    got = items_store.pluck(Item, "label", order_by=(Item.grade, "desc"))
    order = _oracle(_rows(lambda r: r[1]), True)
    label_of = {r[0]: r[3] for r in _DATA}
    assert got == [label_of[code] for code in order]


def test_pluck_order_by_unindexed_with_window(items_store):
    got = items_store.pluck(Item, "code", order_by=(Item.label, "asc"), limit=4)
    assert got == _oracle(_rows(lambda r: r[3]), False)[:4]


# --- snapshot symmetry ------------------------------------------------------

def test_snapshot_query_order_by(items_store):
    snap = items_store.snapshot()
    got = [v.code for v in snap.query(Item, order_by=(Item.grade, "desc"))]
    assert got == _oracle(_rows(lambda r: r[1]), True)


def test_snapshot_query_order_by_unindexed(items_store):
    snap = items_store.snapshot()
    got = [v.code for v in snap.query(Item, order_by=(Item.label, "asc"))]
    assert got == _oracle(_rows(lambda r: r[3]), False)


def test_snapshot_all_order_by(items_store):
    snap = items_store.snapshot()
    got = [v.code for v in snap.all(Item, order_by=(Item.grade, "asc"))]
    assert got == _oracle(_rows(lambda r: r[1]), False)


# --- the frozen contract's loud edges ---------------------------------------

def test_bad_direction_raises(items_store):
    with pytest.raises(QueryError, match="asc.*desc|direction"):
        items_store.query(Item, order_by=(Item.grade, "sideways"))


def test_unknown_order_field_raises(items_store):
    with pytest.raises(QueryError, match="no persisted field"):
        items_store.query(Item, order_by="nonexistent")


def test_multivalued_order_field_rejected(items_store):
    with pytest.raises(QueryError, match="multi-valued"):
        items_store.query(Item, order_by=Item.tags)


class _CountingPostings(dict):
    """A postings map that counts ``__getitem__`` — i.e. how many distinct keys
    the order_by walk actually touches. Proves the #66 short-circuit visits
    O(offset+limit) keys, not O(extent)."""

    def __init__(self, base):
        super().__init__(base)
        self.gets = 0

    def __getitem__(self, key):
        self.gets += 1
        return super().__getitem__(key)


def test_order_by_limit_short_circuits_on_sorted_index(store):
    from datacrystal._entity import type_info

    # 300 distinct grades, one Item each → each key contributes exactly one OID,
    # so "keys touched" == "OIDs collected": a clean op-count.
    for i in range(300):
        store.store(Item(code=f"i{i:03d}", grade=float(i)))
    store.commit()
    ci = store._index.ensure(type_info(Item))  # pyright: ignore[reportPrivateUsage]
    counter = _CountingPostings(ci.eq["grade"])
    ci.eq["grade"] = counter

    # limit=5 ascending → touches ~5 keys, NOT 300, and is correct
    res = store.query(Item, order_by=(Item.grade, "asc"), limit=5)
    assert [r.code for r in res] == [f"i{i:03d}" for i in range(5)]
    assert counter.gets <= 8, f"touched {counter.gets} keys for limit=5 (want ~5 of 300)"

    # offset+limit also short-circuits (touches ~offset+limit keys) and pages right
    counter.gets = 0
    page2 = store.query(Item, order_by=(Item.grade, "asc"), limit=5, offset=5)
    assert [r.code for r in page2] == [f"i{i:03d}" for i in range(5, 10)]
    assert counter.gets <= 12, counter.gets

    # descending walks from the top, same short-circuit
    counter.gets = 0
    desc = store.query(Item, order_by=(Item.grade, "desc"), limit=3)
    assert [r.code for r in desc] == ["i299", "i298", "i297"]
    assert counter.gets <= 6, counter.gets

    # the no-limit path is the honest contrast: it visits EVERY key (proving the
    # gate above is real, not a vacuous bound)
    counter.gets = 0
    full = store.query(Item, order_by=(Item.grade, "asc"))
    assert len(full) == 300
    assert counter.gets >= 300


def test_order_by_is_additive_default_none(items_store):
    # the surface stays frozen: no order_by behaves exactly as before (OID order)
    plain = [it.code for it in items_store.query(Item)]
    assert plain == [r[0] for r in _DATA]   # ascending OID == insertion order
