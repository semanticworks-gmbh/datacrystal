#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Mutate the functions a branch changed in src/ and list the mutants no test notices.

Usage:
    uv run scripts/mutation_check.py               # the branch against origin/main
    uv run scripts/mutation_check.py main

Run it before you push a change to src/; the branch includes uncommitted edits.
The first run maps which tests reach which function, minutes for the whole suite;
mutants/ keeps the map, so later runs take a few;
a mutant a later run leaves uncaught is rerun on a rebuilt map.
Survivors are advice, never a failure: it exits 0 unless mutmut itself fails.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_changed

CAUGHT = {"killed", "caught by type check"}
NOT_RUN = "not checked"  # the run stopped before this mutant
MEANING = (
    "A survivor is a change to the function that no test notices: "
    "write the test that fails on it, or mark the line `# pragma: no mutate` with the reason; "
    "a change to a cast() type string or to a message's wording needs neither."
)
# macOS aborts a forked child that touches Objective-C state; mutmut forks one per mutant
FORK_SAFE_ENV = {"OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES"}
# mutmut's kept map of which tests reach which function; a kept map misses tests that reach new code
TEST_MAP = Path("mutants/mutmut-stats.json")


def statuses(results_text: str, patterns: list[str]) -> dict[str, str]:
    """Map each mutant a pattern selects to its status, from lines like `    name: survived`."""
    found = {}
    for line in results_text.splitlines():
        name, separator, status = line.strip().partition(": ")
        if separator and any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            found[name] = status
    return found


def report(results_text: str, patterns: list[str], show: Callable[[str], str]) -> str:
    """Write the report: counts by status, then the diff of each survivor."""
    found = statuses(results_text, patterns)
    counts = Counter(found.values())
    survivors = sorted(name for name, status in found.items() if status not in CAUGHT | {NOT_RUN})
    functions = sum(1 for pattern in patterns if fnmatch.filter(found, pattern))
    lines = [f"Mutation check: {functions} changed functions, {len(found)} mutants: " + ", ".join(
        f"{count} {status}" for status, count in sorted(counts.items())) + "."]
    if counts[NOT_RUN]:
        lines += ["", f"{counts[NOT_RUN]} mutants not run: the run stopped early, run the check again."]
    elif not survivors:
        lines += ["", "The tests catch every mutant of the changed functions."]
    for name in survivors:
        lines += ["", f"{name} ({found[name]}):", show(name).strip()]
    if survivors:
        lines += ["", MEANING]
    return "\n".join(lines) + "\n"


def no_mutants_report() -> str:
    """Write the report for a branch whose changed functions have no mutants."""
    return "Mutation check: no mutants in the functions this branch changes.\n"


def mutmut_command(*args: str) -> list[str]:
    """Build the command; `uv run` reaches the project's mutmut from this isolated script."""
    return ["uv", "run", "mutmut", *args]


def mutmut(*args: str) -> str:
    """Run mutmut quietly and return its stdout."""
    environment = os.environ | FORK_SAFE_ENV
    return subprocess.run(mutmut_command(*args), capture_output=True, text=True, check=True, env=environment).stdout


def run_mutmut(names: list[str]) -> int | None:
    """Run mutmut on the names; None when no mutant matches, else its exit code."""
    # progress streams to the terminal; stderr is kept to tell "no mutants" from a broken setup
    run = subprocess.run(
        mutmut_command("run", *names), env=os.environ | FORK_SAFE_ENV, stderr=subprocess.PIPE, text=True, check=False
    )
    if "nothing matches" in run.stderr:
        return None
    if run.returncode != 0:
        sys.stderr.write(run.stderr)
    return run.returncode


def main() -> int:
    """Mutate the changed functions, confirm the uncaught on a rebuilt map, print the report."""
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    patterns = mutation_changed.changed_patterns(base)
    map_was_kept = TEST_MAP.exists()
    exit_code = run_mutmut(patterns) if patterns else None
    if exit_code is None:
        sys.stdout.write(no_mutants_report())
        return 0
    found = statuses(mutmut("results", "--all", "true"), patterns)
    uncaught = [name for name, status in found.items() if status not in CAUGHT]
    if exit_code == 0 and map_was_kept and uncaught:
        # a stale map can only miss tests: a kill stands, an uncaught mutant is rerun on a fresh map
        print(f"Rebuilding the test map to confirm {len(uncaught)} uncaught mutants.", file=sys.stderr)
        TEST_MAP.unlink()
        exit_code = run_mutmut(uncaught)
    if exit_code:
        print(f"mutmut failed (exit {exit_code}): a broken setup, not a survivor.", file=sys.stderr)
        return exit_code
    sys.stdout.write(report(mutmut("results", "--all", "true"), patterns, lambda name: mutmut("show", name)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
