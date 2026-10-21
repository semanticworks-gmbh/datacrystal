"""Fitness #5 — replay determinism (KICKOFF §7, M3 exit criterion).

The same operation sequence must produce the same delta stream byte for
byte: TIDs are sequence-derived (never wall-clock), OIDs/CIDs are counters,
ops are in capture order. The deep variant runs the golden session in child
processes under DIFFERENT PYTHONHASHSEED values — if any set/hash-order
iteration leaks into the stream, the digests diverge here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.contract.golden_gen import stream

REPO_ROOT = Path(__file__).parents[2]
GENERATOR = "from tests.contract.golden_gen import main; main()"


def _env_with_hash_seed(seed: str) -> dict[str, str]:
    return {**os.environ, "PYTHONHASHSEED": seed}


def test_same_process_replay_is_byte_identical() -> None:
    """Given the golden session
    When its delta stream is generated twice in one process
    Then both streams are byte-identical
    """
    first_stream = stream()
    second_stream = stream()

    assert first_stream == second_stream


@pytest.mark.parametrize(("first_seed", "second_seed"), [("0", "42"), ("1", "271828")])
def test_delta_stream_is_identical_across_hash_seeds(first_seed: str, second_seed: str) -> None:
    """Given the golden session generator run in child processes
    When it runs under two different PYTHONHASHSEED values
    Then both runs print the same stream digest
    """
    first_run = subprocess.run(
        [sys.executable, "-c", GENERATOR],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=_env_with_hash_seed(first_seed),
        timeout=120,
        check=True,
    )
    second_run = subprocess.run(
        [sys.executable, "-c", GENERATOR],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=_env_with_hash_seed(second_seed),
        timeout=120,
        check=True,
    )

    assert len(first_run.stdout.strip()) == 64, f"no digest: {first_run.stderr}"
    assert len(second_run.stdout.strip()) == 64, f"no digest: {second_run.stderr}"
    assert first_run.stdout.strip() == second_run.stdout.strip(), (
        "hash-order iteration leaked into the emission path "
        "(replay determinism is a public-contract property, invariant 5)"
    )
