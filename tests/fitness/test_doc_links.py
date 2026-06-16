"""Fitness gate (#119): internal doc links resolve — no dangling anchors or paths.

Over README.md, CLAUDE.md, and docs/**/*.md:

* every ``](#slug)`` same-file anchor link must match a heading in that file, where the
  heading is reduced to its GitHub slug (lowercase, drop punctuation, spaces -> hyphens,
  GitHub's ``-1``/``-2`` de-duplication of repeated headings);
* every ``](relative/path)`` link (optionally with a ``#fragment``) must point at a path
  that exists on disk, resolved relative to the linking file.

This catches the dangling-anchor class that slipped before (the old ``#querying``, fixed in
#131) and broken relative paths. External (``scheme://`` / ``mailto:``) links are out of
scope — they need the network and are not a documentation-honesty contract.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# A markdown link: [text](target). text may contain nested brackets in our docs only rarely;
# the target runs to the closing paren. Good enough for our prose (no parens inside targets).
_LINK = re.compile(r"\[(?:[^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*://|^mailto:", re.IGNORECASE)


def _doc_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "CLAUDE.md"]
    files += sorted(ROOT.glob("docs/**/*.md"))
    return [f for f in files if f.exists()]


def _github_slug(heading: str) -> str:
    """GitHub's anchor-slug algorithm for a heading's display text.

    Lowercase; strip inline markdown markers (backticks, asterisks, underscores); drop all
    characters that are not alphanumerics, spaces, or hyphens; then spaces -> hyphens.
    Unicode letters/digits are kept (``str.isalnum`` is unicode-aware), matching GitHub.
    """
    s = heading.strip().lower()
    s = s.replace("`", "").replace("*", "").replace("_", "")
    kept = [ch for ch in s if ch.isalnum() or ch in " -"]
    return "".join(kept).replace(" ", "-")


def _file_slugs(text: str) -> set[str]:
    """Every anchor slug a GitHub render of this file would expose (with -N de-duplication)."""
    seen: dict[str, int] = {}
    slugs: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        base = _github_slug(m.group(2))
        if base in seen:
            seen[base] += 1
            slugs.add(f"{base}-{seen[base]}")
        else:
            seen[base] = 0
            slugs.add(base)
    return slugs


def test_internal_doc_links_resolve():
    files = _doc_files()
    slug_cache = {f: _file_slugs(f.read_text()) for f in files}

    problems: list[str] = []
    for f in files:
        text = f.read_text()
        own_slugs = slug_cache[f]
        for m in _LINK.finditer(text):
            target = m.group(1).strip()
            if _EXTERNAL.match(target):
                continue
            if target.startswith("#"):
                slug = target[1:]
                if slug not in own_slugs:
                    problems.append(f"{f.relative_to(ROOT)}: dangling anchor '{target}'")
                continue
            # relative path, optionally with its own #fragment
            path_part, _, _frag = target.partition("#")
            if path_part == "":  # pure fragment handled above; empty means nothing to check
                continue
            resolved = (f.parent / path_part).resolve()
            if not resolved.exists():
                problems.append(f"{f.relative_to(ROOT)}: missing path target '{target}'")

    assert not problems, "internal documentation links do not resolve:\n  " + "\n  ".join(problems)
