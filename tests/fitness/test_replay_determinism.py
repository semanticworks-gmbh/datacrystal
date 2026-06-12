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


def test_same_process_replay_is_byte_identical():
    assert stream() == stream()


@pytest.mark.parametrize("seeds", [("0", "42"), ("1", "271828")])
def test_delta_stream_is_identical_across_hash_seeds(seeds):
    digests = []
    for seed in seeds:
        result = subprocess.run(
            [sys.executable, "-c", GENERATOR],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
            timeout=120,
            check=True,
        )
        digest = result.stdout.strip()
        assert len(digest) == 64, f"generator produced no digest: {result.stderr}"
        digests.append(digest)
    assert digests[0] == digests[1], (
        f"PYTHONHASHSEED={seeds[0]} and ={seeds[1]} produced different delta "
        "streams — hash-order iteration leaked into the emission path "
        "(replay determinism is a public-contract property, invariant 5)"
    )
