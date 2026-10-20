#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""List mutants the v0.10.0 tests killed (mutation/baseline-killed.txt) that survive now (#189).

Run `uv run mutmut run` first; this reads `uv run mutmut results --all true`.
Exit 1 if any baseline-killed mutant is not killed now.

    uv run scripts/mutation_check.py
    uv run scripts/mutation_check.py --results results.txt   # a saved `mutmut results --all true`
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "mutation" / "baseline-killed.txt"


def read_baseline(path: Path) -> list[str]:
    """Return the mutant names, skipping comment and blank lines."""
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def mutmut_results_text() -> str:
    """Return the current `mutmut results --all true` output."""
    command = ["uv", "run", "mutmut", "results", "--all", "true"]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True).stdout


def parse_statuses(results_text: str) -> dict[str, str]:
    """Map mutant name to status from `name: status` lines."""
    statuses: dict[str, str] = {}
    for line in results_text.splitlines():
        name, separator, status = line.strip().rpartition(": ")
        if separator and name.startswith("datacrystal."):
            statuses[name] = status
    return statuses


def lost_mutants(baseline: list[str], statuses: dict[str, str]) -> list[tuple[str, str]]:
    """Return (name, current status) for each baseline mutant not killed now."""
    return [
        (name, statuses.get(name, "absent")) for name in baseline if statuses.get(name) != "killed"
    ]


def main() -> int:
    """Print the lost mutants and return the exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", type=Path, help="saved output of `mutmut results --all true`")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    args = parser.parse_args()

    baseline = read_baseline(args.baseline)
    results_text = (
        args.results.read_text(encoding="utf-8") if args.results else mutmut_results_text()
    )
    lost = lost_mutants(baseline, parse_statuses(results_text))

    for name, status in lost:
        print(f"{name}: {status}")
    print(
        f"{len(lost)} of {len(baseline)} baseline-killed mutants are not killed now",
        file=sys.stderr,
    )
    return 1 if lost else 0


if __name__ == "__main__":
    sys.exit(main())
