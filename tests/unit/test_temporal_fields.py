"""Temporal values round-trip as their own types (format v2).

The MaStR import feedback found naive datetimes silently round-tripping as
ISO *strings*. Format v2 gives naive datetime / date / time datacrystal
extension codes; tz-aware datetimes ride msgpack's standard timestamp ext
(the instant is preserved; a non-UTC offset normalizes to UTC).
"""

from __future__ import annotations

import datetime
from dataclasses import field

import datacrystal as dc
from tests.conftest import Mineral

BERLIN = datetime.timezone(datetime.timedelta(hours=2))


@dc.entity
class Reading:
    sensor: str
    at: datetime.datetime | None = None
    on_day: datetime.date | None = None
    at_time: datetime.time | None = None
    history: list = field(default_factory=list)


def test_naive_datetime_survives_as_datetime(store_factory):
    store = store_factory()
    naive = datetime.datetime(2026, 6, 12, 10, 30, 0, 123456)
    store.store(Reading(sensor="a", at=naive))
    store.commit()
    store.close()

    reopened = store_factory()
    (back,) = reopened.query(dc.fields(Reading).sensor == "a")
    assert back.at == naive
    assert isinstance(back.at, datetime.datetime)
    assert back.at.tzinfo is None
    reopened.close()


def test_aware_datetime_keeps_the_instant(store_factory):
    store = store_factory()
    aware = datetime.datetime(2026, 6, 12, 10, 30, tzinfo=BERLIN)
    store.store(Reading(sensor="b", at=aware))
    store.commit()
    store.close()

    reopened = store_factory()
    (back,) = reopened.query(dc.fields(Reading).sensor == "b")
    assert isinstance(back.at, datetime.datetime)
    assert back.at == aware  # same instant (normalized to UTC by msgpack)
    assert back.at.tzinfo is not None
    reopened.close()


def test_date_and_time_survive_as_their_types(store_factory):
    store = store_factory()
    store.store(Reading(sensor="c", on_day=datetime.date(2026, 6, 12),
                        at_time=datetime.time(10, 30, 5)))
    store.commit()
    store.close()

    reopened = store_factory()
    (back,) = reopened.query(dc.fields(Reading).sensor == "c")
    assert back.on_day == datetime.date(2026, 6, 12)
    assert type(back.on_day) is datetime.date
    assert back.at_time == datetime.time(10, 30, 5)
    assert type(back.at_time) is datetime.time
    reopened.close()


def test_temporals_survive_inside_containers(store_factory):
    store = store_factory()
    moments = [datetime.datetime(2026, 1, 1), datetime.date(2026, 1, 2)]
    store.store(Reading(sensor="d", history=moments))
    store.commit()
    store.close()

    reopened = store_factory()
    (back,) = reopened.query(dc.fields(Reading).sensor == "d")
    assert list(back.history) == moments
    assert type(back.history[0]) is datetime.datetime
    assert type(back.history[1]) is datetime.date
    reopened.close()


def test_a_v1_era_store_upgrades_its_stamp_on_first_commit(tmp_path):
    # Simulate a store written before format v2: open, downgrade the stamp,
    # then reopen with the current library — reads leave the stamp alone,
    # the first commit re-stamps (a v1 reader must refuse only once v2
    # payload bytes may actually exist).
    import sqlite3

    path = tmp_path / "store"
    store = dc.Store.open(path)
    store.close()
    with sqlite3.connect(path / "data.sqlite") as conn:
        conn.execute("UPDATE meta SET value='1' WHERE key='format_version'")

    store = dc.Store.open(path)
    with sqlite3.connect(path / "data.sqlite") as conn:
        (stamp,) = conn.execute(
            "SELECT value FROM meta WHERE key='format_version'"
        ).fetchone()
    assert stamp == "1"  # opening alone does not upgrade
    store.store(Mineral(qid="Q43010", name="quartz"))
    store.commit()
    store.close()
    with sqlite3.connect(path / "data.sqlite") as conn:
        (stamp,) = conn.execute(
            "SELECT value FROM meta WHERE key='format_version'"
        ).fetchone()
    assert stamp == "2"
