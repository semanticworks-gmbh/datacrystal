"""The fsync triad (KICKOFF M2): commit / interval / never.

Gates are *settings*, not wall-clock (perf-gate principle): we assert the
SQLite pragmas each policy promises, plus that data written under every
policy survives a clean close+reopen. The crash-window differences between
the policies are an OS/power-loss property no in-process test can observe
honestly — the SIGKILL crash gate pins ``"commit"`` for the strongest claim.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

import datacrystal as dc
from datacrystal._storage.sqlite import SqliteBackend
from tests.conftest import Mineral

# PRAGMA synchronous: OFF=0, NORMAL=1, FULL=2


@pytest.mark.parametrize(
    ("policy", "expected_synchronous"), [("commit", 2), ("interval", 1), ("never", 0)]
)
def test_policy_sets_the_promised_synchronous_pragma(
    tmp_path: Path, policy: str, expected_synchronous: int
) -> None:
    """Given a SQLite backend opened with a durability policy
    When its synchronous pragma is read
    Then it holds the level the policy promises
    """
    with closing(SqliteBackend(tmp_path / "t.sqlite", durability=policy)) as backend:
        (synchronous,) = backend._conn.execute("PRAGMA synchronous").fetchone()
    assert synchronous == expected_synchronous


@pytest.mark.parametrize("policy", ["commit", "interval", "never"])
def test_every_policy_runs_the_journal_in_wal_mode(tmp_path: Path, policy: str) -> None:
    """Given a SQLite backend opened with a durability policy
    When its journal mode is read
    Then it is wal
    """
    with closing(SqliteBackend(tmp_path / "t.sqlite", durability=policy)) as backend:
        (journal_mode,) = backend._conn.execute("PRAGMA journal_mode").fetchone()
    assert journal_mode == "wal"


def test_unknown_policy_is_rejected(tmp_path: Path) -> None:
    """Given the retired policy name "full"
    When a SQLite backend is opened with it
    Then a ValueError names the durability setting
    """
    with pytest.raises(ValueError, match="durability"):
        SqliteBackend(tmp_path / "t.sqlite", durability="full")


@pytest.mark.parametrize("policy", ["commit", "interval", "never"])
def test_every_policy_round_trips_after_clean_close(tmp_path: Path, policy: str) -> None:
    """Given a quartz committed and the store closed under a durability policy
    When the store is reopened
    Then the quartz is found by its key
    """
    with dc.Store.open(tmp_path / "s", durability=policy, lock_ttl=0.5) as store:
        store.store(Mineral(qid="Q43010", name="quartz"))
        store.commit()
    with dc.Store.open(tmp_path / "s", durability=policy, lock_ttl=0.5) as reopened:
        found = reopened.get(Mineral, qid="Q43010")
    assert found is not None
    assert found.name == "quartz"
