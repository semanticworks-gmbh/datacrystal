"""The fsync triad (KICKOFF M2): commit / interval / never.

Gates are *settings*, not wall-clock (perf-gate principle): we assert the
SQLite pragmas each policy promises, plus that data written under every
policy survives a clean close+reopen. The crash-window differences between
the policies are an OS/power-loss property no in-process test can observe
honestly — the SIGKILL crash gate pins ``"commit"`` for the strongest claim.
"""

from __future__ import annotations

import pytest

import datacrystal as dc
from datacrystal._storage.sqlite import SqliteBackend

# PRAGMA synchronous: OFF=0, NORMAL=1, FULL=2
_EXPECTED_SYNCHRONOUS = {"commit": 2, "interval": 1, "never": 0}


@pytest.mark.parametrize("policy", ["commit", "interval", "never"])
def test_policy_sets_promised_pragmas(tmp_path, policy):
    backend = SqliteBackend(tmp_path / "t.sqlite", durability=policy)
    try:
        (mode,) = backend._conn.execute("PRAGMA journal_mode").fetchone()
        (sync,) = backend._conn.execute("PRAGMA synchronous").fetchone()
        assert mode == "wal"
        assert sync == _EXPECTED_SYNCHRONOUS[policy]
    finally:
        backend.close()


def test_unknown_policy_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="durability"):
        SqliteBackend(tmp_path / "t.sqlite", durability="full")  # the old name


@pytest.mark.parametrize("policy", ["commit", "interval", "never"])
def test_every_policy_round_trips_after_clean_close(tmp_path, policy):
    from tests.conftest import Mineral

    with dc.Store.open(tmp_path / "s", durability=policy, lock_ttl=0.5) as store:
        store.store(Mineral(qid="Q43010", name="quartz"))
        store.commit()
    with dc.Store.open(tmp_path / "s", durability=policy, lock_ttl=0.5) as store:
        found = store.get(Mineral, qid="Q43010")
        assert found is not None and found.name == "quartz"
