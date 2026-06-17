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
    """The ## Errors reference region (up to the next H2). Exceptions/warnings live here."""
    m = re.search(r"\n## Errors\b(.*?)(?:\n## |\Z)", reference, re.DOTALL)
    assert m, "docs/reference.md must have a '## Errors' reference section"
    return m.group(1)


def _planned_section(reference: str) -> str:
    """The ## Planned features region (up to the next H2)."""
    m = re.search(r"\n## Planned features\b(.*?)(?:\n## |\Z)", reference, re.DOTALL)
    assert m, "docs/reference.md must have a '## Planned features' section"
    return m.group(1)


def _mentioned(text: str, name: str) -> bool:
    return re.search(r"\b" + re.escape(name) + r"\b", text) is not None


def test_core_all_is_documented():
    reference = REFERENCE.read_text()
    code = _code_mentions(reference)
    errors = _errors_section(reference)

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
        "core datacrystal.__all__ names missing from docs/reference.md "
        f"(API symbols need a runnable/inline-code mention; exceptions & warnings need a "
        f"row in the ## Errors reference table): {undocumented}"
    )


def test_no_shipped_feature_listed_as_planned():
    reference = REFERENCE.read_text()
    planned = _planned_section(reference)
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


# --- #129: the documentation drift-guard now covers datacrystal.web ----------------
# Mirror the core-__all__ API check over datacrystal.web.__all__: every exported web symbol
# (the REST/GraphQL reflection surface — entity_model, reflect, reflect_strawberry_type,
# StrawberryReflector, FieldDescriptor, snapshot_context, the context-key constants, …) must
# appear as code somewhere in the reference. Adding a new datacrystal.web.__all__ name without
# documenting it turns this gate red — the same Definition-of-Done discipline as core (#129
# closed the original 8-symbol gap; this assertion is the standing guard). The web extra's
# symbols are all API (no exception/warning classes), so the runnable-mention check is the whole
# contract. importorskip so the bare suite (no web extra) stays green.
def test_web_surface_documented():
    pytest.importorskip("strawberry", reason="datacrystal[web] extra not installed")
    web = importlib.import_module("datacrystal.web")
    reference = REFERENCE.read_text()
    code = _code_mentions(reference)
    web_names = [n for n in getattr(web, "__all__", ()) if n != "__version__"]
    undocumented = [n for n in web_names if not _mentioned(code, n)]
    assert not undocumented, (
        "datacrystal.web.__all__ names missing a runnable/inline-code mention in docs/reference.md "
        f"(document them in the datacrystal[web] section, #129): {undocumented}"
    )
