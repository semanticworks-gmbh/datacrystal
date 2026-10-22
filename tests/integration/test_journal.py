"""The journal example is an executable contract (KICKOFF §2): it must run
twice from a clean directory, with run 2 finding run 1's data, exercising
the M2 scenes (unique keys + recovery, frozen events, Lazy attachments,
get_many backlinks, indexed queries, the async session) and the M3 scenes
(worker-thread snapshot reads, a commit-delta consumer on the pipeline).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

JOURNAL = Path("examples/journal/journal.py")


def _journal_command(store_dir: Path) -> list[str]:
    return [sys.executable, str(JOURNAL), str(store_dir)]


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    """Return a clean store directory for the journal example."""
    return tmp_path / "journal.store"


@pytest.fixture
def first_run(store_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the journal example once on a clean directory, leaving its store behind."""
    return subprocess.run(
        _journal_command(store_dir), capture_output=True, text=True, timeout=120, check=False
    )


def test_first_run_exits_cleanly(store_dir: Path) -> None:
    """Given a clean store directory
    When the journal example runs
    Then it exits with status 0
    """
    first = subprocess.run(
        _journal_command(store_dir), capture_output=True, text=True, timeout=120, check=False
    )

    assert first.returncode == 0, first.stderr


@pytest.mark.parametrize(
    "scene",
    [
        "run 1: journal started",
        "journal: 1 entries, 2 specimens",
        "async session appended",
    ],
)
def test_first_run_prints_its_seeding_scenes(store_dir: Path, scene: str) -> None:
    """Given a clean store directory
    When the journal example runs
    Then its output contains a seeding scene
    """
    first = subprocess.run(
        _journal_command(store_dir), capture_output=True, text=True, timeout=120, check=False
    )

    assert scene in first.stdout


def test_second_run_exits_cleanly(
    store_dir: Path, first_run: subprocess.CompletedProcess[str]
) -> None:
    """Given the store directory the first run left behind
    When the journal example runs again
    Then it exits with status 0
    """
    second = subprocess.run(
        _journal_command(store_dir), capture_output=True, text=True, timeout=120, check=False
    )

    assert second.returncode == 0, second.stderr


def test_second_run_does_not_reseed(
    store_dir: Path, first_run: subprocess.CompletedProcess[str]
) -> None:
    """Given the store directory the first run left behind
    When the journal example runs again
    Then its output has no first-run scene
    """
    second = subprocess.run(
        _journal_command(store_dir), capture_output=True, text=True, timeout=120, check=False
    )

    assert "run 1" not in second.stdout


@pytest.mark.parametrize(
    "scene",
    [
        # run 1 left 2 entries (1 seeded + 1 async) and 3 specimens (+1 relabeled)
        "journal: 2 entries, 3 specimens",
        "collision refused",
        "frozen event: mutation refused",
        "lazy-loaded, identity preserved",
        "worker-thread snapshot at tid=",
        "delta consumer saw commit tid=",
    ],
)
def test_second_run_finds_the_first_runs_data_and_prints_its_scenes(
    store_dir: Path, first_run: subprocess.CompletedProcess[str], scene: str
) -> None:
    """Given the store directory the first run left behind
    When the journal example runs again
    Then its output contains a scene that needs run 1's data
    """
    second = subprocess.run(
        _journal_command(store_dir), capture_output=True, text=True, timeout=120, check=False
    )

    assert scene in second.stdout


def test_second_run_leaves_no_planned_marker(
    store_dir: Path, first_run: subprocess.CompletedProcess[str]
) -> None:
    """Given the store directory the first run left behind
    When the journal example runs again
    Then no "[planned — M3]" honesty marker is left in its output, as the M3 scenes are real
    """
    second = subprocess.run(
        _journal_command(store_dir), capture_output=True, text=True, timeout=120, check=False
    )

    assert "[planned — M3]" not in second.stdout
