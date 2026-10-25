#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""List the mutmut mutant-name patterns of the functions a branch changed in src/.

Usage:
    uv run scripts/mutation_changed.py origin/main      # one pattern per line
    mutmut run $(uv run scripts/mutation_changed.py origin/main)

A function counts as changed when a line the diff added or deleted lies inside it.
Mutmut skips a decorated function, unless its one decorator is @staticmethod or @classmethod.
It never mutates __new__, __setattr__ or __getattribute__.
Those functions get no pattern.
Mutmut mutates module-level functions and the methods of module-level classes,
so a nested function belongs to its outer function.
Edits outside any function (constants, imports, class attributes) have no mutants.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

SOURCE_ROOT = Path("src")
PACKAGE_DIR = "src/datacrystal"
CLASS_SEPARATOR = "ǁ"  # mutmut's separator in mangled method names
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$")


def changed_lines(diff: str) -> dict[str, set[int]]:
    """Map each changed file to the new-side line numbers of its hunks.

    A pure deletion marks the lines on both sides of the gap.
    """
    lines_by_file: dict[str, set[int]] = {}
    current: set[int] | None = None
    for line in diff.splitlines():
        if file_match := FILE_HEADER.match(line):
            current = lines_by_file.setdefault(file_match.group(1), set())
        elif (hunk := HUNK_HEADER.match(line)) and current is not None:
            start = int(hunk.group(1))
            count = 1 if hunk.group(2) is None else int(hunk.group(2))
            current.update(range(start, start + count) if count else (start, start + 1))
    return lines_by_file


def module_name(path: str) -> str:
    """Turn `src/datacrystal/_store.py` into `datacrystal._store`; a package keeps its name."""
    parts = list(Path(path).relative_to(SOURCE_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


NEVER_MUTATED_NAMES = {"__new__", "__setattr__", "__getattribute__"}
PLAIN_DECORATORS = {"staticmethod", "classmethod"}


def _has_mutants(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if node.name in NEVER_MUTATED_NAMES:
        return False
    if not node.decorator_list:
        return True
    only = node.decorator_list[0]
    return len(node.decorator_list) == 1 and isinstance(only, ast.Name) and only.id in PLAIN_DECORATORS


def function_spans(source: str) -> list[tuple[str, int, int]]:
    """Return the mangled name stem, first and last line of each function mutmut mutates."""
    spans = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if _has_mutants(node):
                    spans.append((f"x_{node.name}", _first_line(node), node.end_lineno or node.lineno))
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and _has_mutants(member):
                    stem = f"x{CLASS_SEPARATOR}{node.name}{CLASS_SEPARATOR}{member.name}"
                    spans.append((stem, _first_line(member), member.end_lineno or member.lineno))
    return spans


def _first_line(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])


def patterns_for(module: str, source: str, lines: set[int]) -> list[str]:
    """Return the `module.stem__mutmut_*` pattern of each function holding a changed line."""
    return [
        f"{module}.{stem}__mutmut_*"
        for stem, first, last in function_spans(source)
        if any(first <= line <= last for line in lines)
    ]


def git(*args: str) -> str:
    """Run git and return its stdout."""
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def main() -> int:
    """Print the patterns of the functions changed since the base ref."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base", help="git ref the branch is compared with (merge base to HEAD)")
    parser.add_argument("--worktree", action="store_true", help="compare the base with the working tree instead of HEAD")
    args = parser.parse_args()
    range_args = [args.base] if args.worktree else [f"{args.base}...HEAD"]
    diff = git("diff", "-U0", "--diff-filter=d", *range_args, "--", f"{PACKAGE_DIR}/*.py")
    for path, lines in sorted(changed_lines(diff).items()):
        source = Path(path).read_text(encoding="utf-8")
        for pattern in patterns_for(module_name(path), source, lines):
            print(pattern)
    return 0


if __name__ == "__main__":
    sys.exit(main())
