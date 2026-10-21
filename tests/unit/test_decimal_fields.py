"""``decimal.Decimal`` fields persist, compare and index exactly (#184).

Format v2 gains one extension code holding the Decimal's exact text, so an
appraisal amount of 72.46 stays 72.46 instead of coming back as ``str``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import field
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import pytest

import datacrystal as dc
import datacrystal._indexes as _idx
from datacrystal._records import decode_scalar_tree, encode_scalar_tree


@dc.entity
class Appraisal:
    """A cabinet appraisal of one specimen, with exact money and weight fields."""

    specimen: Annotated[str, dc.Unique]
    price: Decimal | None = None
    carats: Annotated[Decimal | None, dc.SortedIndex] = None
    tier: Annotated[Decimal | None, dc.Index] = None
    code: Annotated[Decimal | None, dc.Unique] = None
    lines: list = field(default_factory=list)


@dc.entity
class Scale:
    """A balance reading in float grams."""

    species: str
    reading: float | None = None


APP = dc.fields(Appraisal)
G = dc.fields(Scale)

StoreFactory = Callable[[], dc.Store]
FIELD_KINDS = pytest.mark.parametrize(
    "field_name", ["price", "carats", "tier"], ids=["unindexed", "sorted", "hash"]
)


def _specimens(appraisals: Iterable[Appraisal]) -> list[str]:
    return sorted(appraisal.specimen for appraisal in appraisals)


def _specimens_in_query_order(appraisals: Iterable[Appraisal]) -> list[str]:
    return [appraisal.specimen for appraisal in appraisals]


def _stock_range(store: dc.Store) -> None:
    """Store six appraisals a..f valued -5.50, 0, 72.46, 500, 500.01 and None."""
    values = [("a", "-5.50"), ("b", "0"), ("c", "72.46"), ("d", "500"), ("e", "500.01"), ("f", None)]
    for specimen, value in values:
        amount = None if value is None else Decimal(value)
        store.store(Appraisal(specimen=specimen, price=amount, carats=amount, tier=amount))


def _delete_index_sidecars(store_dir: Path) -> None:
    for sidecar in store_dir.iterdir():
        if "index" in sidecar.name and sidecar.is_file():
            sidecar.unlink()


def _counting_build_class_indexes(calls: list[int]) -> Callable[..., object]:
    real: Callable[..., object] = _idx.build_class_indexes

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)

    return counting


def _evolving_appraisal(carats_annotation: object) -> type:
    """Redefine 'EvolvingAppraisal' with the given `carats` annotation, under one typename.

    The same typename across runs simulates code changed between runs.
    """
    namespace: dict = {
        "__module__": __name__,
        "__qualname__": "EvolvingAppraisal",
        "__annotations__": {"carats": carats_annotation},
        "carats": None,
    }
    return dc.entity(type("EvolvingAppraisal", (), namespace))


# -- the record codec ------------------------------------------------------------


def test_decimal_round_trips_exactly_through_commit_and_reopen(store_factory: StoreFactory) -> None:
    """Given a stored appraisal priced Decimal("72.46")
    When the store is reopened and the appraisal is fetched
    Then its price equals Decimal("72.46")
    And the price is a Decimal
    """
    store = store_factory()
    store.store(Appraisal(specimen="a", price=Decimal("72.46")))
    store.commit()
    store.close()
    reopened = store_factory()

    back = reopened.get(Appraisal, specimen="a")

    assert back is not None
    assert back.price == Decimal("72.46")
    assert type(back.price) is Decimal
    reopened.close()


def test_decimal_sum_stays_exact_after_reopen(store_factory: StoreFactory) -> None:
    """Given stored appraisals priced Decimal("0.1") and Decimal("0.2")
    When the store is reopened and both prices are added
    Then the sum is exactly Decimal("0.3")
    """
    store = store_factory()
    store.store(Appraisal(specimen="a", price=Decimal("0.1")))
    store.store(Appraisal(specimen="b", price=Decimal("0.2")))
    store.commit()
    store.close()
    reopened = store_factory()
    a = reopened.get(Appraisal, specimen="a")
    b = reopened.get(Appraisal, specimen="b")
    assert a is not None
    assert b is not None
    assert a.price is not None
    assert b.price is not None

    total = a.price + b.price

    assert total == Decimal("0.3")
    reopened.close()


def test_a_none_decimal_round_trips_as_none(store_factory: StoreFactory) -> None:
    """Given a stored appraisal without a price
    When the store is reopened and the appraisal is fetched
    Then its price is None
    """
    store = store_factory()
    store.store(Appraisal(specimen="none", price=None))
    store.commit()
    store.close()
    reopened = store_factory()

    back = reopened.get(Appraisal, specimen="none")

    assert back is not None
    assert back.price is None
    reopened.close()


def test_a_negative_decimal_keeps_its_exponent(store_factory: StoreFactory) -> None:
    """Given a stored appraisal priced Decimal("-1234.5600")
    When the store is reopened and the appraisal is fetched
    Then its price reads "-1234.5600" with both trailing zeros
    """
    store = store_factory()
    store.store(Appraisal(specimen="neg", price=Decimal("-1234.5600")))
    store.commit()
    store.close()
    reopened = store_factory()

    back = reopened.get(Appraisal, specimen="neg")

    assert back is not None
    assert str(back.price) == "-1234.5600"
    reopened.close()


def test_a_high_precision_decimal_round_trips_exactly(store_factory: StoreFactory) -> None:
    """Given a stored appraisal priced with 20 integer digits and 18 decimals
    When the store is reopened and the appraisal is fetched
    Then its price equals the stored Decimal
    """
    store = store_factory()
    store.store(Appraisal(specimen="big", price=Decimal("12345678901234567890.123456789012345678")))
    store.commit()
    store.close()
    reopened = store_factory()

    back = reopened.get(Appraisal, specimen="big")

    assert back is not None
    assert back.price == Decimal("12345678901234567890.123456789012345678")
    reopened.close()


def test_decimal_inside_a_container_round_trips(store_factory: StoreFactory) -> None:
    """Given a stored appraisal whose lines list holds Decimal("1.10") and Decimal("2.20")
    When the store is reopened and the appraisal is fetched
    Then the lines equal the stored list
    And the first line is a Decimal
    """
    store = store_factory()
    store.store(Appraisal(specimen="a", lines=[Decimal("1.10"), Decimal("2.20")]))
    store.commit()
    store.close()
    reopened = store_factory()

    back = reopened.get(Appraisal, specimen="a")

    assert back is not None
    assert list(back.lines) == [Decimal("1.10"), Decimal("2.20")]
    assert type(back.lines[0]) is Decimal
    reopened.close()


def test_an_updated_decimal_replaces_the_stored_one(store_factory: StoreFactory) -> None:
    """Given a committed appraisal priced Decimal("1.00") whose price was changed to Decimal("2.50")
    When the changed appraisal is committed, and the store reopened
    Then its price equals Decimal("2.50")
    """
    store = store_factory()
    appraisal = Appraisal(specimen="a", price=Decimal("1.00"))
    store.store(appraisal)
    store.commit()
    appraisal.price = Decimal("2.50")
    store.commit()
    store.close()
    reopened = store_factory()

    back = reopened.get(Appraisal, specimen="a")

    assert back is not None
    assert back.price == Decimal("2.50")
    reopened.close()


# -- conditions ------------------------------------------------------------------


@FIELD_KINDS
def test_equality_with_a_decimal_literal_finds_the_exact_amount(
    store_factory: StoreFactory, field_name: str
) -> None:
    """Given appraisals a..f stocked from -5.50 to 500.01, queried on one decimal field
    When the field is compared equal to Decimal("72.46")
    Then only c matches
    """
    store = store_factory()
    _stock_range(store)
    store.commit()

    found = store.query(getattr(APP, field_name) == Decimal("72.46"))

    assert _specimens(found) == ["c"]
    store.close()


@FIELD_KINDS
def test_equality_with_an_int_literal_finds_the_exact_amount(
    store_factory: StoreFactory, field_name: str
) -> None:
    """Given appraisals a..f stocked from -5.50 to 500.01, queried on one decimal field
    When the field is compared equal to the int 500
    Then only d matches
    """
    store = store_factory()
    _stock_range(store)
    store.commit()

    found = store.query(getattr(APP, field_name) == 500)

    assert _specimens(found) == ["d"]
    store.close()


@FIELD_KINDS
def test_greater_than_a_decimal_literal_excludes_the_boundary(
    store_factory: StoreFactory, field_name: str
) -> None:
    """Given appraisals a..f stocked from -5.50 to 500.01, queried on one decimal field
    When the field is compared greater than Decimal("500")
    Then only e matches
    """
    store = store_factory()
    _stock_range(store)
    store.commit()

    found = store.query(getattr(APP, field_name) > Decimal(500))

    assert _specimens(found) == ["e"]
    store.close()


@FIELD_KINDS
def test_at_least_an_int_literal_includes_the_boundary(
    store_factory: StoreFactory, field_name: str
) -> None:
    """Given appraisals a..f stocked from -5.50 to 500.01, queried on one decimal field
    When the field is compared at least the int 500
    Then d and e match
    """
    store = store_factory()
    _stock_range(store)
    store.commit()

    found = store.query(getattr(APP, field_name) >= 500)

    assert _specimens(found) == ["d", "e"]
    store.close()


@FIELD_KINDS
def test_less_than_an_int_literal_finds_only_negative_amounts(
    store_factory: StoreFactory, field_name: str
) -> None:
    """Given appraisals a..f stocked from -5.50 to 500.01, queried on one decimal field
    When the field is compared less than the int 0
    Then only a matches
    """
    store = store_factory()
    _stock_range(store)
    store.commit()

    found = store.query(getattr(APP, field_name) < 0)

    assert _specimens(found) == ["a"]
    store.close()


@FIELD_KINDS
def test_at_most_a_decimal_literal_includes_the_boundary(
    store_factory: StoreFactory, field_name: str
) -> None:
    """Given appraisals a..f stocked from -5.50 to 500.01, queried on one decimal field
    When the field is compared at most Decimal("0")
    Then a and b match
    """
    store = store_factory()
    _stock_range(store)
    store.commit()

    found = store.query(getattr(APP, field_name) <= Decimal(0))

    assert _specimens(found) == ["a", "b"]
    store.close()


@FIELD_KINDS
def test_in_with_mixed_decimal_and_int_literals_matches_both(
    store_factory: StoreFactory, field_name: str
) -> None:
    """Given appraisals a..f stocked from -5.50 to 500.01, queried on one decimal field
    When the field is tested for membership in [Decimal("72.46"), 0]
    Then b and c match
    """
    store = store_factory()
    _stock_range(store)
    store.commit()

    found = store.query(getattr(APP, field_name).in_([Decimal("72.46"), 0]))

    assert _specimens(found) == ["b", "c"]
    store.close()


def test_price_equality_ignores_the_exponent(store_factory: StoreFactory) -> None:
    """Given an appraisal priced Decimal("1.50")
    When the unindexed price is compared equal to Decimal("1.5")
    Then the appraisal matches
    """
    store = store_factory()
    store.store(Appraisal(specimen="a", price=Decimal("1.50"), tier=Decimal("1.50")))
    store.commit()

    found = store.query(APP.price == Decimal("1.5"))

    assert _specimens(found) == ["a"]
    store.close()


def test_tier_equality_ignores_the_exponent(store_factory: StoreFactory) -> None:
    """Given an appraisal with tier Decimal("1.50")
    When the hash-indexed tier is compared equal to Decimal("1.5")
    Then the appraisal matches
    """
    store = store_factory()
    store.store(Appraisal(specimen="a", price=Decimal("1.50"), tier=Decimal("1.50")))
    store.commit()

    found = store.query(APP.tier == Decimal("1.5"))

    assert _specimens(found) == ["a"]
    store.close()


def test_a_float_in_a_range_condition_is_refused_naming_the_field() -> None:
    """Given the Decimal field price
    When it is compared greater than the float 500.0
    Then TypeError names Appraisal.price as a Decimal field
    """
    with pytest.raises(TypeError, match=r"Appraisal\.price is a Decimal field"):
        APP.price > 500.0  # noqa: B015  # building the condition is the act under test


def test_a_float_in_an_equality_condition_is_refused_naming_the_field() -> None:
    """Given the Decimal field carats
    When it is compared equal to the float 0.1
    Then TypeError names Appraisal.carats as a Decimal field
    """
    with pytest.raises(TypeError, match=r"Appraisal\.carats is a Decimal field"):
        APP.carats == 0.1  # noqa: B015  # building the condition is the act under test


def test_a_float_in_an_in_condition_is_refused_naming_the_field() -> None:
    """Given the Decimal field tier
    When it is tested for membership in [Decimal("1"), 0.5]
    Then TypeError names Appraisal.tier as a Decimal field
    """
    with pytest.raises(TypeError, match=r"Appraisal\.tier is a Decimal field"):
        APP.tier.in_([Decimal(1), 0.5])


def test_a_float_literal_against_a_float_field_is_still_fine() -> None:
    """Given the float field Scale.reading
    When it is compared greater than the float 0.5
    Then the condition reads "(Scale.reading > 0.5)"
    """
    condition = G.reading > 0.5

    assert repr(condition) == "(Scale.reading > 0.5)"


# -- index kinds -----------------------------------------------------------------


def test_sorted_index_answers_a_decimal_range(store_factory: StoreFactory) -> None:
    """Given appraisals a..f stocked with carats from -5.50 to 500.01
    When carats are compared at least Decimal("72.46")
    Then c, d and e match
    """
    store = store_factory()
    _stock_range(store)
    store.commit()

    found = store.query(APP.carats >= Decimal("72.46"))

    assert _specimens(found) == ["c", "d", "e"]
    store.close()


def test_sorted_index_answers_a_two_sided_decimal_range(store_factory: StoreFactory) -> None:
    """Given appraisals a..f stocked with carats from -5.50 to 500.01
    When carats are compared above 0 and below 500
    Then only c matches
    """
    store = store_factory()
    _stock_range(store)
    store.commit()

    found = store.query((APP.carats > 0) & (APP.carats < 500))

    assert _specimens(found) == ["c"]
    store.close()


def test_sorted_index_orders_descending_by_decimal_value(store_factory: StoreFactory) -> None:
    """Given appraisals a..f stocked with carats from -5.50 to 500.01
    When they are queried ordered by carats descending
    Then the first three are e, d, c
    """
    store = store_factory()
    _stock_range(store)
    store.commit()

    ordered = store.query(Appraisal, order_by=(APP.carats, "desc"))

    assert _specimens_in_query_order(ordered)[:3] == ["e", "d", "c"]
    store.close()


def test_a_decimal_range_on_the_sorted_index_leaves_no_residual(store_factory: StoreFactory) -> None:
    """Given appraisals a..f stocked with carats from -5.50 to 500.01
    When the plan for carats at least Decimal("72.46") is explained
    Then the plan has no residual filter
    """
    store = store_factory()
    _stock_range(store)
    store.commit()

    plan = store.explain(APP.carats >= Decimal("72.46"))

    assert plan.residual is None
    store.close()


def test_unique_decimal_rejects_an_equal_value_with_another_exponent(
    store_factory: StoreFactory,
) -> None:
    """Given a committed appraisal with code Decimal("1.0")
    When an appraisal with code Decimal("1.00") is committed
    Then the commit raises UniqueViolationError
    """
    store = store_factory()
    store.store(Appraisal(specimen="a", code=Decimal("1.0")))
    store.commit()
    store.store(Appraisal(specimen="b", code=Decimal("1.00")))

    with pytest.raises(dc.UniqueViolationError):
        store.commit()
    store.close()


def test_unique_decimal_looks_up_by_key_after_reopen(store_factory: StoreFactory) -> None:
    """Given a stored appraisal with code Decimal("9.99")
    When the store is reopened and an appraisal is fetched by that code
    Then it is appraisal a
    """
    store = store_factory()
    store.store(Appraisal(specimen="a", code=Decimal("9.99")))
    store.commit()
    store.close()
    reopened = store_factory()

    found = reopened.get(Appraisal, code=Decimal("9.99"))

    assert found is not None
    assert found.specimen == "a"
    reopened.close()


def test_decimal_nan_is_refused_in_a_sorted_index_before_anything_is_written(
    store_factory: StoreFactory,
) -> None:
    """Given a committed appraisal and a new appraisal whose carats are Decimal("NaN")
    When the new appraisal is committed
    Then the commit raises ValueError saying a SortedIndex cannot hold Decimal NaN
    """
    store = store_factory()
    store.store(Appraisal(specimen="a", carats=Decimal(1)))
    store.commit()
    store.store(Appraisal(specimen="b", carats=Decimal("NaN")))

    with pytest.raises(ValueError, match=r"Appraisal\.carats .* cannot hold Decimal NaN"):
        store.commit()
    store.close()


def test_a_decimal_nan_already_on_disk_is_refused_when_its_field_becomes_a_sorted_index(
    store_factory: StoreFactory,
) -> None:
    """Given a stored Decimal NaN in a field that was unindexed when it was written
    When the field is redeclared a SortedIndex and queried after reopen
    Then the query raises ValueError saying a SortedIndex cannot hold Decimal NaN
    """
    unindexed = _evolving_appraisal(Decimal | None)
    store = store_factory()
    store.root = [unindexed(carats=Decimal(1)), unindexed(carats=Decimal("NaN"))]
    store.commit()
    store.close()
    indexed = _evolving_appraisal(Annotated[Decimal | None, dc.SortedIndex])
    reopened = store_factory()

    with pytest.raises(ValueError, match=r"carats is a SortedIndex field and cannot hold Decimal NaN"):
        reopened.query(indexed)
    reopened.close()


# -- the on-disk index cache (ADR-005) ---------------------------------------------


def test_cache_codec_keeps_decimal_keys_as_decimals() -> None:
    """Given a cache tree holding Decimal keys
    When it is encoded and decoded
    Then the keys equal the originals
    And each key is a Decimal
    """
    keys = [Decimal("-1.50"), Decimal(0), Decimal("72.46")]

    back = decode_scalar_tree(encode_scalar_tree({"classes": {"x": keys}}))

    assert back["classes"]["x"] == keys
    assert all(type(k) is Decimal for k in back["classes"]["x"])


def _stock_and_cache_indexes(path: Path) -> None:
    store = dc.Store.open(path, cache_index=True)
    _stock_range(store)
    store.commit()
    store.count(APP.carats >= 500)
    store.close()


def test_cached_sorted_index_answers_a_decimal_range_after_reopen(tmp_path: Path) -> None:
    """Given a store whose decimal indexes were cached on close
    When it is reopened and carats are compared at least Decimal("72.46")
    Then c, d and e match
    """
    _stock_and_cache_indexes(tmp_path / "cabinet")
    reopened = dc.Store.open(tmp_path / "cabinet", cache_index=True)

    found = reopened.query(APP.carats >= Decimal("72.46"))

    assert _specimens(found) == ["c", "d", "e"]
    reopened.close()


def test_cached_hash_index_answers_a_decimal_equality_after_reopen(tmp_path: Path) -> None:
    """Given a store whose decimal indexes were cached on close
    When it is reopened and tier is compared equal to Decimal("-5.50")
    Then only a matches
    """
    _stock_and_cache_indexes(tmp_path / "cabinet")
    reopened = dc.Store.open(tmp_path / "cabinet", cache_index=True)

    found = reopened.query(APP.tier == Decimal("-5.50"))

    assert _specimens(found) == ["a"]
    reopened.close()


def test_cached_sorted_index_orders_ascending_after_reopen(tmp_path: Path) -> None:
    """Given a store whose decimal indexes were cached on close
    When it is reopened and appraisals are queried ordered by carats ascending
    Then the first two are a and b
    """
    _stock_and_cache_indexes(tmp_path / "cabinet")
    reopened = dc.Store.open(tmp_path / "cabinet", cache_index=True)

    ordered = reopened.query(Appraisal, order_by=(APP.carats, "asc"))

    assert _specimens_in_query_order(ordered)[:2] == ["a", "b"]
    reopened.close()


def test_decimal_indexes_are_reconstructed_from_the_cache_not_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a store whose decimal indexes were cached on close
    When it is reopened and a carats range is queried
    Then no class index is built again
    """
    _stock_and_cache_indexes(tmp_path / "cabinet")
    built: list[int] = []
    monkeypatch.setattr(_idx, "build_class_indexes", _counting_build_class_indexes(built))
    reopened = dc.Store.open(tmp_path / "cabinet", cache_index=True)

    reopened.query(APP.carats >= Decimal("72.46"))

    assert built == []
    reopened.close()


def test_decimal_index_rebuilds_from_disk_when_the_cache_is_gone(tmp_path: Path) -> None:
    """Given a stored cabinet whose index sidecars were deleted
    When it is reopened and carats are compared above Decimal("0")
    Then c, d and e match
    """
    _stock_and_cache_indexes(tmp_path / "cabinet")
    _delete_index_sidecars(tmp_path / "cabinet")
    reopened = dc.Store.open(tmp_path / "cabinet", cache_index=True)

    found = reopened.query(APP.carats > Decimal(0))

    assert _specimens(found) == ["c", "d", "e"]
    reopened.close()
