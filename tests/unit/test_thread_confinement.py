"""ADR-001 owner confinement: foreign threads fail loudly, mutations never land."""

from __future__ import annotations

import threading

import pytest

import datacrystal as dc
from tests.conftest import Mineral


def _run_in_thread(fn):
    result: list = []

    def target():
        try:
            result.append(("ok", fn()))
        except Exception as exc:  # noqa: BLE001
            result.append(("err", exc))

    t = threading.Thread(target=target)
    t.start()
    t.join()
    return result[0]


def test_foreign_thread_store_access_raises(store):
    kind, value = _run_in_thread(lambda: store.root)
    assert kind == "err" and isinstance(value, dc.WrongThreadError)
    assert "snapshot" in str(value)  # the message carries the escape recipe


def test_foreign_thread_query_raises(store):
    store.root = [Mineral(qid="Q1", name="quartz", crystal_system="cubic")]
    store.commit()
    kind, value = _run_in_thread(lambda: store.query(Mineral.crystal_system == "cubic"))
    assert kind == "err" and isinstance(value, dc.WrongThreadError)


def test_foreign_thread_write_raises_before_mutating(store):
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()
    quartz = store.get(Mineral, qid="Q1")

    def mutate():
        quartz.name = "changed"

    kind, value = _run_in_thread(mutate)
    assert kind == "err" and isinstance(value, dc.WrongThreadError)
    assert quartz.name == "quartz"  # the guard fired BEFORE the write landed
    assert store.commit() is None  # and nothing was buffered


def test_closed_store_raises(store_factory):
    store = store_factory()
    store.close()
    with pytest.raises(dc.StoreClosedError):
        _ = store.root
