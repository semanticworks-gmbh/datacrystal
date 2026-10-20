"""Fitness (Sprint 13 / #157, gate C12): the federation tests can't false-green.

Replication is a permanent correctness surface, and the build is agent-written —
an agent can produce a follower test that runs memory-only, or a "fail-closed"
guard test whose only assertion is ``pytest.raises`` (it catches the exception
but never proves the bad value did NOT land). This meta-test polices the
federation tests so those shortcuts go RED.

Convention (so the build stories land green): federation tests live in files
matching :data:`_FED_GLOBS` under ``tests/`` — ``test_federation*`` /
``test_follower*`` / ``test_occ*``. The #149–#156 suites now exist; these meters
police them (both-backends coverage, no vacuous ``pytest.raises``-only asserts).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
_TESTS = ROOT / "tests"
_FED_GLOBS = ("test_federation*.py", "test_follower*.py", "test_occ*.py")
_SELF = pathlib.Path(__file__).name


def _fed_files() -> list[pathlib.Path]:
    seen: dict[pathlib.Path, None] = {}
    for glob in _FED_GLOBS:
        for path in _TESTS.rglob(glob):
            if path.name != _SELF:
                seen[path] = None
    return list(seen)


def _test_defs(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name.startswith("test_")
    ]


def _uses_raises(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Attribute) and func.attr == "raises":
                return True
            if isinstance(func, ast.Name) and func.id == "raises":
                return True
    return False


def _has_assert(node: ast.AST) -> bool:
    return any(isinstance(sub, ast.Assert) for sub in ast.walk(node))


def _raises_without_assert() -> list[str]:
    offenders: list[str] = []
    for path in _fed_files():
        tree = ast.parse(path.read_text(), str(path))
        offenders.extend(
            f"{path.relative_to(ROOT)}::{fn.name}"
            for fn in _test_defs(tree)
            if _uses_raises(fn) and not _has_assert(fn)
        )
    return offenders


_FED_FILE_NAMES = sorted(str(path.relative_to(ROOT)) for path in _fed_files())


def test_federation_test_files_are_found() -> None:
    """Given the tests directory
    When the federation test files are globbed
    Then at least one file is found, so the gates below police something
    """
    assert _FED_FILE_NAMES != []


@pytest.mark.parametrize("relative_path", _FED_FILE_NAMES)
def test_federation_tests_run_on_both_backends(relative_path: str) -> None:
    """Given a federation test file
    When its source is read
    Then it uses the store_factory fixture or parametrizes sqlite

    A follower/OCC test that never touches sqlite is a memory-only false-green.
    """
    source = (ROOT / relative_path).read_text()

    assert "store_factory" in source or "sqlite" in source, (
        f"{relative_path}: federation tests must run on BOTH backends — use the "
        "store_factory fixture or parametrize sqlite. A memory-only follower "
        "test silently never exercises the sqlite read/commit path."
    )


def test_federation_negative_tests_assert_post_state() -> None:
    """Given every federation test that uses pytest.raises
    When its body is searched for an assert
    Then none lacks one, so each proves the bad value did not land

    The design requires fail-closed guards to assert the bad value did NOT land
    (watermark unchanged, record byte-identical, skew field absent), not merely
    that an exception was raised.
    """
    offenders = _raises_without_assert()

    assert offenders == [], (
        "federation negative tests use pytest.raises with no post-state assert "
        f"(prove the bad value did not land): {offenders}"
    )
