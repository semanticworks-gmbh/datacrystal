#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Turn `mutmut results` into the Markdown of the PR comment.

Usage:
    uv run scripts/mutation_changed.py origin/main > patterns.txt
    uv run scripts/mutation_summary.py patterns.txt > comment.md

An empty patterns file gives the "no mutants" comment, so an older comment is replaced.
Always exits 0 on a readable run: survivors are advice, never a failure.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

MARKER = "<!-- mutation-check -->"
SHOWN_LIMIT = 30
CAUGHT = {"killed", "caught by type check"}
MEANING = (
    "A survivor is a change to the function that no test notices: "
    "write the test that fails on it, or mark the line `# pragma: no mutate` with the reason."
)
REPRODUCE = "OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES uv run mutmut run $(uv run scripts/mutation_changed.py origin/main)"


def statuses(results_text: str, patterns: list[str]) -> dict[str, str]:
    """Map each mutant a pattern selects to its status, from lines like `    name: survived`."""
    found = {}
    for line in results_text.splitlines():
        name, separator, status = line.strip().partition(": ")
        if separator and any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            found[name] = status
    return found


def summarize(results_text: str, patterns: list[str], show: Callable[[str], str]) -> str:
    """Write the comment body: counts by status, then the diff of each survivor up to the limit."""
    found = statuses(results_text, patterns)
    counts = Counter(found.values())
    survivors = sorted(name for name, status in found.items() if status not in CAUGHT)
    functions = sum(1 for pattern in patterns if fnmatch.filter(found, pattern))
    lines = [MARKER, "### Mutation check (advisory)", ""]
    lines.append(f"{functions} changed functions, {len(found)} mutants: " + ", ".join(
        f"{count} {status}" for status, count in sorted(counts.items())) + ".")
    if not survivors:
        lines += ["", "The tests catch every mutant of the changed functions."]
    for name in survivors[:SHOWN_LIMIT]:
        lines += ["", f"<details><summary><code>{name}</code></summary>", "", "```diff", show(name).strip(), "```", "", "</details>"]
    if len(survivors) > SHOWN_LIMIT:
        lines += ["", f"{len(survivors) - SHOWN_LIMIT} more survivors not shown."]
    lines += ["", MEANING, f"Reproduce: `{REPRODUCE}`, then `uv run mutmut results` and `uv run mutmut show <name>`."]
    return "\n".join(lines) + "\n"


def mutmut_command(*args: str) -> list[str]:
    """Build the command; `uv run` reaches the project's mutmut from this isolated script."""
    return ["uv", "run", "mutmut", *args]


def mutmut(*args: str) -> str:
    """Run mutmut and return its stdout."""
    return subprocess.run(mutmut_command(*args), capture_output=True, text=True, check=True).stdout


def empty_comment() -> str:
    """Write the comment for a push whose changed functions have no mutants."""
    return f"{MARKER}\n### Mutation check (advisory)\n\nNo mutants in the functions this PR changes.\n"


def main() -> int:
    """Print the comment for the patterns in the file named on the command line."""
    patterns = [line for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if line]
    if not patterns:
        sys.stdout.write(empty_comment())
        return 0
    sys.stdout.write(summarize(mutmut("results", "--all", "true"), patterns, lambda name: mutmut("show", name)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
