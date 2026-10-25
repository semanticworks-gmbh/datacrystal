"""Forward-version guard (DESIGN amendment 7 / fitness #18).

Old code must refuse newer stores loudly, naming both versions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import datacrystal as dc


def _stamp_sqlite_format_version(store_dir: Path, version: str) -> None:
    conn = sqlite3.connect(store_dir / "data.sqlite")
    conn.execute("UPDATE meta SET value=? WHERE key='format_version'", (version,))
    conn.commit()
    conn.close()


def test_sqlite_newer_store_is_refused_naming_both_versions(tmp_path: Path) -> None:
    """Given a SQLite store stamped with format version 99
    When it is opened by code that knows an older format
    Then opening raises NewerStoreError naming v99 and the known version
    """
    dc.Store.open(tmp_path / "s").close()
    _stamp_sqlite_format_version(tmp_path / "s", "99")

    with pytest.raises(dc.NewerStoreError, match="v99.*v2"):
        dc.Store.open(tmp_path / "s")


def test_refused_sqlite_open_leaves_no_lease_behind(tmp_path: Path) -> None:
    """Given a SQLite store stamped with format version 99
    When opening it is refused
    Then the store directory holds no used.lock
    """
    dc.Store.open(tmp_path / "s").close()
    _stamp_sqlite_format_version(tmp_path / "s", "99")
    with pytest.raises(dc.NewerStoreError):
        dc.Store.open(tmp_path / "s")

    assert not (tmp_path / "s" / "used.lock").exists()

