#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Checks the suppression comments in src/ against standard typed-python.

Usage: uv run scripts/check_suppressions.py [<dir-or-file>...]   (default: src)
Ruff and pyrefly read the keywords in any letter case, so the check does too.
A `# noqa` names its codes, only BLE001, TRY004, UP007 or ANN401, and ends in ` - <reason>`.
A `# pyrefly: ignore[<code>]` ends in ` - <reason>`.
`type: ignore` and the file-wide `ruff: noqa`, `flake8: noqa`, `pyrefly: ignore-errors` are refused.
Prints file:line and the problem per finding; exit 1 on any.
"""

from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

ALLOWED_NOQA_CODES = {"BLE001", "TRY004", "UP007", "ANN401"}
COMMENT_BOUNDARY = re.compile(r"\s+#\s*")
NOQA = re.compile(
    r"noqa\b(?::\s*(?P<codes>[A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*))?(?P<rest>.*)$", re.IGNORECASE
)
PYREFLY_IGNORE = re.compile(
    r"pyrefly:\s*ignore\b(?P<codes>\[[^\]]+\])?(?P<rest>.*)$", re.IGNORECASE
)
FILE_WIDE = re.compile(r"(ruff|flake8):\s*noqa\b|pyrefly:\s*ignore-errors\b")
REASON = re.compile(r"^ - \S")


def segment_problems(segment: str) -> list[str]:
    """Problems of one `# ...` fragment of a comment; empty when it is fine or no suppression."""
    keyword = segment.lower()
    if keyword.startswith("type:") and "ignore" in keyword:
        return ["`type: ignore` is refused, fix the type or use a reasoned `pyrefly: ignore[...]`"]
    if FILE_WIDE.match(keyword):
        return ["a file-wide suppression is refused"]
    if keyword.startswith("noqa"):
        return noqa_problems(segment)
    if keyword.startswith("pyrefly:"):
        return pyrefly_problems(segment)
    return []


def noqa_problems(segment: str) -> list[str]:
    """Problems of a `noqa` fragment: no code, a code src/ may not waive, no reason."""
    match = NOQA.match(segment)
    if match is None or match["codes"] is None:
        return ["a blanket `noqa` names no code"]
    problems = []
    codes = {code.strip().upper() for code in match["codes"].split(",")}
    refused = sorted(codes - ALLOWED_NOQA_CODES)
    if refused:
        problems.append(f"`noqa` for {', '.join(refused)} is not allowed in src/, fix the code")
    if not REASON.match(match["rest"]):
        problems.append("`noqa` has no ` - <reason>`")
    return problems


def pyrefly_problems(segment: str) -> list[str]:
    """Problems of a `pyrefly: ignore` fragment: no code in brackets, no reason."""
    match = PYREFLY_IGNORE.match(segment)
    if match is None:
        return []
    if match["codes"] is None:
        return ["a blanket `pyrefly: ignore` names no code"]
    return [] if REASON.match(match["rest"]) else ["`pyrefly: ignore` has no ` - <reason>`"]


def source_problems(source: str) -> list[tuple[int, str]]:
    """Every (line, problem) of one module's comments."""
    found = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        for segment in COMMENT_BOUNDARY.split(token.string.lstrip("# ")):
            found.extend((token.start[0], problem) for problem in segment_problems(segment))
    return found


def main(arguments: list[str]) -> int:
    """Check every Python file under the given paths and return the exit code."""
    roots = [Path(argument) for argument in arguments] or [Path("src")]
    files = sorted(path for root in roots for path in ([root] if root.is_file() else root.rglob("*.py")))
    report = [
        f"{path}:{line}: {problem}"
        for path in files
        for line, problem in source_problems(path.read_text(encoding="utf-8"))
    ]
    print("\n".join(report) or "ok")
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
