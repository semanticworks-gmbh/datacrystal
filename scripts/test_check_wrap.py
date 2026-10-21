#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for check_wrap.py.

Run: `uv run scripts/test_check_wrap.py` — exits non-zero on failure.

Plain asserts, no pytest — no project dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_wrap


def test_a_sentence_broken_over_two_lines_is_reported() -> None:
    """Given a sentence broken by hand, When checked, Then its first line is named."""
    assert check_wrap.wrap_errors("A sentence that was\nbroken by hand.\n") == [
        "line 1: hard-wrapped — write one sentence per line"
    ]


def test_one_sentence_per_line_lists_tables_fences_and_frontmatter_pass() -> None:
    """Given structured Markdown, When checked, Then nothing is reported."""
    fenced = "```python\nx = (1,\n     2)\n```\n"
    assert check_wrap.wrap_errors("One sentence.\nAnother one.\n\n- a bullet\n- another\n") == []
    assert check_wrap.wrap_errors("| a | b |\n| - | - |\n") == []
    assert check_wrap.wrap_errors(fenced) == []
    assert check_wrap.wrap_errors("---\ntitle: a\nb\n---\nDone.\n") == []


def test_a_line_ending_in_a_colon_may_introduce_the_next_line() -> None:
    """Given a line that ends in a colon, When checked, Then the next prose line is accepted."""
    assert check_wrap.wrap_errors("Run it:\nuv run x\n") == []


def test_the_since_gate_counts_only_the_lines_a_branch_added() -> None:
    """Given a diff adding line 42, When a wrap is judged, Then it counts at 41 and 42, not at 5."""
    diff = "@@ -10,0 +11,2 @@\n+new one\n+new two\n@@ -40 +42 @@\n-old\n+changed\n@@ -50,3 +53,0 @@\n"
    assert check_wrap.added_lines(diff) == {11, 12, 42}
    assert check_wrap.touches_added("line 41: hard-wrapped — write one sentence per line", {42})
    assert check_wrap.touches_added("line 42: hard-wrapped — write one sentence per line", {42})
    assert not check_wrap.touches_added("line 5: hard-wrapped — write one sentence per line", {42})


def main() -> int:
    """Run every test_ function; return 1 if any fails."""
    failures = 0
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            try:
                test()
            except AssertionError as error:
                failures += 1
                print(f"FAIL {name}: {error}")
    print("ok" if not failures else f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
