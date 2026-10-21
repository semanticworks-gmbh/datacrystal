#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Checks test modules against the standards readable-tests and spec-by-example.

Usage: uv run scripts/testrules.py [<test_*.py>...]
With no path it checks every test_*.py under tests/; exit 1 on any finding.
Copied from tetrahedron tools/testrules, 2026-10-20 (#186); owned here from now on.
The rules are standards readable-tests and spec-by-example; the self-tests in test_testrules.py list
each one.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

MAX_RULES, MAX_BULLETS, MIN_NAME_WORDS = 7, 5, 3
PURPOSE_LINE = re.compile(r"^In order to .+, .+\.$", re.MULTILINE)
RULE_MARKER = re.compile(r"(?:pytest\.mark\.)?rule\(['\"]AC-\d+['\"]\)")
ISSUE_REFERENCE = re.compile(r"#\d+")
CONTROL_FLOW = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Lambda)
COMPREHENSION = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
PLAIN_ROW_VALUE = (ast.Constant, ast.UnaryOp)
GIVEN_WHEN_THEN = ["Given", "When", "Then"]


@dataclass(frozen=True)
class Finding:
    """One rule broken at a line; ``control_flow_in_test`` keys the test's control-flow count."""

    line: int
    message: str
    control_flow_in_test: int | None = None


def is_test(node: ast.AST) -> bool:
    """Whether the node is a function named like a test."""
    return isinstance(node, FUNCTIONS) and node.name.startswith("test_")


def tests_of(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every test function of the module, module-level or inside a class."""
    found = [node for node in tree.body if is_test(node)]
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        found.extend(node for node in cls.body if is_test(node))
    return found


def is_spec_module(path: Path) -> bool:
    """Whether the file lives under tests/spec/."""
    parts = path.resolve().parts
    return any(pair == ("tests", "spec") for pair in pairwise(parts))


def call_name(node: ast.AST) -> str:
    """Return the unparsed callee of a call node, or an empty string for any other node."""
    return ast.unparse(node.func) if isinstance(node, ast.Call) else ""


def row_values(parametrize: ast.Call) -> list[ast.expr]:
    """Return the cells of every parametrize row, inside pytest.param(...) or a bare tuple."""
    cells: list[ast.expr] = []
    for row in getattr(parametrize.args[1], "elts", []):
        if isinstance(row, ast.Call):
            cells.extend(row.args)
        else:
            cells.extend(getattr(row, "elts", [row]))
    return cells


def non_function_scope(fixture: ast.Call) -> bool:
    """Whether a fixture call declares a scope other than function."""
    scope = next((kw.value for kw in fixture.keywords if kw.arg == "scope"), None)
    return scope is not None and not (isinstance(scope, ast.Constant) and scope.value == "function")


def scoped_fixture_names(tree: ast.Module) -> set[str]:
    """Return the names of the fixtures in the module whose scope is not function."""
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, FUNCTIONS)
        and any(call_name(deco).endswith("fixture") and non_function_scope(deco) for deco in node.decorator_list if isinstance(deco, ast.Call))
    }


def conftest_scoped_fixtures(path: Path) -> set[str]:
    """Return the names of the wider-scoped fixtures in the neighbouring conftest.py."""
    names: set[str] = set()
    for folder in path.resolve().parents:
        if (folder / "conftest.py").is_file():
            names |= scoped_fixture_names(ast.parse((folder / "conftest.py").read_text(encoding="utf-8")))
        if folder.name == "tests" or (folder / "pyproject.toml").exists():
            break
    return names


def control_flow_findings(tree: ast.Module) -> list[Finding]:
    """Return the findings for control flow and comprehensions outside an assert in test bodies."""
    found = []
    for test in tests_of(tree):
        inside_assert = {id(inner) for node in ast.walk(test) if isinstance(node, ast.Assert) for inner in ast.walk(node)}
        for node in ast.walk(test):
            if isinstance(node, CONTROL_FLOW):
                kind = type(node).__name__.lower()
                found.append(Finding(node.lineno, f"{kind} — a test is straight-line code", test.lineno))
            if isinstance(node, COMPREHENSION) and id(node) not in inside_assert:
                found.append(Finding(node.lineno, "comprehension outside an assert — a test is straight-line code"))
    return found


def sleep_and_row_findings(tree: ast.Module) -> list[Finding]:
    """Return the findings for a sleep() call and for a parametrize row of non-plain values."""
    found = []
    for node in ast.walk(tree):
        if call_name(node).endswith("sleep"):
            found.append(Finding(node.lineno, "sleep() — wait on a condition, never on time"))
        if call_name(node).endswith("parametrize") and len(node.args) > 1:
            for cell in row_values(node):
                if not isinstance(cell, PLAIN_ROW_VALUE):
                    found.append(Finding(cell.lineno, f"row holds `{ast.unparse(cell)}` — plain values only; an event sequence is its own test"))
    return found


def helper_assert_findings(tree: ast.Module) -> list[Finding]:
    """Return the findings for an assert inside a function that is not a test."""
    found = []
    test_ids = {id(test) for test in tests_of(tree)}
    for node in ast.walk(tree):
        if isinstance(node, FUNCTIONS) and id(node) not in test_ids:
            found.extend(Finding(inner.lineno, f"assert inside {node.name}() — the test asserts, a helper builds") for inner in ast.walk(node) if isinstance(inner, ast.Assert))
    return found


def module_findings(tree: ast.Module) -> list[Finding]:
    """Return the findings in test bodies and module-wide (control flow, sleep, asserts, rows)."""
    return control_flow_findings(tree) + sleep_and_row_findings(tree) + helper_assert_findings(tree)


def test_findings(test: ast.FunctionDef | ast.AsyncFunctionDef, scoped_fixtures: set[str]) -> list[Finding]:
    """The findings inside one test: name, docstring, fixtures and control flow."""
    found = []
    keywords = [line.split()[0] for line in (ast.get_docstring(test) or "").splitlines() if line.strip()]
    if [word for word in keywords if word in GIVEN_WHEN_THEN] != GIVEN_WHEN_THEN:
        found.append(Finding(test.lineno, f"{test.name}: docstring must read Given / When / Then"))
    if len(test.name.removeprefix("test_").split("_")) < MIN_NAME_WORDS:
        found.append(Finding(test.lineno, f"{test.name}: name must say the behaviour, {MIN_NAME_WORDS} words or more"))
    found.extend(
        Finding(test.lineno, f"{test.name}: requests `{arg.arg}`, a fixture with a wider scope than function — a function-scoped fixture requests it")
        for arg in test.args.args
        if arg.arg in scoped_fixtures
    )
    return found


def xfail_findings(tree: ast.Module) -> list[Finding]:
    """Return the findings for an xfail that is not strict or names no issue."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node).endswith("mark.xfail"):
            strict = next((kw.value for kw in node.keywords if kw.arg == "strict"), None)
            reason = next((kw.value for kw in node.keywords if kw.arg == "reason"), None)
            if not (isinstance(strict, ast.Constant) and strict.value is True):
                found.append(Finding(node.lineno, "xfail without strict=True — an open bug must fail loudly when it starts to pass"))
            if not (isinstance(reason, ast.Constant) and ISSUE_REFERENCE.search(str(reason.value))):
                found.append(Finding(node.lineno, "xfail without a reason naming its issue as #N"))
        elif isinstance(node, (*FUNCTIONS, ast.ClassDef)):
            found.extend(
                Finding(deco.lineno, "xfail without arguments — needs strict=True and a reason naming its issue as #N")
                for deco in node.decorator_list
                if ast.unparse(deco).endswith("mark.xfail")
            )
    return found


def spec_findings(tree: ast.Module) -> list[Finding]:
    """Return the findings that apply to a module under tests/spec/."""
    found = []
    head = ast.get_docstring(tree) or ""
    if not PURPOSE_LINE.search(head):
        found.append(Finding(1, "module docstring: no purpose line 'In order to <value>, <who> <does what>.'"))
    if not head.splitlines() or PURPOSE_LINE.match(head.splitlines()[0]):
        found.append(Finding(1, "module docstring: the first line is the title, not the purpose"))
    if (bullets := sum(line.startswith("- ") for line in head.splitlines())) > MAX_BULLETS:
        found.append(Finding(1, f"module docstring: {bullets} bullets, at most {MAX_BULLETS}"))
    rules = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    if len(rules) > MAX_RULES:
        found.append(Finding(rules[MAX_RULES].lineno, f"{len(rules)} rules, at most {MAX_RULES}"))
    for rule in rules:
        doc = ast.get_docstring(rule) or ""
        if not any(RULE_MARKER.fullmatch(ast.unparse(deco)) for deco in rule.decorator_list):
            found.append(Finding(rule.lineno, f'{rule.name}: not marked rule("AC-n")'))
        if doc.count(".") != 1 or "\n" in doc:
            found.append(Finding(rule.lineno, f"{rule.name}: docstring must be the rule as one sentence"))
    return found + xfail_findings(tree)


def check_file(path: Path) -> list[Finding]:
    """All findings of one test module, ordered by line."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scoped_fixtures = scoped_fixture_names(tree) | conftest_scoped_fixtures(path)
    found = module_findings(tree)
    for test in tests_of(tree):
        found.extend(test_findings(test, scoped_fixtures))
    if is_spec_module(path):
        found.extend(spec_findings(tree))
    return sorted(found, key=lambda finding: finding.line)


def main(paths: list[str]) -> int:
    """Check the given test modules, print the findings and return the exit code."""
    if not paths:
        paths = [str(path) for path in sorted(Path("tests").rglob("test_*.py"))]
    report, control_flow_tests = [], 0
    for path in paths:
        findings = check_file(Path(path))
        report.extend(f"{path}:{finding.line}: {finding.message}" for finding in findings)
        control_flow_tests += len({(path, f.control_flow_in_test) for f in findings if f.control_flow_in_test is not None})
    print("\n".join(report) or "ok")
    if report:
        print(f"{len(report)} findings, {control_flow_tests} tests with control flow")
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
