"""``decimal.Decimal`` fields persist, compare and index exactly (#184).

Format v2 gains one extension code holding the Decimal's exact text, so an
invoice amount of 72.46 stays 72.46 instead of coming back as ``str``.
"""

from __future__ import annotations

from dataclasses import field
from decimal import Decimal
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._records import decode_scalar_tree, encode_scalar_tree


@dc.entity
class Invoice:
    number: Annotated[str, dc.Unique]
    net: Decimal | None = None
    gross: Annotated[Decimal | None, dc.SortedIndex] = None
    tier: Annotated[Decimal | None, dc.Index] = None
    code: Annotated[Decimal | None, dc.Unique] = None
    lines: list = field(default_factory=list)


@dc.entity
class Gauge:
    label: str
    reading: float | None = None


INV = dc.fields(Invoice)
G = dc.fields(Gauge)


def _numbers(store, condition) -> list[str]:
    return sorted(inv.number for inv in store.query(condition))


def _stock_range(store) -> None:
    for number, value in [("a", "-5.50"), ("b", "0"), ("c", "72.46"),
                          ("d", "500"), ("e", "500.01"), ("f", None)]:
        amount = None if value is None else Decimal(value)
        store.store(Invoice(number=number, net=amount, gross=amount, tier=amount))
    store.commit()


# -- the record codec ------------------------------------------------------------


def test_decimal_round_trips_exactly_through_commit_and_reopen(store_factory):
    store = store_factory()
    store.store(Invoice(number="a", net=Decimal("72.46")))
    store.commit()
    store.close()

    reopened = store_factory()
    back = reopened.get(Invoice, number="a")
    assert back.net == Decimal("72.46")
    assert type(back.net) is Decimal
    reopened.close()


def test_decimal_sum_stays_exact_after_reopen(store_factory):
    store = store_factory()
    store.store(Invoice(number="a", net=Decimal("0.1")))
    store.store(Invoice(number="b", net=Decimal("0.2")))
    store.commit()
    store.close()

    reopened = store_factory()
    a = reopened.get(Invoice, number="a")
    b = reopened.get(Invoice, number="b")
    assert a.net + b.net == Decimal("0.3")
    reopened.close()


def test_none_negative_and_high_precision_values_round_trip(store_factory):
    store = store_factory()
    store.store(Invoice(number="none", net=None))
    store.store(Invoice(number="neg", net=Decimal("-1234.5600")))
    store.store(Invoice(number="big", net=Decimal("12345678901234567890.123456789012345678")))
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.get(Invoice, number="none").net is None
    assert str(reopened.get(Invoice, number="neg").net) == "-1234.5600"  # exponent kept
    assert reopened.get(Invoice, number="big").net == Decimal(
        "12345678901234567890.123456789012345678")
    reopened.close()


def test_decimal_inside_a_container_round_trips(store_factory):
    store = store_factory()
    store.store(Invoice(number="a", lines=[Decimal("1.10"), Decimal("2.20")]))
    store.commit()
    store.close()

    reopened = store_factory()
    back = reopened.get(Invoice, number="a")
    assert list(back.lines) == [Decimal("1.10"), Decimal("2.20")]
    assert type(back.lines[0]) is Decimal
    reopened.close()


def test_an_updated_decimal_replaces_the_stored_one(store_factory):
    store = store_factory()
    invoice = Invoice(number="a", net=Decimal("1.00"))
    store.store(invoice)
    store.commit()
    invoice.net = Decimal("2.50")
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.get(Invoice, number="a").net == Decimal("2.50")
    reopened.close()


# -- conditions ------------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["net", "gross", "tier"],
                         ids=["unindexed", "sorted", "hash"])
def test_conditions_compare_with_decimal_and_int_literals(store_factory, field_name):
    store = store_factory()
    _stock_range(store)
    column = getattr(INV, field_name)

    assert _numbers(store, column == Decimal("72.46")) == ["c"]
    assert _numbers(store, column == 500) == ["d"]
    assert _numbers(store, column > Decimal("500")) == ["e"]
    assert _numbers(store, column >= 500) == ["d", "e"]
    assert _numbers(store, column < 0) == ["a"]
    assert _numbers(store, column <= Decimal("0")) == ["a", "b"]
    assert _numbers(store, column.in_([Decimal("72.46"), 0])) == ["b", "c"]
    store.close()


def test_equal_decimals_with_different_exponents_match(store_factory):
    store = store_factory()
    store.store(Invoice(number="a", net=Decimal("1.50"), tier=Decimal("1.50")))
    store.commit()

    assert _numbers(store, INV.net == Decimal("1.5")) == ["a"]
    assert _numbers(store, INV.tier == Decimal("1.5")) == ["a"]
    store.close()


@pytest.mark.parametrize("build", [
    lambda: INV.net > 500.0,
    lambda: INV.gross == 0.1,
    lambda: INV.tier.in_([Decimal("1"), 0.5]),
], ids=["range", "equality", "in"])
def test_a_float_literal_against_a_decimal_field_is_refused_naming_the_field(build):
    with pytest.raises(TypeError, match=r"Invoice\.(net|gross|tier) is a Decimal field"):
        build()


def test_a_float_literal_against_a_float_field_is_still_fine():
    assert repr(G.reading > 0.5) == "(Gauge.reading > 0.5)"


# -- index kinds -----------------------------------------------------------------


def test_sorted_index_answers_a_decimal_range_from_the_index(store_factory):
    store = store_factory()
    _stock_range(store)

    assert _numbers(store, INV.gross >= Decimal("72.46")) == ["c", "d", "e"]
    assert _numbers(store, (INV.gross > 0) & (INV.gross < 500)) == ["c"]
    assert [i.number for i in store.query(Invoice, order_by=(INV.gross, "desc"))][:3] == [
        "e", "d", "c"]
    assert store.explain(INV.gross >= Decimal("72.46")).residual is None
    store.close()


def test_unique_decimal_rejects_an_equal_value_with_another_exponent(store_factory):
    store = store_factory()
    store.store(Invoice(number="a", code=Decimal("1.0")))
    store.commit()
    store.store(Invoice(number="b", code=Decimal("1.00")))

    with pytest.raises(dc.UniqueViolationError):
        store.commit()
    store.close()


def test_unique_decimal_looks_up_by_key_after_reopen(store_factory):
    store = store_factory()
    store.store(Invoice(number="a", code=Decimal("9.99")))
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.get(Invoice, code=Decimal("9.99")).number == "a"
    reopened.close()


def test_decimal_nan_is_refused_in_a_sorted_index_before_anything_is_written(store_factory):
    store = store_factory()
    store.store(Invoice(number="a", gross=Decimal("1")))
    store.commit()
    store.store(Invoice(number="b", gross=Decimal("NaN")))

    with pytest.raises(ValueError, match=r"Invoice\.gross .* cannot hold Decimal NaN"):
        store.commit()
    store.close()


# -- the on-disk index cache (ADR-005) ---------------------------------------------


def test_cache_codec_keeps_decimal_keys_as_decimals():
    keys = [Decimal("-1.50"), Decimal("0"), Decimal("72.46")]

    back = decode_scalar_tree(encode_scalar_tree({"classes": {"x": keys}}))

    assert back["classes"]["x"] == keys
    assert all(type(k) is Decimal for k in back["classes"]["x"])


def test_decimal_index_is_served_from_the_cache_after_reopen(tmp_path, monkeypatch):
    import datacrystal._indexes as _idx

    path = tmp_path / "cabinet"
    s = dc.Store.open(path, cache_index=True)
    _stock_range(s)
    assert s.count(INV.gross >= 500) == 2  # builds + caches the indexes
    s.close()

    built: list[int] = []
    real = _idx.build_class_indexes
    monkeypatch.setattr(_idx, "build_class_indexes",
                        lambda *a, **k: built.append(1) or real(*a, **k))
    s2 = dc.Store.open(path, cache_index=True)
    assert _numbers(s2, INV.gross >= Decimal("72.46")) == ["c", "d", "e"]
    assert _numbers(s2, INV.tier == Decimal("-5.50")) == ["a"]
    assert [i.number for i in s2.query(Invoice, order_by=(INV.gross, "asc"))][:2] == ["a", "b"]
    assert built == []  # reconstructed from the cache, not rebuilt
    s2.close()


def test_decimal_index_rebuilds_from_disk_when_the_cache_is_gone(tmp_path):
    path = tmp_path / "cabinet"
    s = dc.Store.open(path, cache_index=True)
    _stock_range(s)
    assert s.count(INV.gross >= 500) == 2
    s.close()
    for sidecar in path.iterdir():
        if "index" in sidecar.name and sidecar.is_file():
            sidecar.unlink()

    s2 = dc.Store.open(path, cache_index=True)
    assert _numbers(s2, INV.gross > Decimal("0")) == ["c", "d", "e"]
    s2.close()
