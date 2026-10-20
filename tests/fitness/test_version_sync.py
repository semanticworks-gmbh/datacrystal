"""Fitness gate: the version has ONE source, and every mirror agrees (#176).

pyproject.toml is authoritative. The mirrors — ``__version__``, the README
badge, every ``@vX.Y.Z`` install pin, the README status line + additive
range, and the CLAUDE.md header version — must all carry the same number.
``scripts/bump_version.py`` rewrites them at release time; this gate makes
drift fail CI no matter how it happened (it happened three times before
this gate existed: ``__version__`` at v0.1.0, CLAUDE.md prose at v0.7.0,
README at v0.8.0).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

import datacrystal

ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "bump_version", ROOT / "scripts" / "bump_version.py"
)
assert _spec is not None
assert _spec.loader is not None
bump_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump_version)


@pytest.fixture
def pyproject_version() -> str:
    """Return the version pyproject.toml declares, the one authoritative source."""
    return bump_version.read_version(ROOT)


@pytest.fixture
def readme() -> str:
    """Return the repository README text."""
    return (ROOT / "README.md").read_text()


@pytest.fixture
def fabricated_tree(tmp_path: Path) -> Path:
    """Return a tree in the repo's shape whose every mirror carries 1.2.3."""
    (tmp_path / "src/datacrystal").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('name = "x"\nversion = "1.2.3"\n')
    (tmp_path / "src/datacrystal/__init__.py").write_text('__version__ = "1.2.3"\n')
    (tmp_path / "README.md").write_text(
        "[![version](https://img.shields.io/badge/version-1.2.3-blue)](CHANGELOG.md)\n"
        '"datacrystal @ git+https://github.com/x/datacrystal@v1.2.3"\n'
        '"datacrystal @ git+https://github.com/x/datacrystal@main"\n'
        '"datacrystal[web] @ git+https://github.com/x/datacrystal@v1.2.3"\n'
        "Status: **1.2.3** — frozen; v0.2–1.2 are purely additive.\n"
    )
    (tmp_path / "CLAUDE.md").write_text("blah. Version\n`1.2.3` — v0.1.0 was the baseline\n")
    return tmp_path


def test_package_version_matches_pyproject(pyproject_version: str) -> None:
    """Given the version in pyproject.toml
    When the package's __version__ is read
    Then it is that version
    """
    assert datacrystal.__version__ == pyproject_version


def test_readme_badge_matches_pyproject(pyproject_version: str, readme: str) -> None:
    """Given the version in pyproject.toml
    When the README badge is read
    Then it shows that version
    """
    badge = re.search(r"badge/version-(\d+\.\d+\.\d+)-blue", readme)

    assert badge is not None
    assert badge.group(1) == pyproject_version


def test_readme_install_pins_match_pyproject(pyproject_version: str, readme: str) -> None:
    """Given the version in pyproject.toml
    When the README's @vX.Y.Z install pins are read
    Then every pin is that version
    """
    pins = re.findall(r"@v(\d+\.\d+\.\d+)", readme)

    assert pins != [], "README lost its @vX.Y.Z install pins — update the gate's expectations"
    assert set(pins) == {pyproject_version}


def test_readme_status_line_matches_pyproject(pyproject_version: str, readme: str) -> None:
    """Given the version in pyproject.toml
    When the README status line is read
    Then it shows that version
    """
    status = re.search(r"Status: \*\*(\d+\.\d+\.\d+)\*\*", readme)

    assert status is not None
    assert status.group(1) == pyproject_version


def test_readme_additive_range_ends_at_pyproject_minor(pyproject_version: str, readme: str) -> None:
    """Given the version in pyproject.toml
    When the README's additive range is read
    Then it ends at that version's major.minor
    """
    additive = re.search(r"v0\.2–(\d+\.\d+) are purely additive", readme)

    assert additive is not None
    assert additive.group(1) == ".".join(pyproject_version.split(".")[:2])


def test_claude_md_header_matches_pyproject(pyproject_version: str) -> None:
    """Given the version in pyproject.toml
    When the CLAUDE.md header version is read
    Then it is that version
    """
    header = re.search(r"Version\s+`(\d+\.\d+\.\d+)`", (ROOT / "CLAUDE.md").read_text())

    assert header is not None
    assert header.group(1) == pyproject_version


@pytest.mark.parametrize(
    ("part", "expected"), [("minor", "1.3.0"), ("patch", "1.2.4"), ("major", "2.0.0")]
)
def test_next_version_bumps_the_named_part(part: str, expected: str) -> None:
    """Given the version 1.2.3
    When the next version is asked for a part
    Then that part is bumped and the lower parts reset
    """
    assert bump_version.next_version("1.2.3", part) == expected


def test_bump_rewrites_the_pyproject_version(fabricated_tree: Path) -> None:
    """Given a tree whose mirrors carry 1.2.3
    When every mirror is rewritten to 1.3.0
    Then pyproject.toml reads 1.3.0
    """
    bump_version.rewrite_all(fabricated_tree, "1.3.0")

    assert bump_version.read_version(fabricated_tree) == "1.3.0"


def test_bump_rewrites_the_package_version(fabricated_tree: Path) -> None:
    """Given a tree whose mirrors carry 1.2.3
    When every mirror is rewritten to 1.3.0
    Then the package's __version__ reads 1.3.0
    """
    bump_version.rewrite_all(fabricated_tree, "1.3.0")

    init = (fabricated_tree / "src/datacrystal/__init__.py").read_text()
    assert '__version__ = "1.3.0"' in init


def test_bump_rewrites_the_readme_badge(fabricated_tree: Path) -> None:
    """Given a tree whose mirrors carry 1.2.3
    When every mirror is rewritten to 1.3.0
    Then the README badge reads 1.3.0
    """
    bump_version.rewrite_all(fabricated_tree, "1.3.0")

    assert "version-1.3.0-blue" in (fabricated_tree / "README.md").read_text()


def test_bump_rewrites_both_version_install_pins(fabricated_tree: Path) -> None:
    """Given a tree whose README has two @v1.2.3 install pins
    When every mirror is rewritten to 1.3.0
    Then both pins read @v1.3.0
    """
    bump_version.rewrite_all(fabricated_tree, "1.3.0")

    readme = (fabricated_tree / "README.md").read_text()
    assert readme.count("@v1.3.0") == 2
    assert "@v1.2.3" not in readme


def test_bump_leaves_the_main_install_pin_untouched(fabricated_tree: Path) -> None:
    """Given a tree whose README has an @main install pin
    When every mirror is rewritten to 1.3.0
    Then the @main pin is still there
    """
    bump_version.rewrite_all(fabricated_tree, "1.3.0")

    assert "@main" in (fabricated_tree / "README.md").read_text()


def test_bump_rewrites_the_readme_status_line(fabricated_tree: Path) -> None:
    """Given a tree whose mirrors carry 1.2.3
    When every mirror is rewritten to 1.3.0
    Then the README status line reads 1.3.0
    """
    bump_version.rewrite_all(fabricated_tree, "1.3.0")

    assert "Status: **1.3.0**" in (fabricated_tree / "README.md").read_text()


def test_bump_rewrites_the_additive_range(fabricated_tree: Path) -> None:
    """Given a tree whose mirrors carry 1.2.3
    When every mirror is rewritten to 1.3.0
    Then the README's additive range ends at 1.3
    """
    bump_version.rewrite_all(fabricated_tree, "1.3.0")

    assert "v0.2–1.3 are purely additive" in (fabricated_tree / "README.md").read_text()


def test_bump_rewrites_the_claude_md_header(fabricated_tree: Path) -> None:
    """Given a tree whose mirrors carry 1.2.3
    When every mirror is rewritten to 1.3.0
    Then the CLAUDE.md header version reads 1.3.0
    """
    bump_version.rewrite_all(fabricated_tree, "1.3.0")

    assert "`1.3.0`" in (fabricated_tree / "CLAUDE.md").read_text()
