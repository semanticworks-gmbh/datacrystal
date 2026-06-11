"""Forward-version guard (DESIGN amendment 7 / fitness #18): old code must
refuse newer stores loudly, naming both versions."""

from __future__ import annotations

import sqlite3

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend


def test_sqlite_newer_store_is_refused(tmp_path):
    store = dc.Store.open(tmp_path / "s")
    store.close()
    conn = sqlite3.connect(tmp_path / "s" / "data.sqlite")
    conn.execute("UPDATE meta SET value='99' WHERE key='format_version'")
    conn.commit()
    conn.close()

    with pytest.raises(dc.NewerStoreError, match="v99.*v1"):
        dc.Store.open(tmp_path / "s")
    # The failed open must not leave the lease behind.
    assert not (tmp_path / "s" / "used.lock").exists()


def test_memory_newer_store_is_refused():
    backend = MemoryBackend()
    backend._meta["format_version"] = "99"
    with pytest.raises(dc.NewerStoreError):
        dc.Store._from_backend(backend)
