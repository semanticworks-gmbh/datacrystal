#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for mutation_changed.py — small fixture sources and diffs, one behaviour each.

Run: `uv run scripts/test_mutation_changed.py` — exits non-zero on failure.

Plain asserts, no pytest — no project dependency.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_changed
import mutation_summary

SOURCE = '''\
LIMIT = 3


def free(x):
    return x + 1


@decorator
def decorated(x):
    return x


class Box:
    kind = "box"

    def put(self, item):
        def inner():
            return item

        return inner()

    async def fetch(self):
        return 1
'''
# free: 4-5, decorated: 8-10 (decorator on 8), Box.put: 15-19 (inner on 16-17), Box.fetch: 21-22
MODULE = "datacrystal._box"


def patterns(*lines: int) -> list[str]:
    return mutation_changed.patterns_for(MODULE, SOURCE, set(lines))


def test_a_changed_line_in_a_module_function_names_that_function() -> None:
    """Given a module-level function
    When a line of its body changed
    Then its pattern is listed
    """
    assert patterns(5) == ["datacrystal._box.x_free__mutmut_*"]


def test_a_changed_decorator_names_the_decorated_function() -> None:
    """Given a decorated function
    When only its decorator line changed
    Then its pattern is listed
    """
    assert patterns(8) == ["datacrystal._box.x_decorated__mutmut_*"]


def test_a_changed_line_in_a_method_names_the_method_with_its_class() -> None:
    """Given a method of a module-level class
    When a line of its body changed
    Then the pattern carries the class name between mutmut's separators
    """
    assert patterns(19) == ["datacrystal._box.xǁBoxǁput__mutmut_*"]


def test_a_changed_line_in_a_nested_function_names_the_outer_method() -> None:
    """Given a function nested in a method
    When a line of the nested function changed
    Then the outer method is listed, as mutmut mutates it as a whole
    """
    assert patterns(17) == ["datacrystal._box.xǁBoxǁput__mutmut_*"]


def test_an_async_method_is_listed() -> None:
    """Given an async method
    When its body changed
    Then its pattern is listed
    """
    assert patterns(22) == ["datacrystal._box.xǁBoxǁfetch__mutmut_*"]


def test_a_changed_constant_and_class_attribute_list_nothing() -> None:
    """Given edits to a module constant and a class attribute
    When the patterns are computed
    Then none are listed, as no mutant exists for them
    """
    assert patterns(1, 13) == []


def test_two_changed_functions_list_both_in_source_order() -> None:
    """Given edits in two functions
    When the patterns are computed
    Then both are listed in source order
    """
    assert patterns(22, 5) == [
        "datacrystal._box.x_free__mutmut_*",
        "datacrystal._box.xǁBoxǁfetch__mutmut_*",
    ]


DIFF = """\
diff --git a/src/datacrystal/_box.py b/src/datacrystal/_box.py
--- a/src/datacrystal/_box.py
+++ b/src/datacrystal/_box.py
@@ -4,0 +5,2 @@ def free(x):
+    y = 1
+    z = 2
@@ -15 +17 @@ class Box:
-    old
+    new
@@ -20,2 +21,0 @@ class Box:
-    gone
-    gone too
diff --git a/src/datacrystal/_other.py b/src/datacrystal/_other.py
--- a/src/datacrystal/_other.py
+++ b/src/datacrystal/_other.py
@@ -1 +1 @@
-a
+b
"""


def test_added_and_replaced_hunks_give_their_new_side_lines() -> None:
    """Given a diff with an added block and a one-line replacement
    When the changed lines are read
    Then the file holds exactly the new-side line numbers
    """
    lines = mutation_changed.changed_lines(DIFF)["src/datacrystal/_box.py"]
    assert {5, 6, 17} <= lines


def test_a_pure_deletion_marks_the_lines_around_the_gap() -> None:
    """Given a hunk that only deletes lines
    When the changed lines are read
    Then the line before and the line after the gap are marked
    """
    lines = mutation_changed.changed_lines(DIFF)["src/datacrystal/_box.py"]
    assert {21, 22} <= lines


def test_each_file_of_a_diff_has_its_own_lines() -> None:
    """Given a diff over two files
    When the changed lines are read
    Then each file maps to its own lines
    """
    assert mutation_changed.changed_lines(DIFF)["src/datacrystal/_other.py"] == {1}


def test_a_module_path_becomes_a_dotted_name_and_a_package_init_the_package() -> None:
    """Given a module path and a package __init__ path
    When the module names are derived
    Then the first is dotted and the second is the package
    """
    assert mutation_changed.module_name("src/datacrystal/_storage/lock.py") == "datacrystal._storage.lock"
    assert mutation_changed.module_name("src/datacrystal/_storage/__init__.py") == "datacrystal._storage"


RESULTS = """\
    datacrystal._box.x_free__mutmut_1: survived
    datacrystal._box.x_free__mutmut_2: killed
    datacrystal._box.x_free__mutmut_3: survived
    datacrystal._other.x_old__mutmut_1: survived
    datacrystal._other.x_old__mutmut_2: not checked
"""
FREE = ["datacrystal._box.x_free__mutmut_*"]


def show(name: str) -> str:
    return f"diff of {name}"


def test_only_mutants_of_the_given_patterns_are_counted() -> None:
    """Given results that also hold another function's survivor
    When the statuses of one pattern are read
    Then the other function is left out
    """
    assert mutation_summary.statuses(RESULTS, FREE) == {
        "datacrystal._box.x_free__mutmut_1": "survived",
        "datacrystal._box.x_free__mutmut_2": "killed",
        "datacrystal._box.x_free__mutmut_3": "survived",
    }


def test_the_comment_starts_with_the_marker_and_counts_by_status() -> None:
    """Given one function with two survivors and one kill
    When the comment is written
    Then it starts with the marker and counts the statuses
    """
    comment = mutation_summary.summarize(RESULTS, FREE, show)
    assert comment.startswith("<!-- mutation-check -->\n")
    assert "1 changed functions, 3 mutants: 1 killed, 2 survived." in comment


def test_each_survivor_shows_its_diff_and_the_killed_mutant_does_not() -> None:
    """Given a function with two survivors and one kill
    When the comment is written
    Then the survivors' diffs are in it and the kill's is not
    """
    comment = mutation_summary.summarize(RESULTS, FREE, show)
    assert "diff of datacrystal._box.x_free__mutmut_1" in comment
    assert "diff of datacrystal._box.x_free__mutmut_3" in comment
    assert "diff of datacrystal._box.x_free__mutmut_2" not in comment


def test_survivors_beyond_the_limit_are_counted_not_shown() -> None:
    """Given 32 survivors
    When the comment is written
    Then 30 diffs are shown and the other 2 are counted
    """
    results = "".join(f"    datacrystal._box.x_free__mutmut_{n}: survived\n" for n in range(1, 33))
    comment = mutation_summary.summarize(results, FREE, show)
    assert comment.count("<details>") == 30
    assert "2 more survivors not shown." in comment


def test_a_run_without_survivors_says_so() -> None:
    """Given results where every mutant was killed
    When the comment is written
    Then it says no survivor was found
    """
    results = "    datacrystal._box.x_free__mutmut_1: killed\n"
    assert "No survivor" in mutation_summary.summarize(results, FREE, show)


def main() -> int:
    tests: list[Callable[[], None]] = [
        value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
