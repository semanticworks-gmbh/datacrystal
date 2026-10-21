#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Fail on a hard-wrapped Markdown sentence: one sentence per line (standard teaching-docs).

Usage:
    uv run scripts/check_wrap.py README.md docs/tutorial.md   # whole files
    uv run scripts/check_wrap.py --since origin/main          # the lines this branch added

Copied from tetrahedron's tools/architecture/office.py (wrap_errors, cmd_wrap).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
FENCE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)
SENTENCE_END = re.compile(r"[.!?:;…][)\"'`*_\]]*$")
# headings, tables, quotes, lists, HTML, fences
NOT_PROSE = re.compile(r"^(\s|[#|>*<@-]|\d+\. |```)")


def wrap_errors(text: str) -> list[str]:
    """Name each line that is prose, does not end its sentence and is followed by more prose."""
    match = FRONTMATTER.match(text)
    body, offset = (match.group(2), text.count("\n", 0, match.start(2))) if match else (text, 0)
    lines = FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), body).splitlines()
    prose = [bool(line.strip()) and not NOT_PROSE.match(line) for line in lines]
    return [
        f"line {i + 1 + offset}: hard-wrapped — write one sentence per line"
        for i in range(len(lines) - 1)
        if prose[i] and prose[i + 1] and not SENTENCE_END.search(lines[i].rstrip())
    ]


def added_lines(diff: str) -> set[int]:
    """Return the line numbers a `git diff -U0` adds to the new file."""
    hunks = re.finditer(r"^@@ [^+]*\+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE)
    return {start + k for start, count in ((int(m[1]), int(m[2] or 1)) for m in hunks) for k in range(count)}


def touches_added(problem: str, added: set[int]) -> bool:
    """Tell whether the branch wrote either of the two lines a hard wrap joins."""
    line = int(re.match(r"line (\d+):", problem)[1])  # pyrefly: ignore[unsupported-operation] - problem always starts with "line N:"
    return line in added or line + 1 in added


def git(*args: str) -> str:
    """Run git and return its stdout."""
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def check(paths: list[Path], since: str | None = None) -> int:
    """Print every finding to stderr and return the exit code."""
    if since:
        names = git("diff", "--name-only", "--diff-filter=d", f"{since}...HEAD", "--", "*.md").split()
        paths = [Path(name) for name in names]
    found = [
        f"{path}: {problem}"
        for path in paths
        for problem in wrap_errors(path.read_text(encoding="utf-8"))
        if not since or touches_added(problem, added_lines(git("diff", "-U0", f"{since}...HEAD", "--", str(path))))
    ]
    for line in found:
        print(f"✗ {line}", file=sys.stderr)
    return 1 if found else 0


def main() -> int:
    """Parse the command line and run the check."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--since", help="check only the lines added since this ref, in every Markdown file changed")
    args = parser.parse_args()
    if not args.paths and not args.since:
        parser.error("needs files or --since <ref>")
    return check(args.paths, args.since)


if __name__ == "__main__":
    sys.exit(main())
