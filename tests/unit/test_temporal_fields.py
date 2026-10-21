"""Temporal values round-trip as their own types (format v2).

The MaStR import feedback found naive datetimes silently round-tripping as
ISO *strings*. Format v2 gives naive datetime / date / time datacrystal
extension codes; tz-aware datetimes ride msgpack's standard timestamp ext
(the instant is preserved; a non-UTC offset normalizes to UTC).
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Callable
from dataclasses import field
from pathlib import Path

import datacrystal as dc
from tests.conftest import Mineral

BERLIN = datetime.timezone(datetime.timedelta(hours=2))


@dc.entity
class Specimen:
    """A catalogued specimen with the moments it was found, shown and moved."""

    label: str
    catalogued_at: datetime.datetime | None = None
    found_on: datetime.date | None = None
    shown_at: datetime.time | None = None
    history: list = field(default_factory=list)


def _stored_format_version(store_dir: Path) -> str:
    with sqlite3.connect(store_dir / "data.sqlite") as conn:
        (stamp,) = conn.execute("SELECT value FROM meta WHERE key='format_version'").fetchone()
    return stamp


def _downgrade_format_version_to_v1(store_dir: Path) -> None:
    with sqlite3.connect(store_dir / "data.sqlite") as conn:
        conn.execute("UPDATE meta SET value='1' WHERE key='format_version'")


def test_naive_datetime_survives_as_datetime(store_factory: Callable[[], dc.Store]) -> None:
    """Given a stored specimen catalogued at a naive datetime with microseconds
    When the store is reopened and the specimen is queried
    Then the datetime comes back equal to the original
    And it is a datetime
    And it carries no timezone
    """
    store = store_factory()
    naive = datetime.datetime(2026, 6, 12, 10, 30, 0, 123456)
    store.store(Specimen(label="a", catalogued_at=naive))
    store.commit()
    store.close()
    reopened = store_factory()

    (back,) = reopened.query(dc.fields(Specimen).label == "a")

    assert back.catalogued_at == naive
    assert isinstance(back.catalogued_at, datetime.datetime)
    assert back.catalogued_at.tzinfo is None
    reopened.close()


def test_aware_datetime_keeps_the_instant(store_factory: Callable[[], dc.Store]) -> None:
    """Given a stored specimen catalogued at a timezone-aware datetime
    When the store is reopened and the specimen is queried
    Then the datetime is the same instant
    And it is a datetime
    And it carries a timezone
    """
    store = store_factory()
    aware = datetime.datetime(2026, 6, 12, 10, 30, tzinfo=BERLIN)
    store.store(Specimen(label="b", catalogued_at=aware))
    store.commit()
    store.close()
    reopened = store_factory()

    (back,) = reopened.query(dc.fields(Specimen).label == "b")

    assert isinstance(back.catalogued_at, datetime.datetime)
    assert back.catalogued_at == aware
    assert back.catalogued_at.tzinfo is not None
    reopened.close()


def test_date_survives_as_a_date(store_factory: Callable[[], dc.Store]) -> None:
    """Given a stored specimen found on a date
    When the store is reopened and the specimen is queried
    Then the date comes back equal to the original
    And it is a date, not a datetime
    """
    store = store_factory()
    store.store(Specimen(label="c", found_on=datetime.date(2026, 6, 12)))
    store.commit()
    store.close()
    reopened = store_factory()

    (back,) = reopened.query(dc.fields(Specimen).label == "c")

    assert back.found_on == datetime.date(2026, 6, 12)
    assert type(back.found_on) is datetime.date
    reopened.close()


def test_time_survives_as_a_time(store_factory: Callable[[], dc.Store]) -> None:
    """Given a stored specimen shown at a time of day
    When the store is reopened and the specimen is queried
    Then the time comes back equal to the original
    And it is a time
    """
    store = store_factory()
    store.store(Specimen(label="c", shown_at=datetime.time(10, 30, 5)))
    store.commit()
    store.close()
    reopened = store_factory()

    (back,) = reopened.query(dc.fields(Specimen).label == "c")

    assert back.shown_at == datetime.time(10, 30, 5)
    assert type(back.shown_at) is datetime.time
    reopened.close()


def test_temporals_survive_inside_containers(store_factory: Callable[[], dc.Store]) -> None:
    """Given a stored specimen whose history list holds a datetime and a date
    When the store is reopened and the specimen is queried
    Then the history equals the original list
    And its first item is a datetime
    And its second item is a date
    """
    store = store_factory()
    moments = [datetime.datetime(2026, 1, 1), datetime.date(2026, 1, 2)]
    store.store(Specimen(label="d", history=moments))
    store.commit()
    store.close()
    reopened = store_factory()

    (back,) = reopened.query(dc.fields(Specimen).label == "d")

    assert list(back.history) == moments
    assert type(back.history[0]) is datetime.datetime
    assert type(back.history[1]) is datetime.date
    reopened.close()


def test_opening_a_v1_era_store_leaves_its_stamp_alone(tmp_path: Path) -> None:
    """Given a SQLite store stamped with format version 1
    When it is opened with the current library and nothing is committed
    Then the stamp is still 1
    """
    path = tmp_path / "store"
    dc.Store.open(path).close()
    _downgrade_format_version_to_v1(path)

    store = dc.Store.open(path)

    assert _stored_format_version(path) == "1"
    store.close()


def test_the_first_commit_upgrades_a_v1_era_stamp(tmp_path: Path) -> None:
    """Given a SQLite store stamped with format version 1, opened with the current library
    When a mineral is committed
    Then the stamp is 2
    """
    path = tmp_path / "store"
    dc.Store.open(path).close()
    _downgrade_format_version_to_v1(path)
    store = dc.Store.open(path)
    store.store(Mineral(qid="Q43010", name="quartz"))

    store.commit()

    store.close()
    assert _stored_format_version(path) == "2"
