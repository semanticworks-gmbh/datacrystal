"""Fitness gate (#119): the GUIDE never drifts from the core public surface.

Two honesty contracts, both CI-enforced:

1. **Every name in core ``datacrystal.__all__`` is documented in docs/GUIDE.md.**
   The honesty bar matches how the GUIDE actually documents each kind of symbol:

   * API symbols (``Store``, ``entity``, ``Snapshot``, ``DeltaConsumer``, …) get a
     **runnable mention** — they appear inside a backticked code reference, either a
     fenced code block or an inline ``code`` span (the GUIDE documents API by showing
     it in use, not by tabulating it).
   * Exception and warning classes get a **row in the error/warning reference table**
     (the ``## Errors`` section). They are not shown "in use"; the reference table is
     where the guide promises they exist.

   The DOCS audit confirmed core ``__all__`` is fully documented, so this PASSES today.
   Adding a new ``datacrystal.__all__`` name without documenting it in the GUIDE turns
   this gate red — that is the point (see the Definition-of-Done line in CLAUDE.md).

2. **No symbol presented under the GUIDE's "Planned features" section is actually
   exported** — no feature is described as planned while it already ships. (The section
   may *cite* a shipped symbol as a contrast — e.g. "indexed-field renames are planned;
   ``dc.RenamedFrom`` already ships" — so an exported token is only a violation when it
   is NOT qualified as already-shipping on its line.)

**Scope: core ``datacrystal.__all__`` ONLY.** ``datacrystal.web.__all__`` is deliberately
excluded for now: 8 of its symbols are knowingly undocumented, tracked in #129. Extending
this guard to the web surface is gated on #129 closing — see ``test_web_surface_documented``
below (xfail placeholder).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

import datacrystal as dc

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "GUIDE.md"


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


def _code_mentions(guide: str) -> str:
    """All backticked code text in the guide: fenced blocks + inline spans.

    An API symbol is "documented" when it appears as code somewhere here — the guide
    documents API by showing it in use, not by listing it in a table.
    """
    fenced = re.findall(r"```.*?\n(.*?)```", guide, re.DOTALL)
    inline = re.findall(r"`([^`\n]+)`", guide)
    return "\n".join(fenced) + "\n" + "\n".join(inline)


def _errors_section(guide: str) -> str:
    """The ## Errors reference region (up to the next H2). Exceptions/warnings live here."""
    m = re.search(r"\n## Errors\b(.*?)(?:\n## |\Z)", guide, re.DOTALL)
    assert m, "docs/GUIDE.md must have a '## Errors' reference section"
    return m.group(1)


def _planned_section(guide: str) -> str:
    """The ## Planned features region (to end of file)."""
    m = re.search(r"\n## Planned features\b(.*)\Z", guide, re.DOTALL)
    assert m, "docs/GUIDE.md must have a '## Planned features' section"
    return m.group(1)


def _mentioned(text: str, name: str) -> bool:
    return re.search(r"\b" + re.escape(name) + r"\b", text) is not None


def test_core_all_is_documented():
    guide = GUIDE.read_text()
    code = _code_mentions(guide)
    errors = _errors_section(guide)

    undocumented: list[str] = []
    for name in _core_public_names():
        kind = _classify(name)
        if kind == "api":
            ok = _mentioned(code, name)
        else:  # exception or warning -> must be in the error/warning reference table
            ok = _mentioned(errors, name)
        if not ok:
            undocumented.append(f"{name} ({kind})")

    assert not undocumented, (
        "core datacrystal.__all__ names missing from docs/GUIDE.md "
        f"(API symbols need a runnable/inline-code mention; exceptions & warnings need a "
        f"row in the ## Errors reference table): {undocumented}"
    )


def test_no_shipped_feature_listed_as_planned():
    guide = GUIDE.read_text()
    planned = _planned_section(guide)
    exported = {n for n in _core_public_names()}

    offenders: list[str] = []
    for line in planned.splitlines():
        # A line that explicitly qualifies a symbol as already shipping is honest, not drift.
        if "already ship" in line:
            continue
        for token in re.findall(r"`dc\.(\w+)", line):
            if token in exported:
                offenders.append(token)
        # also catch a bare backticked exported name presented as planned
        for token in re.findall(r"`(\w+)`", line):
            if token in exported:
                offenders.append(token)

    assert not offenders, (
        "exported datacrystal.__all__ symbols are presented under '## Planned features' "
        f"as if not yet shipped (shipped-under-Planned): {sorted(set(offenders))}"
    )


# --- #129: extending the documentation drift-guard to datacrystal.web -------------
# datacrystal.web exports 8 symbols that are knowingly undocumented in the GUIDE today.
# This guard is intentionally NOT enforced against the web surface until #129 lands the
# missing web docs. Kept as an xfail so the day #129 closes, this turns green and we can
# flip it to a hard gate (drop the xfail). Do NOT delete — it is the tracking signal.
@pytest.mark.xfail(reason="#129: datacrystal.web public surface not yet fully documented", strict=False)
def test_web_surface_documented():
    web = importlib.import_module("datacrystal.web")
    guide = GUIDE.read_text()
    code = _code_mentions(guide)
    web_names = [n for n in getattr(web, "__all__", ()) if n != "__version__"]
    undocumented = [n for n in web_names if not _mentioned(code, n)]
    assert not undocumented, f"datacrystal.web names undocumented in GUIDE (#129): {undocumented}"
