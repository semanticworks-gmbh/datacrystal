"""``decimal.Decimal`` fields persist, compare and index exactly (#184).

Format v2 gains one extension code holding the Decimal's exact text, so an
appraisal amount of 72.46 stays 72.46 instead of coming back as ``str``.
"""

from __future__ import annotations

from dataclasses import field
from decimal import Decimal
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._records import decode_scalar_tree, encode_scalar_tree


@dc.entity
class Appraisal:
    specimen: Annotated[str, dc.Unique]
    price: Decimal | None = None
    carats: Annotated[Decimal | None, dc.SortedIndex] = None
    tier: Annotated[Decimal | None, dc.Index] = None
    code: Annotated[Decimal | None, dc.Unique] = None
    lines: list = field(default_factory=list)


@dc.entity
class Scale:
    species: str
    reading: float | None = None


APP = dc.fields(Appraisal)
G = dc.fields(Scale)


def _numbers(store, condition) -> list[str]:
    return sorted(appraisal.specimen for appraisal in store.query(condition))


def _stock_range(store) -> None:
    for specimen, value in [("a", "-5.50"), ("b", "0"), ("c", "72.46"),
                          ("d", "500"), ("e", "500.01"), ("f", None)]:
        amount = None if value is None else Decimal(value)
        store.store(Appraisal(specimen=specimen, price=amount, carats=amount, tier=amount))
    store.commit()


# -- the record codec ------------------------------------------------------------


def test_decimal_round_trips_exactly_through_commit_and_reopen(store_factory):
    store = store_factory()
    store.store(Appraisal(specimen="a", price=Decimal("72.46")))
    store.commit()
    store.close()

    reopened = store_factory()
    back = reopened.get(Appraisal, specimen="a")
    assert back.price == Decimal("72.46")
    assert type(back.price) is Decimal
    reopened.close()


def test_decimal_sum_stays_exact_after_reopen(store_factory):
    store = store_factory()
    store.store(Appraisal(specimen="a", price=Decimal("0.1")))
    store.store(Appraisal(specimen="b", price=Decimal("0.2")))
    store.commit()
    store.close()

    reopened = store_factory()
    a = reopened.get(Appraisal, specimen="a")
    b = reopened.get(Appraisal, specimen="b")
    assert a.price + b.price == Decimal("0.3")
    reopened.close()


def test_none_negative_and_high_precision_values_round_trip(store_factory):
    store = store_factory()
    store.store(Appraisal(specimen="none", price=None))
    store.store(Appraisal(specimen="neg", price=Decimal("-1234.5600")))
    store.store(Appraisal(specimen="big", price=Decimal("12345678901234567890.123456789012345678")))
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.get(Appraisal, specimen="none").price is None
    assert str(reopened.get(Appraisal, specimen="neg").price) == "-1234.5600"  # exponent kept
    assert reopened.get(Appraisal, specimen="big").price == Decimal(
        "12345678901234567890.123456789012345678")
    reopened.close()


def test_decimal_inside_a_container_round_trips(store_factory):
    store = store_factory()
    store.store(Appraisal(specimen="a", lines=[Decimal("1.10"), Decimal("2.20")]))
    store.commit()
    store.close()

    reopened = store_factory()
    back = reopened.get(Appraisal, specimen="a")
    assert list(back.lines) == [Decimal("1.10"), Decimal("2.20")]
    assert type(back.lines[0]) is Decimal
    reopened.close()


def test_an_updated_decimal_replaces_the_stored_one(store_factory):
    store = store_factory()
    appraisal = Appraisal(specimen="a", price=Decimal("1.00"))
    store.store(appraisal)
    store.commit()
    appraisal.price = Decimal("2.50")
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.get(Appraisal, specimen="a").price == Decimal("2.50")
    reopened.close()


# -- conditions ------------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["price", "carats", "tier"],
                         ids=["unindexed", "sorted", "hash"])
def test_conditions_compare_with_decimal_and_int_literals(store_factory, field_name):
    store = store_factory()
    _stock_range(store)
    column = getattr(APP, field_name)

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
    store.store(Appraisal(specimen="a", price=Decimal("1.50"), tier=Decimal("1.50")))
    store.commit()

    assert _numbers(store, APP.price == Decimal("1.5")) == ["a"]
    assert _numbers(store, APP.tier == Decimal("1.5")) == ["a"]
    store.close()


@pytest.mark.parametrize("build", [
    lambda: APP.price > 500.0,
    lambda: APP.carats == 0.1,
    lambda: APP.tier.in_([Decimal("1"), 0.5]),
], ids=["range", "equality", "in"])
def test_a_float_literal_against_a_decimal_field_is_refused_naming_the_field(build):
    with pytest.raises(TypeError, match=r"Appraisal\.(price|carats|tier) is a Decimal field"):
        build()


def test_a_float_literal_against_a_float_field_is_still_fine():
    assert repr(G.reading > 0.5) == "(Scale.reading > 0.5)"


# -- index kinds -----------------------------------------------------------------


def test_sorted_index_answers_a_decimal_range_from_the_index(store_factory):
    store = store_factory()
    _stock_range(store)

    assert _numbers(store, APP.carats >= Decimal("72.46")) == ["c", "d", "e"]
    assert _numbers(store, (APP.carats > 0) & (APP.carats < 500)) == ["c"]
    assert [i.specimen for i in store.query(Appraisal, order_by=(APP.carats, "desc"))][:3] == [
        "e", "d", "c"]
    assert store.explain(APP.carats >= Decimal("72.46")).residual is None
    store.close()


def test_unique_decimal_rejects_an_equal_value_with_another_exponent(store_factory):
    store = store_factory()
    store.store(Appraisal(specimen="a", code=Decimal("1.0")))
    store.commit()
    store.store(Appraisal(specimen="b", code=Decimal("1.00")))

    with pytest.raises(dc.UniqueViolationError):
        store.commit()
    store.close()


def test_unique_decimal_looks_up_by_key_after_reopen(store_factory):
    store = store_factory()
    store.store(Appraisal(specimen="a", code=Decimal("9.99")))
    store.commit()
    store.close()

    reopened = store_factory()
    assert reopened.get(Appraisal, code=Decimal("9.99")).specimen == "a"
    reopened.close()


def test_decimal_nan_is_refused_in_a_sorted_index_before_anything_is_written(store_factory):
    store = store_factory()
    store.store(Appraisal(specimen="a", carats=Decimal("1")))
    store.commit()
    store.store(Appraisal(specimen="b", carats=Decimal("NaN")))

    with pytest.raises(ValueError, match=r"Appraisal\.carats .* cannot hold Decimal NaN"):
        store.commit()
    store.close()


def _evolving_appraisal(gross_annotation):
    """Redefine 'EvolvingAppraisal' with a given annotation for `carats`, the same
    typename across runs (the code-changed-between-runs simulation)."""
    namespace: dict = {
        "__module__": __name__,
        "__qualname__": "EvolvingAppraisal",
        "__annotations__": {"carats": gross_annotation},
        "carats": None,
    }
    return dc.entity(type("EvolvingAppraisal", (), namespace))


def test_a_decimal_nan_already_on_disk_is_refused_when_its_field_becomes_a_sorted_index(
        store_factory):
    unindexed = _evolving_appraisal(Decimal | None)
    store = store_factory()
    store.root = [unindexed(carats=Decimal("1")), unindexed(carats=Decimal("NaN"))]
    store.commit()
    store.close()

    indexed = _evolving_appraisal(Annotated[Decimal | None, dc.SortedIndex])
    reopened = store_factory()

    with pytest.raises(ValueError, match=r"carats is a SortedIndex field and cannot hold Decimal NaN"):
        reopened.query(indexed).where(dc.fields(indexed).carats >= Decimal("0")).all()
    reopened.close()


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
    assert s.count(APP.carats >= 500) == 2  # builds + caches the indexes
    s.close()

    built: list[int] = []
    real = _idx.build_class_indexes
    monkeypatch.setattr(_idx, "build_class_indexes",
                        lambda *a, **k: built.append(1) or real(*a, **k))
    s2 = dc.Store.open(path, cache_index=True)
    assert _numbers(s2, APP.carats >= Decimal("72.46")) == ["c", "d", "e"]
    assert _numbers(s2, APP.tier == Decimal("-5.50")) == ["a"]
    assert [i.specimen for i in s2.query(Appraisal, order_by=(APP.carats, "asc"))][:2] == ["a", "b"]
    assert built == []  # reconstructed from the cache, not rebuilt
    s2.close()


def test_decimal_index_rebuilds_from_disk_when_the_cache_is_gone(tmp_path):
    path = tmp_path / "cabinet"
    s = dc.Store.open(path, cache_index=True)
    _stock_range(s)
    assert s.count(APP.carats >= 500) == 2
    s.close()
    for sidecar in path.iterdir():
        if "index" in sidecar.name and sidecar.is_file():
            sidecar.unlink()

    s2 = dc.Store.open(path, cache_index=True)
    assert _numbers(s2, APP.carats > Decimal("0")) == ["c", "d", "e"]
    s2.close()
