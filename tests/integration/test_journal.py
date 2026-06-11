"""The journal example is an executable contract (KICKOFF §2): it must run
twice from a clean directory, with run 2 finding run 1's data, exercising
the M2 scenes (unique keys + recovery, frozen events, Lazy attachments,
get_many backlinks, indexed queries, the async session)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

JOURNAL = Path("examples/journal/journal.py")


def _run(store_dir: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(JOURNAL), str(store_dir)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_journal_runs_twice_and_run_two_finds_run_one(tmp_path):
    store_dir = tmp_path / "journal.store"
    first = _run(store_dir)
    assert "run 1: journal started" in first
    assert "journal: 1 entries, 2 specimens" in first
    assert "async session appended" in first

    second = _run(store_dir)
    assert "run 1" not in second  # no reseeding
    # run 1 left 2 entries (1 seeded + 1 async) and 3 specimens (+1 relabeled)
    assert "journal: 2 entries, 3 specimens" in second
    assert "collision refused" in second
    assert "frozen event: mutation refused" in second
    assert "lazy-loaded, identity preserved" in second
    assert "[planned — M3]" in second  # honesty marker stays until it lands
