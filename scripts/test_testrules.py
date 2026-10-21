#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for testrules.py — one passing and one failing module per finding.

Run: `uv run scripts/test_testrules.py` — exits non-zero on failure.

Plain asserts, no pytest — no project dependency.
Every case writes a module to a temporary tests/ folder, runs the check on it
and reads the findings back.
"""

from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testrules

GWT = '    """Given a\n    When b\n    Then c\n    """\n'


def module_of(*tests: str, head: str = "") -> str:
    return head + "\n\n" + "\n\n".join(tests) + "\n"


def good_test(name: str = "test_a_reader_borrows_a_book", body: str = "    assert 1 == 1\n") -> str:
    return f"def {name}() -> None:\n{GWT}{body}"


def messages(source: str, *, spec: bool = False, conftest: str | None = None) -> list[str]:
    root = Path(tempfile.mkdtemp()) / "tests"
    folder = root / "spec" if spec else root
    folder.mkdir(parents=True)
    path = folder / "test_sample.py"
    path.write_text(source, encoding="utf-8")
    if conftest:
        (root / "conftest.py").write_text(conftest, encoding="utf-8")
    return [finding.message for finding in testrules.check_file(path)]


def mentions(found: list[str], text: str) -> bool:
    return any(text in message for message in found)


def test_a_clean_module_has_no_findings() -> None:
    assert messages(module_of(good_test())) == []


def test_if_in_a_test_is_found() -> None:
    found = messages(module_of(good_test(body="    if True:\n        assert 1 == 1\n")))
    assert len([f for f in found if "if —" in f]) == 1


def test_for_in_a_test_is_found() -> None:
    found = messages(module_of(good_test(body="    for n in (1,):\n        assert n == 1\n")))
    assert len([f for f in found if "for —" in f]) == 1


def test_while_in_a_test_is_found() -> None:
    found = messages(module_of(good_test(body="    while False:\n        pass\n    assert 1 == 1\n")))
    assert len([f for f in found if "while —" in f]) == 1


def test_try_in_a_test_is_found() -> None:
    found = messages(module_of(good_test(body="    try:\n        pass\n    finally:\n        pass\n    assert 1 == 1\n")))
    assert len([f for f in found if "try —" in f]) == 1


def test_lambda_in_a_test_is_found() -> None:
    found = messages(module_of(good_test(body="    f = lambda: 1\n    assert f() == 1\n")))
    assert len([f for f in found if "lambda —" in f]) == 1


def test_for_in_a_helper_is_not_found() -> None:
    helper = "def build() -> list[int]:\n    rows = []\n    for n in (1,):\n        rows.append(n)\n    return rows\n"
    assert messages(module_of(helper, good_test())) == []


def test_try_finally_in_a_fixture_is_not_found() -> None:
    fixture = "@pytest.fixture\ndef store() -> int:\n    try:\n        return 1\n    finally:\n        pass\n"
    assert messages(module_of(fixture, good_test(), head="import pytest")) == []


def test_comprehension_in_a_helper_is_not_found() -> None:
    helper = "def build() -> list[int]:\n    return [n for n in (1,)]\n"
    assert messages(module_of(helper, good_test())) == []


def test_sleep_in_a_helper_is_found() -> None:
    helper = "def wait() -> None:\n    time.sleep(1)\n"
    assert mentions(messages(module_of(helper, good_test(), head="import time")), "sleep()")


def test_a_conditional_expression_is_not_control_flow() -> None:
    assert messages(module_of(good_test(body="    n = 1 if True else 2\n    assert n == 1\n"))) == []


def test_async_test_is_checked_like_a_sync_test() -> None:
    source = module_of("async def test_x() -> None:\n    for n in (1,):\n        assert n\n")
    found = messages(source)
    assert mentions(found, "for —")
    assert mentions(found, "name must say the behaviour")


def test_comprehension_outside_an_assert_is_found() -> None:
    assert mentions(messages(module_of(good_test(body="    xs = [n for n in (1,)]\n    assert xs == [1]\n"))), "comprehension outside an assert")


def test_comprehension_inside_an_assert_passes() -> None:
    assert messages(module_of(good_test(body="    assert [n for n in (1,)] == [1]\n"))) == []


def test_assert_in_a_helper_is_found() -> None:
    helper = "def check_it() -> None:\n    assert 1 == 1\n"
    assert mentions(messages(module_of(helper, good_test())), "assert inside check_it()")


def test_assert_in_a_test_is_not_a_helper_assert() -> None:
    assert not mentions(messages(module_of(good_test())), "assert inside")


def test_parametrize_row_of_a_list_is_found() -> None:
    deco = '@pytest.mark.parametrize(("events", "n"), [(["a", "b"], 1)])\n'
    source = module_of(deco + good_test(), head="import pytest")
    assert mentions(messages(source), "row holds `['a', 'b']`")


def test_parametrize_row_of_plain_values_passes() -> None:
    deco = '@pytest.mark.parametrize(("name", "n"), [pytest.param("a", -1, id="x"), ("b", 2)])\n'
    assert messages(module_of(deco + good_test(), head="import pytest")) == []


def test_sleep_is_found() -> None:
    assert mentions(messages(module_of(good_test(body="    time.sleep(1)\n    assert 1 == 1\n"), head="import time")), "sleep()")


def test_test_requesting_a_session_fixture_is_found() -> None:
    fixture = '@pytest.fixture(scope="session")\ndef shared_store() -> int:\n    return 1\n'
    test = good_test().replace("() -> None", "(shared_store: int) -> None")
    assert mentions(messages(module_of(fixture, test, head="import pytest")), "requests `shared_store`")


def test_test_requesting_a_session_fixture_from_conftest_is_found() -> None:
    conftest = 'import pytest\n\n\n@pytest.fixture(scope="session")\ndef browser() -> int:\n    return 1\n'
    test = good_test().replace("() -> None", "(browser: int) -> None")
    assert mentions(messages(module_of(test), conftest=conftest), "requests `browser`")


def test_function_scoped_fixture_behind_a_session_fixture_passes() -> None:
    fixtures = '@pytest.fixture(scope="session")\ndef browser() -> int:\n    return 1\n\n\n@pytest.fixture\ndef tab(browser: int) -> int:\n    return browser\n'
    test = good_test().replace("() -> None", "(tab: int) -> None")
    assert messages(module_of(fixtures, test, head="import pytest")) == []


def test_name_under_three_words_is_found() -> None:
    assert mentions(messages(module_of(good_test("test_borrow_works"))), "name must say the behaviour")


def test_docstring_not_given_when_then_is_found() -> None:
    test = 'def test_a_reader_borrows_a_book() -> None:\n    """Borrow a book."""\n    assert 1 == 1\n'
    assert mentions(messages(module_of(test)), "Given / When / Then")


def test_test_without_a_docstring_is_found() -> None:
    assert mentions(messages(module_of("def test_a_reader_borrows_a_book() -> None:\n    assert 1 == 1\n")), "Given / When / Then")


def test_module_level_test_is_checked_like_a_test_in_a_class() -> None:
    in_class = "class TestRule:\n    def test_short(self) -> None:\n        assert 1 == 1\n"
    at_module = good_test("test_short")
    assert mentions(messages(module_of(in_class)), "test_short: name must say")
    assert mentions(messages(module_of(at_module)), "test_short: name must say")


SPEC_HEAD = '"""Borrowing books.\n\nIn order to keep one copy with one reader, a reader borrows a book.\n\n- One copy per book.\n"""\n'


def rule_class(number: int = 1, doc: str = "A borrowed book is not lent twice.") -> str:
    body = good_test().replace("def test_a", "    def test_a").replace("\n    ", "\n        ").replace("() -> None", "(self) -> None")
    return f'@pytest.mark.rule("AC-{number}")\nclass TestRule{number}:\n    """{doc}"""\n\n{body}'


def spec_module(*classes: str, head: str = SPEC_HEAD) -> str:
    return module_of(*classes, head=head + "import pytest\n")


def test_a_clean_spec_module_has_no_findings() -> None:
    assert messages(spec_module(rule_class()), spec=True) == []


def test_spec_rules_do_not_apply_outside_tests_spec() -> None:
    assert messages(spec_module(rule_class(doc="Two. Sentences.")), spec=False) == []


def test_spec_docstring_without_a_purpose_line_is_found() -> None:
    head = '"""Borrowing books.\n\nA reader borrows a book.\n"""\n'
    assert mentions(messages(spec_module(rule_class(), head=head), spec=True), "no purpose line")


def test_spec_docstring_starting_with_the_purpose_has_no_title() -> None:
    head = '"""In order to keep one copy with one reader, a reader borrows a book."""\n'
    assert mentions(messages(spec_module(rule_class(), head=head), spec=True), "title")


def test_spec_docstring_with_six_bullets_is_found() -> None:
    bullets = "\n".join(f"- point {n}" for n in range(6))
    head = f'"""Borrowing books.\n\nIn order to keep one copy with one reader, a reader borrows a book.\n\n{bullets}\n"""\n'
    assert mentions(messages(spec_module(rule_class(), head=head), spec=True), "6 bullets")


def test_spec_with_eight_rules_is_found() -> None:
    classes = [rule_class(n) for n in range(1, 9)]
    assert mentions(messages(spec_module(*classes), spec=True), "8 rules")


def test_spec_class_without_a_rule_marker_is_found() -> None:
    unmarked = rule_class().replace('@pytest.mark.rule("AC-1")\n', "")
    assert mentions(messages(spec_module(unmarked), spec=True), 'not marked rule("AC-n")')


def test_spec_class_docstring_of_two_sentences_is_found() -> None:
    assert mentions(messages(spec_module(rule_class(doc="One. Two.")), spec=True), "one sentence")


def test_xfail_strict_with_an_issue_passes() -> None:
    marked = '@pytest.mark.xfail(strict=True, reason="bug #567")\n' + good_test()
    assert messages(spec_module(marked), spec=True) == []


def test_xfail_without_strict_is_found() -> None:
    marked = '@pytest.mark.xfail(reason="bug #567")\n' + good_test()
    assert mentions(messages(spec_module(marked), spec=True), "without strict=True")


def test_xfail_without_an_issue_reason_is_found() -> None:
    marked = '@pytest.mark.xfail(strict=True, reason="later")\n' + good_test()
    assert mentions(messages(spec_module(marked), spec=True), "naming its issue")


def test_bare_xfail_is_found_once() -> None:
    marked = "@pytest.mark.xfail\n" + good_test()
    assert len([m for m in messages(spec_module(marked), spec=True) if "xfail" in m]) == 1


def test_main_exits_one_on_a_finding_and_zero_on_none() -> None:
    root = Path(tempfile.mkdtemp())
    bad, clean = root / "test_bad.py", root / "test_clean.py"
    bad.write_text(module_of(good_test("test_short")), encoding="utf-8")
    clean.write_text(module_of(good_test()), encoding="utf-8")
    assert testrules.main([str(bad)]) == 1
    assert testrules.main([str(clean)]) == 0
    shutil.rmtree(root)


def main() -> int:
    tests: list[tuple[str, Callable[[], None]]] = [(n, f) for n, f in globals().items() if n.startswith("test_") and n != "test_module" and callable(f)]
    failed = 0
    for name, test in tests:
        with tempfile.TemporaryDirectory() as directory:
            arguments = {"tmp_path": Path(directory)} if "tmp_path" in inspect.signature(test).parameters else {}
            try:
                test(**arguments)
            except AssertionError as problem:
                failed += 1
                print(f"FAIL {name}: {problem}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
