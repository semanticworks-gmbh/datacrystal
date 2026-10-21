#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for check_suppressions.py — one passing and one failing line per rule.

Run: `uv run scripts/test_check_suppressions.py` — exits non-zero on failure.

Plain asserts, no pytest — no project dependency.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_suppressions


def problems(line: str) -> list[str]:
    return [problem for _, problem in check_suppressions.source_problems(line + "\n")]


def test_a_line_without_a_suppression_has_no_findings() -> None:
    assert problems("x = 1  # an ordinary comment about noqa and ignore") == []


def test_a_noqa_with_an_allowed_code_and_a_reason_passes() -> None:
    for code in ("BLE001", "TRY004", "UP007", "ANN401"):
        assert problems(f"x = 1  # noqa: {code} - the contract says so") == []


def test_a_blanket_noqa_is_found() -> None:
    assert problems("x = 1  # noqa") == ["a blanket `noqa` names no code"]


def test_a_noqa_with_another_code_is_found() -> None:
    assert problems("x = 1  # noqa: C901 - too long") == ["`noqa` for C901 is not allowed in src/, fix the code"]


def test_a_noqa_with_one_allowed_and_one_other_code_is_found() -> None:
    assert problems("x = 1  # noqa: BLE001, PLR2004 - both") == ["`noqa` for PLR2004 is not allowed in src/, fix the code"]


def test_a_noqa_without_a_reason_is_found() -> None:
    assert problems("x = 1  # noqa: BLE001") == ["`noqa` has no ` - <reason>`"]
    assert problems("x = 1  # noqa: BLE001 -") == ["`noqa` has no ` - <reason>`"]


def test_a_pyrefly_ignore_with_a_reason_passes() -> None:
    assert problems("x = 1  # pyrefly: ignore[bad-return] - documented wire-null return") == []


def test_a_pyrefly_ignore_without_a_reason_is_found() -> None:
    assert problems("x = 1  # pyrefly: ignore[bad-return]") == ["`pyrefly: ignore` has no ` - <reason>`"]
    assert problems("x = 1  # pyrefly: ignore[bad-return]  # because") == ["`pyrefly: ignore` has no ` - <reason>`"]


def test_a_blanket_pyrefly_ignore_is_found() -> None:
    assert problems("x = 1  # pyrefly: ignore - why") == ["a blanket `pyrefly: ignore` names no code"]


def test_type_ignore_and_file_wide_ignores_are_found() -> None:
    assert len(problems("x = 1  # type: ignore")) == 1
    assert len(problems("# pyrefly: ignore-errors")) == 1
    assert len(problems("# ruff: noqa")) == 1


def test_two_suppressions_on_one_line_are_each_checked() -> None:
    both = "x = 1  # noqa: UP007 - no | spelling  # pyrefly: ignore[invalid-annotation] - same"
    assert problems(both) == []
    assert problems("x = 1  # noqa: UP007 - no | spelling  # pyrefly: ignore[invalid-annotation]") == ["`pyrefly: ignore` has no ` - <reason>`"]


def test_a_noqa_inside_a_string_is_not_a_suppression() -> None:
    assert problems('x = "# noqa"') == []


def test_the_finding_names_its_line() -> None:
    assert check_suppressions.source_problems("a = 1\nb = 2  # noqa\n") == [(2, "a blanket `noqa` names no code")]


def test_main_exits_one_on_a_finding_and_zero_on_none() -> None:
    with tempfile.TemporaryDirectory() as directory:
        bad, clean = Path(directory) / "bad.py", Path(directory) / "clean.py"
        bad.write_text("x = 1  # noqa\n", encoding="utf-8")
        clean.write_text("x = 1  # noqa: BLE001 - reason\n", encoding="utf-8")
        assert check_suppressions.main([str(bad)]) == 1
        assert check_suppressions.main([str(clean)]) == 0


def main() -> int:
    tests: list[tuple[str, Callable[[], None]]] = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for name, test in tests:
        try:
            test()
        except AssertionError as problem:
            failed += 1
            print(f"FAIL {name}: {problem}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
