"""store.submit() — the sanctioned cross-thread write path (ADR-001).

Foreign threads ship closures; the owner runs them at its next store call
(piggyback) or via run_pending(). Futures resolve with plain data only —
a live entity in the result fails with EntityEscapeError, whose message
embeds the recipe (KICKOFF M2 exit criterion 3).
"""

from __future__ import annotations

import threading
from concurrent.futures import Future

import pytest

import datacrystal as dc
from tests.conftest import Mineral


def _submit_from_foreign_thread(store, fn) -> Future:
    out: list[Future] = []
    t = threading.Thread(target=lambda: out.append(store.submit(fn)))
    t.start()
    t.join()
    return out[0]


def test_foreign_submission_runs_on_owner_piggyback(store):
    future = _submit_from_foreign_thread(
        store, lambda: store.store(Mineral(qid="Q1", name="quartz"))
    )
    assert not future.done()  # nothing ran yet: the owner has not called in
    store.commit()  # any owner API boundary pumps the queue
    assert future.done() and isinstance(future.result(), int)
    found = store.get(Mineral, qid="Q1")
    assert found is not None and found.name == "quartz"


def test_run_pending_drains_explicitly(store):
    seen: list[int] = []
    futures = [
        _submit_from_foreign_thread(store, lambda i=i: seen.append(i) or i)
        for i in range(3)
    ]
    assert store.run_pending() == 3
    assert seen == [0, 1, 2]  # FIFO
    assert [f.result() for f in futures] == [0, 1, 2]
    assert store.run_pending() == 0


def test_owner_submit_runs_inline(store):
    future = store.submit(lambda: 41 + 1)
    assert future.result(timeout=0) == 42


def test_submission_exception_lands_in_the_future(store):
    future = _submit_from_foreign_thread(
        store, lambda: (_ for _ in ()).throw(ValueError("boom"))
    )
    store.run_pending()
    with pytest.raises(ValueError, match="boom"):
        future.result(timeout=0)


@pytest.mark.parametrize(
    "shape",
    [
        lambda m: m,                       # the entity itself
        lambda m: [1, [m]],                # nested in containers
        lambda m: {"hit": m},              # dict value
        lambda m: dc.Lazy.of(m),           # a loaded Lazy handle
    ],
)
def test_live_entity_results_raise_entity_escape(store, shape):
    store.store(Mineral(qid="Q1", name="quartz"))
    store.commit()

    def fetch():
        return shape(store.get(Mineral, qid="Q1"))

    future = _submit_from_foreign_thread(store, fetch)
    store.run_pending()
    with pytest.raises(dc.EntityEscapeError, match="submit"):
        future.result(timeout=0)


def test_plain_data_results_pass(store):
    store.store(Mineral(qid="Q1", name="quartz", crystal_system="trigonal"))
    store.commit()

    def fetch():
        m = store.get(Mineral, qid="Q1")
        assert m is not None
        return {"qid": m.qid, "name": m.name}

    future = _submit_from_foreign_thread(store, fetch)
    store.run_pending()
    assert future.result(timeout=0) == {"qid": "Q1", "name": "quartz"}


def test_direct_foreign_access_still_raises_wrong_thread(store):
    errors: list[BaseException] = []

    def trespass():
        try:
            store.commit()
        except BaseException as exc:  # noqa: BLE001 — recording for assert
            errors.append(exc)

    t = threading.Thread(target=trespass)
    t.start()
    t.join()
    assert len(errors) == 1 and isinstance(errors[0], dc.WrongThreadError)
    assert "submit" in str(errors[0])  # the message embeds the recipe


def test_close_fails_pending_submissions(store_factory):
    store = store_factory()
    future = _submit_from_foreign_thread(store, lambda: 1)
    store.close()
    with pytest.raises(dc.StoreClosedError):
        future.result(timeout=0)
