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

NO_REASON = "`noqa` has no ` - <reason>`"
PYREFLY_NO_REASON = "`pyrefly: ignore` has no ` - <reason>`"


def problems(line: str) -> list[str]:
    return [problem for _, problem in check_suppressions.source_problems(line + "\n")]


def module_file(source: str) -> Path:
    path = Path(tempfile.mkdtemp()) / "module.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_a_line_without_a_suppression_has_no_findings() -> None:
    """Given a line whose comment only talks about noqa and ignore
    When the line is checked
    Then there is no finding
    """
    assert problems("x = 1  # an ordinary comment about noqa and ignore") == []


def test_a_ble001_noqa_with_a_reason_passes() -> None:
    """Given a noqa for BLE001 with a reason
    When the line is checked
    Then there is no finding
    """
    assert problems("x = 1  # noqa: BLE001 - the contract says so") == []


def test_a_try004_noqa_with_a_reason_passes() -> None:
    """Given a noqa for TRY004 with a reason
    When the line is checked
    Then there is no finding
    """
    assert problems("x = 1  # noqa: TRY004 - the contract says so") == []


def test_a_up007_noqa_with_a_reason_passes() -> None:
    """Given a noqa for UP007 with a reason
    When the line is checked
    Then there is no finding
    """
    assert problems("x = 1  # noqa: UP007 - the contract says so") == []


def test_an_ann401_noqa_with_a_reason_passes() -> None:
    """Given a noqa for ANN401 with a reason
    When the line is checked
    Then there is no finding
    """
    assert problems("x = 1  # noqa: ANN401 - the contract says so") == []


def test_a_blanket_noqa_is_found() -> None:
    """Given a noqa that names no code
    When the line is checked
    Then the finding says the noqa is blanket
    """
    assert problems("x = 1  # noqa") == ["a blanket `noqa` names no code"]


def test_a_noqa_with_another_code_is_found() -> None:
    """Given a noqa for C901, which src/ may not waive
    When the line is checked
    Then the finding names C901
    """
    assert problems("x = 1  # noqa: C901 - too long") == [
        "`noqa` for C901 is not allowed in src/, fix the code"
    ]


def test_a_noqa_with_one_allowed_and_one_other_code_is_found() -> None:
    """Given a noqa for BLE001 and PLR2004
    When the line is checked
    Then the finding names only PLR2004
    """
    assert problems("x = 1  # noqa: BLE001, PLR2004 - both") == [
        "`noqa` for PLR2004 is not allowed in src/, fix the code"
    ]


def test_a_noqa_without_a_reason_is_found() -> None:
    """Given a noqa for BLE001 with no reason
    When the line is checked
    Then the finding says the reason is missing
    """
    assert problems("x = 1  # noqa: BLE001") == [NO_REASON]


def test_a_noqa_with_an_empty_reason_is_found() -> None:
    """Given a noqa for BLE001 whose reason is only a dash
    When the line is checked
    Then the finding says the reason is missing
    """
    assert problems("x = 1  # noqa: BLE001 -") == [NO_REASON]


def test_a_noqa_in_capitals_is_checked_like_a_lower_case_one() -> None:
    """Given a `# NOQA: PLR2004` that ruff honours
    When the line is checked
    Then the finding names PLR2004
    """
    assert problems("x = 1  # NOQA: PLR2004 - upper case") == [
        "`noqa` for PLR2004 is not allowed in src/, fix the code"
    ]


def test_a_noqa_in_mixed_case_without_a_space_is_checked() -> None:
    """Given a `# NoQA:PLR2004` that ruff honours
    When the line is checked
    Then the finding names PLR2004
    """
    assert problems("x = 1  # NoQA:PLR2004 - mixed case") == [
        "`noqa` for PLR2004 is not allowed in src/, fix the code"
    ]


def test_a_pyrefly_ignore_with_a_reason_passes() -> None:
    """Given a pyrefly ignore with a code and a reason
    When the line is checked
    Then there is no finding
    """
    assert problems("x = 1  # pyrefly: ignore[bad-return] - documented wire-null return") == []


def test_a_pyrefly_ignore_without_a_reason_is_found() -> None:
    """Given a pyrefly ignore with a code and no reason
    When the line is checked
    Then the finding says the reason is missing
    """
    assert problems("x = 1  # pyrefly: ignore[bad-return]") == [PYREFLY_NO_REASON]


def test_a_pyrefly_ignore_followed_by_a_bare_comment_is_found() -> None:
    """Given a pyrefly ignore whose next comment is not a ` - <reason>`
    When the line is checked
    Then the finding says the reason is missing
    """
    assert problems("x = 1  # pyrefly: ignore[bad-return]  # because") == [PYREFLY_NO_REASON]


def test_a_blanket_pyrefly_ignore_is_found() -> None:
    """Given a pyrefly ignore with no code
    When the line is checked
    Then the finding says the ignore is blanket
    """
    assert problems("x = 1  # pyrefly: ignore - why") == [
        "a blanket `pyrefly: ignore` names no code"
    ]


def test_a_type_ignore_is_found() -> None:
    """Given a `type: ignore`
    When the line is checked
    Then there is one finding
    """
    assert len(problems("x = 1  # type: ignore")) == 1


def test_a_file_wide_pyrefly_ignore_is_found() -> None:
    """Given a file-wide `pyrefly: ignore-errors`
    When the line is checked
    Then the finding refuses a file-wide suppression
    """
    assert problems("# pyrefly: ignore-errors") == ["a file-wide suppression is refused"]


def test_a_file_wide_ruff_noqa_is_found() -> None:
    """Given a file-wide `ruff: noqa`
    When the line is checked
    Then the finding refuses a file-wide suppression
    """
    assert problems("# ruff: noqa") == ["a file-wide suppression is refused"]


def test_a_file_wide_flake8_noqa_is_found() -> None:
    """Given a file-wide `flake8: noqa`, which ruff honours
    When the line is checked
    Then the finding refuses a file-wide suppression
    """
    assert problems("# flake8: noqa") == ["a file-wide suppression is refused"]


def test_two_suppressions_on_one_line_pass_when_each_has_its_reason() -> None:
    """Given a noqa and a pyrefly ignore on one line, each with a reason
    When the line is checked
    Then there is no finding
    """
    both = "x = 1  # noqa: UP007 - no | spelling  # pyrefly: ignore[invalid-annotation] - same"
    assert problems(both) == []


def test_two_suppressions_on_one_line_are_each_checked() -> None:
    """Given a noqa with a reason and a pyrefly ignore without one on one line
    When the line is checked
    Then the finding is about the pyrefly ignore only
    """
    mixed = "x = 1  # noqa: UP007 - no | spelling  # pyrefly: ignore[invalid-annotation]"
    assert problems(mixed) == [PYREFLY_NO_REASON]


def test_a_noqa_inside_a_string_is_not_a_suppression() -> None:
    """Given a string literal that contains `# noqa`
    When the line is checked
    Then there is no finding
    """
    assert problems('x = "# noqa"') == []


def test_the_finding_names_its_line() -> None:
    """Given a module whose second line has a blanket noqa
    When its source is checked
    Then the finding carries line 2
    """
    assert check_suppressions.source_problems("a = 1\nb = 2  # noqa\n") == [
        (2, "a blanket `noqa` names no code")
    ]


def test_main_exits_one_on_a_finding() -> None:
    """Given a file with a blanket noqa
    When main checks it
    Then the exit code is 1
    """
    bad = module_file("x = 1  # noqa\n")
    assert check_suppressions.main([str(bad)]) == 1


def test_main_exits_zero_on_a_clean_file() -> None:
    """Given a file with a reasoned noqa
    When main checks it
    Then the exit code is 0
    """
    clean = module_file("x = 1  # noqa: BLE001 - reason\n")
    assert check_suppressions.main([str(clean)]) == 0


def main() -> int:
    tests: list[tuple[str, Callable[[], None]]] = [
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    ]
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
