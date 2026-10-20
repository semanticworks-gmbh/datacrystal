"""`dc.SortedIndex` — range queries from a sorted index (ADR-004 / #18).

A SortedIndex field answers `>=`/`>`/`<=`/`<` (and `between` as their And) from
an in-memory sorted run, not a full-extent scan, and still answers `==` as a
point lookup. Expected results are stated as literals, over both backends.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Annotated

import pytest

import datacrystal as dc

StoreFactory = Callable[[], dc.Store]


@dc.entity
class Specimen:
    """A cabinet specimen with range-queryable mass and year."""

    label: Annotated[str, dc.Unique]
    mass_g: Annotated[float, dc.SortedIndex] = 0.0  # range-queryable
    year: Annotated[int | None, dc.SortedIndex] = None  # range + None
    quality: Annotated[str | None, dc.Index] = None  # plain bitmap (compound)


@dc.entity(frozen=True)
class CatalogEvent:
    """A frozen cabinet event whose acquisition timestamp is a SortedIndex datetime key (#106).

    It is the eval's actual need (newest-N on a `published` timestamp). Aware datetimes ride
    msgspec's timestamp ext as a UTC instant (_records.py); ``at=None`` is an event with no
    recorded time.
    """

    seq: Annotated[int, dc.Unique]
    kind: Annotated[str, dc.Index] = "acquire"
    at: Annotated[datetime | None, dc.SortedIndex] = None


F = dc.fields(Specimen)
EF = dc.fields(CatalogEvent)
_BASE = datetime(2021, 1, 1, 9, 0, tzinfo=UTC)


def _labels(specimens: Iterable[Specimen]) -> str:
    return ", ".join(sorted(specimen.label for specimen in specimens))


def _seqs(events: Iterable[CatalogEvent]) -> str:
    return ", ".join(str(seq) for seq in sorted(event.seq for event in events))


def _seqs_in_query_order(events: Iterable[CatalogEvent]) -> list[int]:
    return [event.seq for event in events]


def _ten_specimens_and_undated() -> list[Specimen]:
    specs = [Specimen(label=f"S{i}", mass_g=float(i * 10)) for i in range(10)]
    specs.append(Specimen(label="undated", mass_g=55.0))
    return specs


@pytest.fixture
def specimens(store: dc.Store) -> list[Specimen]:
    """Return ten specimens S0..S9 (mass i*10 g, year 2000+i, odd ones fine) plus 'undated'.

    'undated' weighs 55 g, has year None and is fine. All are committed.
    """
    specs = [
        Specimen(
            label=f"S{i}",
            mass_g=float(i * 10),
            year=2000 + i,
            quality="fine" if i % 2 else "rough",
        )
        for i in range(10)
    ]
    specs.append(Specimen(label="undated", mass_g=55.0, year=None, quality="fine"))
    for specimen in specs:
        store.store(specimen)
    store.commit()
    return specs


def test_range_ge_returns_the_heavy_specimens(store: dc.Store, specimens: list[Specimen]) -> None:
    """Given specimens S0..S9 of 0..90 g and 'undated' of 55 g
    When specimens of at least 50 g are queried
    Then they are S5..S9 and undated
    """
    hits = store.query(F.mass_g >= 50.0)

    assert _labels(hits) == "S5, S6, S7, S8, S9, undated"


@pytest.mark.parametrize(
    ("operator", "expected"),
    [
        ("__gt__", "S6, S7, S8, S9, undated"),
        ("__ge__", "S5, S6, S7, S8, S9, undated"),
        ("__lt__", "S0, S1, S2, S3, S4"),
        ("__le__", "S0, S1, S2, S3, S4, S5"),
    ],
    ids=["gt", "ge", "lt", "le"],
)
def test_each_range_operator_matches_the_expected_specimens(
    store: dc.Store, specimens: list[Specimen], operator: str, expected: str
) -> None:
    """Given specimens S0..S9 of 0..90 g and 'undated' of 55 g
    When the mass is compared with one range operator against 50 g
    Then exactly the expected specimens match
    """
    hits = store.query(getattr(F.mass_g, operator)(50.0))

    assert _labels(hits) == expected


def test_between_via_and_returns_the_specimens_in_the_band(
    store: dc.Store, specimens: list[Specimen]
) -> None:
    """Given specimens S0..S9 of 0..90 g and 'undated' of 55 g
    When specimens of at least 30 g and at most 70 g are queried
    Then they are S3..S7 and undated
    """
    hits = store.query((F.mass_g >= 30.0) & (F.mass_g <= 70.0))

    assert _labels(hits) == "S3, S4, S5, S6, S7, undated"


def test_point_lookup_on_a_sorted_field_finds_the_specimen(
    store: dc.Store, specimens: list[Specimen]
) -> None:
    """Given specimens S0..S9 of 0..90 g
    When specimens of exactly 30 g are queried
    Then only S3 matches
    """
    hits = store.query(F.mass_g == 30.0)

    assert _labels(hits) == "S3"


def test_point_lookup_on_a_sorted_field_is_answered_from_the_index(
    store: dc.Store, specimens: list[Specimen]
) -> None:
    """Given specimens S0..S9 of 0..90 g
    When the plan for exactly 30 g is explained
    Then the plan is indexed, not a residual
    """
    plan = store.explain(F.mass_g == 30.0)

    assert plan.indexed


def test_none_never_matches_a_range(store: dc.Store, specimens: list[Specimen]) -> None:
    """Given S0..S9 dated 2000..2009 and 'undated' with year None
    When specimens from the year 2000 on are queried
    Then they are S0..S9, without undated
    """
    hits = store.query(F.year >= 2000)

    assert _labels(hits) == "S0, S1, S2, S3, S4, S5, S6, S7, S8, S9"


def test_compound_sorted_and_bitmap_condition(store: dc.Store, specimens: list[Specimen]) -> None:
    """Given specimens S0..S9, odd ones fine, and 'undated' of 55 g and fine
    When fine specimens of at least 40 g are queried
    Then they are S5, S7, S9 and undated
    """
    hits = store.query((F.mass_g >= 40.0) & (F.quality == "fine"))

    assert _labels(hits) == "S5, S7, S9, undated"


def test_range_is_index_backed_with_no_residual_scan(
    store: dc.Store, specimens: list[Specimen]
) -> None:
    """Given specimens S0..S9 of 0..90 g and 'undated' of 55 g
    When the plan for at least 50 g is explained
    Then the plan is indexed
    And it has no Python residual scan
    And it considers fewer candidates than the extent
    """
    plan = store.explain(F.mass_g >= 50.0)

    assert plan.indexed
    assert plan.residual is None
    assert plan.candidates < plan.extent


def test_nothing_reaches_a_kilogram_before_the_update(
    store: dc.Store, specimens: list[Specimen]
) -> None:
    """Given specimens S0..S9 of 0..90 g and 'undated' of 55 g
    When specimens of at least 1000 g are queried
    Then none matches
    """
    hits = store.query(F.mass_g >= 1000.0)

    assert _labels(hits) == ""


def test_updating_a_mass_moves_the_specimen_into_the_range(
    store: dc.Store, specimens: list[Specimen]
) -> None:
    """Given S0 of 0 g, committed
    When S0's mass is set to 5000 g and committed
    Then specimens of at least 1000 g are S0
    """
    specimens[0].mass_g = 5000.0
    store.commit()

    hits = store.query(F.mass_g >= 1000.0)

    assert _labels(hits) == "S0"


def test_updating_a_mass_drops_the_old_key(store: dc.Store, specimens: list[Specimen]) -> None:
    """Given S0 of 0 g, committed
    When S0's mass is set to 5000 g and committed
    Then specimens of exactly 0 g are none
    """
    specimens[0].mass_g = 5000.0
    store.commit()

    hits = store.query(F.mass_g == 0.0)

    assert _labels(hits) == ""


def test_s9_is_in_the_80_g_range_before_the_delete(
    store: dc.Store, specimens: list[Specimen]
) -> None:
    """Given specimens S0..S9 of 0..90 g
    When specimens of at least 80 g are queried
    Then they are S8 and S9
    """
    hits = store.query(F.mass_g >= 80.0)

    assert _labels(hits) == "S8, S9"


def test_delete_drops_the_specimen_from_the_range(
    store: dc.Store, specimens: list[Specimen]
) -> None:
    """Given specimens S0..S9 of 0..90 g
    When S9 is deleted and committed
    Then specimens of at least 80 g are S8 alone
    """
    store.delete(Specimen, label="S9")
    store.commit()

    hits = store.query(F.mass_g >= 80.0)

    assert _labels(hits) == "S8"


def test_sorted_index_is_rebuilt_after_reopen(store_factory: StoreFactory) -> None:
    """Given ten committed specimens and 'undated', the store closed
    When the store is reopened and specimens of at least 50 g are queried
    Then the sorted run, rebuilt from a scan, returns S5..S9 and undated
    """
    store = store_factory()
    store.root = _ten_specimens_and_undated()
    store.commit()
    store.close()
    reopened = store_factory()

    hits = reopened.query(F.mass_g >= 50.0)

    assert _labels(hits) == "S5, S6, S7, S8, S9, undated"
    reopened.close()


def test_sorted_index_rejects_a_list_field() -> None:
    """Given an entity declaring a list field as dc.SortedIndex
    When the entity class is created
    Then TypeError names SortedIndex
    """
    with pytest.raises(TypeError, match="SortedIndex"):

        @dc.entity
        class Bad:
            tags: Annotated[list[str], dc.SortedIndex] = ()  # type: ignore[assignment]


# --- datetime as a SortedIndex key (#106 / ADR-004 §1) ------------------------


def _store_ten_events_and_an_undated_one(store: dc.Store) -> None:
    for i in range(10):
        store.store(CatalogEvent(seq=i, at=_BASE + timedelta(days=i)))
    store.store(CatalogEvent(seq=99, at=None))


@pytest.fixture
def events(store: dc.Store) -> None:
    """Commit ten timestamped acquisitions one day apart (aware UTC), seq 0..9, and seq 99 undated.

    The undated one is SQL-NULL-like: out of every range, last in every order_by.
    """
    _store_ten_events_and_an_undated_one(store)
    store.commit()


@pytest.mark.usefixtures("events")
@pytest.mark.parametrize(
    ("operator", "expected"),
    [
        ("__gt__", "6, 7, 8, 9"),
        ("__ge__", "5, 6, 7, 8, 9"),
        ("__le__", "0, 1, 2, 3, 4, 5"),
        ("__lt__", "0, 1, 2, 3, 4"),
    ],
    ids=["gt", "ge", "le", "lt"],
)
def test_datetime_range_operator_matches_the_expected_events(
    store: dc.Store, operator: str, expected: str
) -> None:
    """Given ten events one day apart and an undated event 99
    When the timestamp is compared with one range operator against day 5
    Then exactly the expected events match, and the undated 99 is in no range
    """
    cut = _BASE + timedelta(days=5)

    hits = store.query(getattr(EF.at, operator)(cut))

    assert _seqs(hits) == expected


@pytest.mark.usefixtures("events")
def test_datetime_between_via_and(store: dc.Store) -> None:
    """Given ten events one day apart and an undated event 99
    When events from day 3 to day 7 inclusive are queried
    Then they are 3, 4, 5, 6 and 7
    """
    lo, hi = _BASE + timedelta(days=3), _BASE + timedelta(days=7)

    hits = store.query((EF.at >= lo) & (EF.at <= hi))

    assert _seqs(hits) == "3, 4, 5, 6, 7"


@pytest.mark.usefixtures("events")
def test_datetime_range_is_index_backed_with_no_residual_scan(store: dc.Store) -> None:
    """Given ten events one day apart and an undated event 99
    When the plan for events from day 5 on is explained
    Then the plan is indexed
    And it has no Python residual scan
    And it considers fewer candidates than the extent
    """
    plan = store.explain(EF.at >= _BASE + timedelta(days=5))

    assert plan.indexed
    assert plan.residual is None
    assert plan.candidates < plan.extent


@pytest.mark.usefixtures("events")
def test_datetime_order_by_ascending_puts_the_undated_last(store: dc.Store) -> None:
    """Given ten events one day apart and an undated event 99
    When events are ordered by time ascending
    Then they come as 0..9 and then 99
    """
    ordered = store.query(CatalogEvent, order_by=EF.at)

    assert _seqs_in_query_order(ordered) == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 99]


@pytest.mark.usefixtures("events")
def test_datetime_order_by_descending_still_puts_the_undated_last(store: dc.Store) -> None:
    """Given ten events one day apart and an undated event 99
    When events are ordered by time descending
    Then they come as 9..0 and then 99, the undated still last
    """
    ordered = store.query(CatalogEvent, order_by=(EF.at, "desc"))

    assert _seqs_in_query_order(ordered) == [9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 99]


@pytest.mark.usefixtures("events")
def test_datetime_order_by_top_k_returns_the_newest(store: dc.Store) -> None:
    """Given ten events one day apart and an undated event 99
    When the 3 newest events are queried
    Then they are 9, 8 and 7
    """
    newest3 = store.query(CatalogEvent, order_by=(EF.at, "desc"), limit=3)

    assert _seqs_in_query_order(newest3) == [9, 8, 7]


@pytest.mark.usefixtures("events")
def test_datetime_snapshot_answers_the_range(store: dc.Store) -> None:
    """Given ten events one day apart and a snapshot rebuilt from its pinned view
    When events from day 5 on are queried on the snapshot
    Then they are 5..9
    """
    snap = store.snapshot()

    views = snap.query(EF.at >= _BASE + timedelta(days=5))

    assert sorted(view.seq for view in views) == [5, 6, 7, 8, 9]
    snap.close()


@pytest.mark.usefixtures("events")
def test_datetime_snapshot_answers_order_by(store: dc.Store) -> None:
    """Given ten events one day apart and an undated event 99, and a snapshot of the store
    When events are ordered by time descending on the snapshot
    Then they come as 9..0 and then 99
    """
    snap = store.snapshot()

    views = snap.query(CatalogEvent, order_by=(EF.at, "desc"))

    assert [view.seq for view in views] == [9, 8, 7, 6, 5, 4, 3, 2, 1, 0, 99]
    snap.close()


def test_datetime_range_is_rebuilt_after_reopen(store_factory: StoreFactory) -> None:
    """Given ten events one day apart and an undated event 99, the store closed
    When the store is reopened and events from day 7 on are queried
    Then the sorted run rebuilt from a backend scan returns 7, 8 and 9 (invariant 11)
    """
    store = store_factory()
    _store_ten_events_and_an_undated_one(store)
    store.commit()
    store.close()
    reopened = store_factory()

    hits = reopened.query(EF.at >= _BASE + timedelta(days=7))

    assert _seqs(hits) == "7, 8, 9"
    reopened.close()


def test_aware_datetimes_order_by_utc_instant(store: dc.Store) -> None:
    """Given 12:00+05:00 (07:00Z) as event 1 and 09:00Z as event 2
    When events are ordered by time
    Then event 1 comes first, the offset noon being the earlier instant
    """
    plus5 = timezone(timedelta(hours=5))
    store.store(CatalogEvent(seq=1, at=datetime(2021, 6, 1, 12, 0, tzinfo=plus5)))
    store.store(CatalogEvent(seq=2, at=datetime(2021, 6, 1, 9, 0, tzinfo=UTC)))
    store.commit()

    ordered = store.query(CatalogEvent, order_by=EF.at)

    assert _seqs_in_query_order(ordered) == [1, 2]


def test_mixing_naive_and_aware_raises_a_named_error_within_one_commit(store: dc.Store) -> None:
    """Given an aware event and a naive event stored for the same field in one commit
    When the commit runs
    Then MixedTemporalIndexError says naive and timezone-aware cannot mix
    """
    store.store(CatalogEvent(seq=1, at=datetime(2021, 6, 1, 12, 0, tzinfo=UTC)))
    store.store(CatalogEvent(seq=2, at=datetime(2021, 6, 1, 12, 0)))

    with pytest.raises(dc.MixedTemporalIndexError, match="naive and timezone-aware"):
        store.commit()


def test_mixing_naive_and_aware_raises_across_commits(store: dc.Store) -> None:
    """Given an aware event committed, then a naive event stored for the same field
    When the second commit runs
    Then MixedTemporalIndexError is raised, never a bare TypeError
    """
    store.store(CatalogEvent(seq=1, at=datetime(2021, 6, 1, 12, 0, tzinfo=UTC)))
    store.commit()
    store.store(CatalogEvent(seq=2, at=datetime(2021, 6, 2, 12, 0)))

    with pytest.raises(dc.MixedTemporalIndexError):
        store.commit()


@pytest.fixture
def naive_events(store: dc.Store) -> datetime:
    """Commit five naive events one day apart, seq 0..4, and return the first timestamp."""
    base = datetime(2021, 1, 1, 9, 0)
    for i in range(5):
        store.store(CatalogEvent(seq=i, at=base + timedelta(days=i)))
    store.commit()
    return base


def test_naive_datetimes_alone_answer_a_range(store: dc.Store, naive_events: datetime) -> None:
    """Given five naive events one day apart, a valid single convention
    When events from day 2 on are queried
    Then they are 2, 3 and 4
    """
    hits = store.query(EF.at >= naive_events + timedelta(days=2))

    assert _seqs(hits) == "2, 3, 4"


def test_naive_datetimes_alone_order_descending(store: dc.Store, naive_events: datetime) -> None:
    """Given five naive events one day apart, a valid single convention
    When events are ordered by time descending
    Then they come as 4, 3, 2, 1, 0
    """
    ordered = store.query(CatalogEvent, order_by=(EF.at, "desc"))

    assert _seqs_in_query_order(ordered) == [4, 3, 2, 1, 0]


# --- datetime/date as eq-bitmap (Index) and unique-map (Unique) keys (#120) ----
# #106 admitted datetime/date at the indexable-scalar type gate and round-trips
# them in the codec (ext codes 2/3/4) — and proved the SortedIndex range path.
# #120 (the "#106B" the test_entity note foreshadows) proves the OTHER two marker
# families: ==/.in_ off the eq bitmap (dc.Index) and get(key=...)/upsert off the
# value→oid unique map (dc.Unique). Both are plain dict lookups keyed by the same
# Python value the codec round-trips; aware equal-instant datetimes collapse to
# one key by hash+eq. Exact expected sets, both backends.


@dc.entity
class Acquisition:
    """A cabinet acquisition keyed by WHEN it happened.

    ``at`` is a plain ``dc.Index`` datetime (eq bitmap, ==/.in_); ``ticket`` is a ``dc.Unique``
    datetime (the value→oid map, get(key=...)/upsert); ``day`` is a ``dc.Index`` date. Proves the
    #106 temporal keys work for the Index and Unique families, not only SortedIndex (#120).
    """

    qid: Annotated[str, dc.Unique]
    at: Annotated[datetime | None, dc.Index] = None
    ticket: Annotated[datetime | None, dc.Unique] = None
    day: Annotated[date | None, dc.Index] = None


AF = dc.fields(Acquisition)
_T0 = datetime(2021, 3, 1, 8, 0, tzinfo=UTC)
_LATER = _T0 + timedelta(days=1)


def _qids(acquisitions: Iterable[Acquisition]) -> str:
    return ", ".join(sorted(acquisition.qid for acquisition in acquisitions))


def _qids_without_a_ticket(acquisitions: Iterable[Acquisition]) -> set[str]:
    return {acquisition.qid for acquisition in acquisitions if acquisition.ticket is None}


@pytest.fixture
def acquisitions(store: dc.Store) -> None:
    """Commit acquisitions A1..A4.

    A1 and A2 share the instant 08:00Z through two clocks (UTC and +02:00), A3 is a day later,
    and A4 has at, ticket and day all None (SQL-NULL-like).
    """
    plus2 = timezone(timedelta(hours=2))
    store.store(Acquisition(qid="A1", at=_T0, ticket=_T0, day=date(2021, 3, 1)))
    store.store(
        Acquisition(
            qid="A2",
            at=datetime(2021, 3, 1, 10, 0, tzinfo=plus2),  # == 08:00Z
            ticket=_T0 + timedelta(hours=1),
            day=date(2021, 3, 1),
        )
    )
    store.store(Acquisition(qid="A3", at=_LATER, ticket=_LATER, day=date(2021, 3, 2)))
    store.store(Acquisition(qid="A4", at=None, ticket=None, day=None))
    store.commit()


@pytest.mark.usefixtures("acquisitions")
def test_index_datetime_eq_collapses_equal_instant_clocks(store: dc.Store) -> None:
    """Given A1 (08:00Z) and A2 (10:00+02:00) acquired at the same instant
    When acquisitions at 08:00Z are queried
    Then both A1 and A2 match, as one key by hash and eq
    """
    hits = store.query(AF.at == _T0)

    assert _qids(hits) == "A1, A2"


@pytest.mark.usefixtures("acquisitions")
def test_index_datetime_eq_excludes_the_later_instant(store: dc.Store) -> None:
    """Given A3 acquired a day after A1 and A2
    When acquisitions at A3's instant are queried
    Then only A3 matches
    """
    hits = store.query(AF.at == _LATER)

    assert _qids(hits) == "A3"


@pytest.mark.usefixtures("acquisitions")
def test_index_datetime_eq_is_bitmap_backed_with_no_residual(store: dc.Store) -> None:
    """Given A1 and A2 acquired at the same instant
    When the plan for acquisitions at 08:00Z is explained
    Then the plan is indexed
    And it has no residual
    """
    plan = store.explain(AF.at == _T0)

    assert plan.indexed
    assert plan.residual is None


@pytest.mark.usefixtures("acquisitions")
def test_index_datetime_eq_loads_only_the_equal_instant_rows(store: dc.Store) -> None:
    """Given A1 and A2 acquired at the same instant
    When the plan for acquisitions at 08:00Z is explained
    Then it considers 2 candidates, nothing else loaded
    """
    plan = store.explain(AF.at == _T0)

    assert plan.candidates == 2


@pytest.mark.usefixtures("acquisitions")
def test_index_datetime_in_unions_keys(store: dc.Store) -> None:
    """Given acquisitions A1..A4 and a never-stored instant
    When acquisitions at 08:00Z, a day later, or the never-stored instant are queried
    Then A1, A2 and A3 match, the unknown key contributing an empty posting
    """
    never_stored = _T0 + timedelta(days=99)

    hits = store.query(AF.at.in_([_T0, _LATER, never_stored]))

    assert _qids(hits) == "A1, A2, A3"


@pytest.mark.usefixtures("acquisitions")
def test_index_date_eq_keys_the_bitmap(store: dc.Store) -> None:
    """Given A1 and A2 acquired on 2021-03-01
    When acquisitions on 2021-03-01 are queried
    Then A1 and A2 match
    """
    hits = store.query(AF.day == date(2021, 3, 1))

    assert _qids(hits) == "A1, A2"


@pytest.mark.usefixtures("acquisitions")
def test_index_date_in_unions_keys(store: dc.Store) -> None:
    """Given acquisitions on 2021-03-01 and 2021-03-02
    When acquisitions on either date are queried
    Then A1, A2 and A3 match
    """
    hits = store.query(AF.day.in_([date(2021, 3, 1), date(2021, 3, 2)]))

    assert _qids(hits) == "A1, A2, A3"


@pytest.mark.usefixtures("acquisitions")
def test_index_datetime_in_over_real_keys_never_matches_the_none_row(store: dc.Store) -> None:
    """Given A4 whose at is None
    When acquisitions in three real instants are queried
    Then A1, A2 and A3 match, never A4
    (an Index == None is a normal nullable equality, not under test here, #120)
    """
    real = [_T0, _LATER, _T0 + timedelta(days=99)]

    hits = store.query(AF.at.in_(real))

    assert _qids(hits) == "A1, A2, A3"


@pytest.mark.usefixtures("acquisitions")
def test_unique_datetime_none_is_not_in_the_unique_map(store: dc.Store) -> None:
    """Given A4 whose ticket is None
    When an acquisition is fetched by ticket None
    Then none is found, a Unique field excluding None from its value-to-oid map
    """
    found = store.get(Acquisition, ticket=None)

    assert found is None


@pytest.mark.usefixtures("acquisitions")
def test_unique_datetime_rows_may_share_a_none_ticket(store: dc.Store) -> None:
    """Given A4 whose ticket is None
    When A5, also without a ticket, is committed
    Then A4 and A5 both have no ticket, None not being a uniqueness value
    """
    store.store(Acquisition(qid="A5", ticket=None))
    store.commit()

    all_acquisitions = store.query(Acquisition)

    assert _qids_without_a_ticket(all_acquisitions) == {"A4", "A5"}


@pytest.mark.usefixtures("acquisitions")
def test_unique_datetime_get_by_key_finds_the_entity(store: dc.Store) -> None:
    """Given A1 with a ticket at 08:00Z
    When an acquisition is fetched by ticket 08:00Z
    Then it is A1
    """
    found = store.get(Acquisition, ticket=_T0)

    assert found is not None
    assert found.qid == "A1"


@pytest.mark.usefixtures("acquisitions")
def test_unique_datetime_get_with_an_equal_instant_finds_the_same_entity(store: dc.Store) -> None:
    """Given A1 with a ticket at 08:00Z
    When it is fetched by 10:00+02:00, the same instant spelled by another clock
    Then it is the very same live instance, one per OID
    """
    a1 = store.get(Acquisition, ticket=_T0)
    plus2 = timezone(timedelta(hours=2))

    a1_again = store.get(Acquisition, ticket=datetime(2021, 3, 1, 10, 0, tzinfo=plus2))

    assert a1_again is a1


@pytest.mark.usefixtures("acquisitions")
def test_unique_datetime_get_of_a_never_stored_instant_returns_none(store: dc.Store) -> None:
    """Given acquisitions A1..A4
    When an acquisition is fetched by a never-stored ticket instant
    Then none is found
    """
    found = store.get(Acquisition, ticket=_T0 + timedelta(days=99))

    assert found is None


@pytest.mark.usefixtures("acquisitions")
def test_unique_datetime_upsert_merges_into_the_live_instance(store: dc.Store) -> None:
    """Given A1 with a ticket at 08:00Z
    When an acquisition with the same ticket and another day is upserted by key "ticket"
    Then the upsert returns the surviving live A1
    """
    a1 = store.get(Acquisition, ticket=_T0)

    merged = store.upsert(
        Acquisition(qid="A1", at=_T0, ticket=_T0, day=date(2099, 1, 1)), key="ticket"
    )

    assert merged is a1


@pytest.mark.usefixtures("acquisitions")
def test_unique_datetime_upsert_writes_the_changed_field(store: dc.Store) -> None:
    """Given A1 with a ticket at 08:00Z and day 2021-03-01
    When an acquisition with the same ticket and day 2099-01-01 is upserted and committed
    Then A1's day is 2099-01-01
    """
    store.upsert(Acquisition(qid="A1", at=_T0, ticket=_T0, day=date(2099, 1, 1)), key="ticket")
    store.commit()

    a1 = store.get(Acquisition, ticket=_T0)

    assert a1 is not None
    assert a1.day == date(2099, 1, 1)


def test_temporal_eq_keys_survive_reopen(store_factory: StoreFactory) -> None:
    """Given acquisitions A1..A3 committed and the store closed
    When the store is reopened and acquisitions at 08:00Z are queried
    Then the eq bitmap rebuilt from a backend scan matches A1 and A2 by hash and eq
    """
    store = store_factory()
    store.store(Acquisition(qid="A1", at=_T0))
    store.store(
        Acquisition(qid="A2", at=datetime(2021, 3, 1, 10, 0, tzinfo=timezone(timedelta(hours=2))))
    )
    store.store(Acquisition(qid="A3", at=_LATER))
    store.commit()
    store.close()
    reopened = store_factory()

    hits = reopened.query(AF.at == _T0)

    assert _qids(hits) == "A1, A2"
    reopened.close()


def test_temporal_date_keys_survive_reopen(store_factory: StoreFactory) -> None:
    """Given A1 and A2 acquired on 2021-03-01, committed and the store closed
    When the store is reopened and acquisitions on 2021-03-01 are queried
    Then A1 and A2 match
    """
    store = store_factory()
    store.store(Acquisition(qid="A1", day=date(2021, 3, 1)))
    store.store(Acquisition(qid="A2", day=date(2021, 3, 1)))
    store.store(Acquisition(qid="A3", day=date(2021, 3, 2)))
    store.commit()
    store.close()
    reopened = store_factory()

    hits = reopened.query(AF.day == date(2021, 3, 1))

    assert _qids(hits) == "A1, A2"
    reopened.close()


def test_temporal_unique_keys_survive_reopen(store_factory: StoreFactory) -> None:
    """Given A1 with a ticket at 08:00Z, committed and the store closed
    When the store is reopened and an acquisition is fetched by that ticket
    Then the unique map rebuilt from a backend scan finds A1
    """
    store = store_factory()
    store.store(Acquisition(qid="A1", ticket=_T0))
    store.store(Acquisition(qid="A3", ticket=_LATER))
    store.commit()
    store.close()
    reopened = store_factory()

    found = reopened.get(Acquisition, ticket=_T0)

    assert found is not None
    assert found.qid == "A1"
    reopened.close()
