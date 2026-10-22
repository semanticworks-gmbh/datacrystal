"""Operational events through stdlib logging (#195).

The library reports a discarded index cache, a migration, a reclaimed stale lease and a follower
resync as log records under the ``datacrystal`` logger, and configures nothing itself.
Each test reads the record through ``caplog``: message, level and every ``extra`` field.
An ``extra`` key that collides with a ``LogRecord`` attribute raises ``KeyError`` when logged,
so every event test also pins that the keys are safe.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import pytest

import datacrystal as dc
from tests.conftest import Mineral
from tests.log_capture import logged, logged_all

StoreFactory = Callable[[], dc.Store]
REQUIRED = object()


@pytest.fixture(autouse=True)
def datacrystal_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Capture everything the library logs from INFO up."""
    caplog.set_level(logging.INFO, logger="datacrystal")


def evolve(*, class_name: str = "Specimen", **fields: tuple[object, object]) -> type[Any]:
    """(Re)define the entity class ``class_name`` of this module with these fields."""
    annotations: dict[str, object] = {}
    namespace: dict[str, object] = {
        "__module__": __name__,
        "__qualname__": class_name,
        "__annotations__": annotations,
    }
    for name, (annotation, default) in fields.items():
        annotations[name] = annotation
        if default is not REQUIRED:
            namespace[name] = default
    return dc.entity(type(class_name, (), namespace))


@pytest.fixture
def exited_pid() -> int:
    """Return the pid of a process that has run and been reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@pytest.fixture
def stale_cache_directory(tmp_path: Path) -> tuple[Path, int]:
    """Return a store directory whose index cache lags one commit, and the cached watermark."""
    directory = tmp_path / "cabinet"
    with dc.Store.open(directory) as first:
        first.store(Mineral(qid="Q1", name="quartz", crystal_system="trigonal"))
        first.commit()
        first.query(dc.fields(Mineral).crystal_system == "trigonal")
        with first.snapshot() as snapshot:
            cached_watermark = snapshot.tid
    with dc.Store.open(directory, cache_index=False) as second:
        second.store(Mineral(qid="Q2", name="azurite", crystal_system="monoclinic"))
        second.commit()
    return directory, cached_watermark


def test_index_cache_behind_the_store_is_logged_as_discarded(
    stale_cache_directory: tuple[Path, int], caplog: pytest.LogCaptureFixture
) -> None:
    """Given an index cache written one commit before the store's last commit
    When the store is opened
    Then an info record names the watermark mismatch with both watermarks
    """
    directory, cached_watermark = stale_cache_directory

    with dc.Store.open(directory):
        record = logged(caplog)["index_cache_discarded"]

    assert record.level == logging.INFO
    assert record.fields["reason"] == "watermark_mismatch"
    assert record.fields["cached_watermark"] == cached_watermark
    assert record.fields["store_watermark"] == cached_watermark + 1


def test_index_cache_at_the_store_watermark_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Given an index cache written at the store's last commit
    When the store is reopened
    Then no discard is logged
    """
    with dc.Store.open(tmp_path / "cabinet") as first:
        first.store(Mineral(qid="Q1", name="quartz", crystal_system="trigonal"))
        first.commit()
        first.query(dc.fields(Mineral).crystal_system == "trigonal")

    with dc.Store.open(tmp_path / "cabinet"):
        events = logged(caplog)

    assert "index_cache_discarded" not in events


def test_migration_logs_the_rewritten_count_per_type(
    store_factory: StoreFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Given quartz, azurite and opal stored before the class gained a mohs field
    When the store is migrated
    Then an info record names the type and three rewritten records
    """
    old_shape = evolve(name=(str, REQUIRED))
    first = store_factory()
    first.store(old_shape(name="quartz"))
    first.store(old_shape(name="azurite"))
    first.store(old_shape(name="opal"))
    first.commit()
    first.close()
    evolve(name=(str, REQUIRED), mohs=(Annotated[float | None, dc.Index], None))
    store = store_factory()

    store.migrate()

    record = logged(caplog)["records_migrated"]
    assert record.level == logging.INFO
    assert record.fields["type_name"] == f"{__name__}:Specimen"
    assert record.fields["count"] == 3
    store.close()


def test_migration_logs_one_record_per_stale_type(
    store_factory: StoreFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Given two specimens and one fossil stored before their classes gained a field
    When the store is migrated
    Then there are two records, each naming its own type and count
    """
    old_specimen = evolve(name=(str, REQUIRED))
    old_fossil = evolve(class_name="Fossil", name=(str, REQUIRED))
    first = store_factory()
    first.store(old_specimen(name="quartz"))
    first.store(old_specimen(name="opal"))
    first.store(old_fossil(name="trilobite"))
    first.commit()
    first.close()
    evolve(name=(str, REQUIRED), mohs=(Annotated[float | None, dc.Index], None))
    evolve(class_name="Fossil", name=(str, REQUIRED), mohs=(Annotated[float | None, dc.Index], None))
    store = store_factory()

    store.migrate()

    records = logged_all(caplog)["records_migrated"]
    assert {(r.fields["type_name"], r.fields["count"]) for r in records} == {
        (f"{__name__}:Specimen", 2), (f"{__name__}:Fossil", 1)}
    assert len(records) == 2
    store.close()


def test_migration_with_nothing_stale_logs_nothing(
    store_factory: StoreFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Given a store whose records already have the current shape
    When the store is migrated
    Then no migration is logged
    """
    current = evolve(name=(str, REQUIRED))
    store = store_factory()
    store.store(current(name="quartz"))
    store.commit()

    store.migrate()

    assert "records_migrated" not in logged(caplog)
    store.close()


def test_reclaiming_an_expired_lease_is_logged_as_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Given a store directory whose lock file was last refreshed long ago
    When the store is opened with a half-second lease
    Then a warning record names the expired lease and the ttl
    """
    directory = tmp_path / "cabinet"
    directory.mkdir()
    (directory / "used.lock").write_text(json.dumps({"pid": 1, "token": "gone", "ts": 1000.0}))

    with dc.Store.open(directory, lock_ttl=0.5):
        record = logged(caplog)["stale_lease_reclaimed"]

    assert record.level == logging.WARNING
    assert record.fields["reason"] == "ttl_expired"
    assert record.fields["lease_ttl_seconds"] == 0.5


def test_reclaiming_an_unreadable_lease_is_logged_with_that_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Given a store directory whose lock file holds no JSON
    When the store is opened
    Then the warning names the lease as unreadable
    """
    directory = tmp_path / "cabinet"
    directory.mkdir()
    (directory / "used.lock").write_text("not json")

    with dc.Store.open(directory, lock_ttl=0.5):
        record = logged(caplog)["stale_lease_reclaimed"]

    assert record.fields["reason"] == "unreadable"


def test_reclaiming_a_lease_of_a_gone_process_is_logged_with_that_reason(
    tmp_path: Path, exited_pid: int, caplog: pytest.LogCaptureFixture
) -> None:
    """Given a store directory whose fresh lock file names a process of this host that has exited
    When the store is opened
    Then the warning names the holder process as gone
    """
    directory = tmp_path / "cabinet"
    directory.mkdir()
    (directory / "used.lock").write_text(json.dumps({
        "pid": exited_pid, "token": "gone", "host": socket.gethostname(), "ts": time.time()}))

    with dc.Store.open(directory, lock_ttl=30):
        record = logged(caplog)["stale_lease_reclaimed"]

    assert record.fields["reason"] == "holder_process_gone"


def test_a_fresh_open_logs_no_lease_event(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Given a store directory without a lock file
    When the store is opened
    Then no lease event is logged
    """
    with dc.Store.open(tmp_path / "cabinet"):
        events = logged(caplog)

    assert "stale_lease_reclaimed" not in events


def test_import_leaves_the_root_logger_alone_and_adds_one_null_handler() -> None:
    """Given a fresh interpreter
    When datacrystal is imported
    Then the root logger has no handler and the datacrystal logger has exactly one NullHandler
    """
    script = (
        "import json, logging, datacrystal\n"
        "lib = logging.getLogger('datacrystal')\n"
        "print(json.dumps({'root': len(logging.getLogger().handlers),\n"
        "    'lib': [type(h).__name__ for h in lib.handlers],\n"
        "    'level': lib.level, 'propagate': lib.propagate}))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )

    report = json.loads(result.stdout)
    assert report["root"] == 0
    assert report["lib"] == ["NullHandler"]
    assert report["level"] == logging.NOTSET
    assert report["propagate"] is True
