"""#195 — rebuilding the FTS sidecar from a snapshot is an operational event."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import pytest

pytest.importorskip("snowballstemmer", reason="datacrystal[fts] extra not installed")

import datacrystal as dc
from datacrystal.fts import FullTextIndex
from tests.log_capture import logged


@dc.entity
class Specimen:
    """A cabinet specimen with a searchable label."""

    qid: Annotated[str, dc.Unique]
    label: Annotated[str | None, dc.FullText(language="en")] = None


SPECIMEN_FULLTEXT = {"tests.extras.test_operational_logging_fts:Specimen": {"label": "en"}}


@pytest.fixture(autouse=True)
def datacrystal_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Capture everything the library logs from INFO up."""
    caplog.set_level(logging.INFO, logger="datacrystal")


@pytest.fixture
def lived_in_store(store: dc.Store) -> dc.Store:
    """Return a store holding two labelled specimens."""
    store.root = [
        Specimen(qid="Q1", label="milky quartz crystals"),
        Specimen(qid="Q2", label="deep blue azurite"),
    ]
    store.commit()
    return store


@pytest.fixture
def sidecar_path(tmp_path: Path) -> Path:
    """Return where the sidecar lives."""
    return tmp_path / "labels.fts"


def test_bootstrapping_the_sidecar_is_logged_with_its_size_and_watermark(
    lived_in_store: dc.Store, sidecar_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Given a store with two labelled specimens
    When the sidecar is bootstrapped from a snapshot
    Then an info record names two documents and the snapshot watermark
    """
    with lived_in_store.snapshot() as snapshot:
        index = FullTextIndex.bootstrap(sidecar_path, snapshot, fulltext=SPECIMEN_FULLTEXT)
        watermark = snapshot.tid

    record = logged(caplog)["fts_sidecar_rebuilt"]
    assert record.level == logging.INFO
    assert record.fields["documents"] == 2
    assert record.fields["watermark"] == watermark
    index.close()
