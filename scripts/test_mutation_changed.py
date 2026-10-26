#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for mutation_changed.py and mutation_check.py — small fixtures, one behaviour each.

Run: `uv run scripts/test_mutation_changed.py` — exits non-zero on failure.

Plain asserts, no pytest — no project dependency.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_changed
import mutation_check

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

    @staticmethod
    def make():
        return 2

    @property
    def size(self):
        return 3
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


def test_a_decorated_function_has_no_mutants_and_no_pattern() -> None:
    """Given a function with a decorator that mutmut does not mutate through
    When its decorator line changed
    Then no pattern is listed
    """
    assert patterns(8) == []


def test_a_lone_staticmethod_decorator_keeps_the_pattern_and_a_property_has_none() -> None:
    """Given a @staticmethod and a @property method
    When their lines changed
    Then only the staticmethod is listed, decorator line included
    """
    assert patterns(25, 30) == ["datacrystal._box.xǁBoxǁmake__mutmut_*"]


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
    assert mutation_check.statuses(RESULTS, FREE) == {
        "datacrystal._box.x_free__mutmut_1": "survived",
        "datacrystal._box.x_free__mutmut_2": "killed",
        "datacrystal._box.x_free__mutmut_3": "survived",
    }


def test_the_report_counts_the_mutants_by_status() -> None:
    """Given one function with two survivors and one kill
    When the report is written
    Then its first line counts the statuses
    """
    report = mutation_check.report(RESULTS, FREE, show)
    assert report.startswith("Mutation check: 1 changed functions, 3 mutants: 1 killed, 2 survived.\n")


def test_each_survivor_shows_its_diff_and_the_killed_mutant_does_not() -> None:
    """Given a function with two survivors and one kill
    When the report is written
    Then the survivors' diffs are in it and the kill's is not
    """
    report = mutation_check.report(RESULTS, FREE, show)
    assert "diff of datacrystal._box.x_free__mutmut_1" in report
    assert "diff of datacrystal._box.x_free__mutmut_3" in report
    assert "diff of datacrystal._box.x_free__mutmut_2" not in report


def test_a_run_where_every_mutant_was_killed_says_so() -> None:
    """Given results where every mutant was killed
    When the report is written
    Then it says the tests catch every mutant
    """
    results = "    datacrystal._box.x_free__mutmut_1: killed\n"
    assert "catch every mutant" in mutation_check.report(results, FREE, show)


def test_mutants_without_tests_are_listed_and_not_called_caught() -> None:
    """Given a mutant with no tests and one timeout, and no survivor
    When the report is written
    Then neither is called caught and each is listed by name
    """
    results = (
        "    datacrystal._box.x_free__mutmut_1: no tests\n"
        "    datacrystal._box.x_free__mutmut_2: timeout\n"
    )
    report = mutation_check.report(results, FREE, show)
    assert "catch every mutant" not in report
    assert "diff of datacrystal._box.x_free__mutmut_1" in report
    assert "diff of datacrystal._box.x_free__mutmut_2" in report


def test_mutants_of_a_stopped_run_are_counted_not_shown_as_survivors() -> None:
    """Given a run that killed one mutant and stopped before two others
    When the report is written
    Then the two are counted as not run, not shown, and the run is not called complete
    """
    results = (
        "    datacrystal._box.x_free__mutmut_1: killed\n"
        "    datacrystal._box.x_free__mutmut_2: not checked\n"
        "    datacrystal._box.x_free__mutmut_3: not checked\n"
    )
    report = mutation_check.report(results, FREE, show)
    assert "2 mutants not run" in report
    assert "diff of datacrystal._box.x_free__mutmut_2" not in report
    assert "catch every mutant" not in report


def test_only_patterns_that_matched_a_mutant_count_as_functions() -> None:
    """Given two patterns of which one matches no mutant
    When the report is written
    Then it counts one function
    """
    results = "    datacrystal._box.x_free__mutmut_1: killed\n"
    patterns = [*FREE, "datacrystal._box.x_gone__mutmut_*"]
    assert "1 changed functions, 1 mutants" in mutation_check.report(results, patterns, show)


def test_mutmut_is_run_through_uv_so_the_project_environment_is_used() -> None:
    """Given a script that runs in an isolated environment without mutmut on PATH
    When it builds the mutmut command
    Then the command starts with uv run
    """
    assert mutation_check.mutmut_command("results")[:3] == ["uv", "run", "mutmut"]


def test_a_branch_without_mutants_says_so() -> None:
    """Given a branch whose changed functions have no mutants
    When the report is written
    Then it says there are none
    """
    assert "no mutants" in mutation_check.no_mutants_report()


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
