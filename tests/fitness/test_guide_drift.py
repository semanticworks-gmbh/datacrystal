"""Fitness gate (#119): the reference never drifts from the public surface.

Two honesty contracts, both CI-enforced. Since the Diátaxis split (#128) the API
reference lives in ``docs/reference.md`` (``docs/GUIDE.md`` is now the thin doc index),
so both contracts read that file:

1. **Every name in core ``datacrystal.__all__`` is documented in docs/reference.md.**
   The honesty bar matches how the reference actually documents each kind of symbol:

   * API symbols (``Store``, ``entity``, ``Snapshot``, ``DeltaConsumer``, …) get a
     **runnable mention** — they appear inside a backticked code reference, either a
     fenced code block or an inline ``code`` span (the reference documents API by showing
     it in use, not by tabulating it).
   * Exception and warning classes get a **row in the error/warning reference table**
     (the ``## Errors`` section). They are not shown "in use"; the reference table is
     where the docs promise they exist.

   Adding a new ``datacrystal.__all__`` name without documenting it in the reference
   turns this gate red — that is the point (see the Definition-of-Done line in CLAUDE.md).

2. **No symbol presented under the reference's "Planned features" section is actually
   exported** — no feature is described as planned while it already ships. (The section
   may *cite* a shipped symbol as a contrast — e.g. "indexed-field renames are planned;
   ``dc.RenamedFrom`` already ships" — so an exported token is only a violation when it
   is NOT qualified as already-shipping on its line.)

**Scope: core ``datacrystal.__all__`` AND ``datacrystal.web.__all__``.** Since #129 the web
extra's public surface is held to the same honesty bar: every name in ``datacrystal.web.__all__``
is documented in the reference's ``datacrystal[web]`` section (a runnable/inline-code mention) —
see ``test_web_surface_documented`` below.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path

import pytest

import datacrystal as dc

REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "reference.md"


def _core_public_names() -> list[str]:
    return [n for n in dc.__all__ if n != "__version__"]


def _classify(name: str) -> str:
    """Return 'warning', 'exception', or 'api' for a core public name."""
    obj = getattr(dc, name)
    if isinstance(obj, type) and issubclass(obj, Warning):
        return "warning"
    if isinstance(obj, type) and issubclass(obj, BaseException):
        return "exception"
    return "api"


def _code_mentions(reference: str) -> str:
    """All backticked code text in the reference: fenced blocks + inline spans.

    An API symbol is "documented" when it appears as code somewhere here — the reference
    documents API by showing it in use, not by listing it in a table.
    """
    fenced = re.findall(r"```.*?\n(.*?)```", reference, re.DOTALL)
    inline = re.findall(r"`([^`\n]+)`", reference)
    return "\n".join(fenced) + "\n" + "\n".join(inline)


def _errors_section(reference: str) -> str:
    """Return the ## Errors reference region (up to the next H2); exceptions and warnings live here."""
    m = re.search(r"\n## Errors\b(.*?)(?:\n## |\Z)", reference, re.DOTALL)
    return m.group(1) if m else ""


def _planned_section(reference: str) -> str:
    """Return the ## Planned features region (up to the next H2); empty when the section is missing."""
    m = re.search(r"\n## Planned features\b(.*?)(?:\n## |\Z)", reference, re.DOTALL)
    return m.group(1) if m else ""


def _mentioned(text: str, name: str) -> bool:
    return re.search(r"\b" + re.escape(name) + r"\b", text) is not None


def _exported_names_listed_as_planned(planned: str) -> list[str]:
    exported = set(_core_public_names())
    offenders: list[str] = []
    for line in planned.splitlines():
        # A line that explicitly qualifies a symbol as already shipping is honest, not drift.
        if "already ship" in line:
            continue
        offenders.extend(t for t in re.findall(r"`dc\.(\w+)", line) if t in exported)
        offenders.extend(t for t in re.findall(r"`(\w+)`", line) if t in exported)
    return sorted(set(offenders))


def _web_public_names() -> list[str]:
    if importlib.util.find_spec("strawberry") is None:
        return []
    web = importlib.import_module("datacrystal.web")
    return [n for n in getattr(web, "__all__", ()) if n != "__version__"]


API_NAMES = [n for n in _core_public_names() if _classify(n) == "api"]
ERROR_NAMES = [n for n in _core_public_names() if _classify(n) != "api"]


@pytest.fixture
def reference() -> str:
    """Return the text of docs/reference.md."""
    return REFERENCE.read_text()


def test_reference_has_an_errors_section(reference: str) -> None:
    """Given docs/reference.md
    When its ## Errors section is looked up
    Then the section exists and is not empty
    """
    assert _errors_section(reference).strip() != ""


def test_reference_has_a_planned_features_section(reference: str) -> None:
    """Given docs/reference.md
    When its ## Planned features section is looked up
    Then the section exists and is not empty
    """
    assert _planned_section(reference).strip() != ""


@pytest.mark.parametrize("name", API_NAMES)
def test_core_api_symbol_has_a_code_mention(name: str, reference: str) -> None:
    """Given an API symbol in datacrystal.__all__
    When the reference's fenced blocks and inline code are searched for it
    Then the symbol is mentioned as code
    """
    assert _mentioned(_code_mentions(reference), name), (
        f"{name} is in datacrystal.__all__ but has no runnable/inline-code mention "
        "in docs/reference.md"
    )


@pytest.mark.parametrize("name", ERROR_NAMES)
def test_core_exception_has_a_row_in_the_errors_table(name: str, reference: str) -> None:
    """Given an exception or warning class in datacrystal.__all__
    When the reference's ## Errors section is searched for it
    Then the class is listed there
    """
    assert _mentioned(_errors_section(reference), name), (
        f"{name} is in datacrystal.__all__ but has no row in the ## Errors table "
        "of docs/reference.md"
    )


def test_no_shipped_feature_is_listed_as_planned(reference: str) -> None:
    """Given the reference's ## Planned features section
    When its backticked names are compared with datacrystal.__all__
    Then no exported symbol is presented as not yet shipped
    """
    planned = _planned_section(reference)

    offenders = _exported_names_listed_as_planned(planned)

    assert offenders == [], (
        "exported datacrystal.__all__ symbols are presented under '## Planned features' "
        f"as if not yet shipped (shipped-under-Planned): {offenders}"
    )


# #129: the drift-guard covers datacrystal.web.__all__ too (the REST/GraphQL reflection surface).
# Its symbols are all API, so a code mention is the whole contract; with no web extra installed
# the parameter set is empty and pytest skips the test.
@pytest.mark.parametrize("name", _web_public_names())
def test_web_symbol_has_a_code_mention(name: str, reference: str) -> None:
    """Given a symbol in datacrystal.web.__all__
    When the reference's fenced blocks and inline code are searched for it
    Then the symbol is mentioned as code
    """
    assert _mentioned(_code_mentions(reference), name), (
        f"{name} is in datacrystal.web.__all__ but has no code mention in docs/reference.md "
        "(document it in the datacrystal[web] section, #129)"
    )
