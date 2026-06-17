"""Fitness gate (#119): internal doc links resolve — no dangling anchors or paths.

Over README.md, CLAUDE.md, and docs/**/*.md:

* every ``](#slug)`` same-file anchor link must match a heading in that file, where the
  heading is reduced to its GitHub slug (lowercase, drop punctuation, spaces -> hyphens,
  GitHub's ``-1``/``-2`` de-duplication of repeated headings);
* every ``](relative/path)`` link (optionally with a ``#fragment``) must point at a path
  that exists on disk, resolved relative to the linking file;
* and, since the Diátaxis split (#128) made cross-file fragments pervasive, every
  ``](relative/path#fragment)`` link whose target is one of our in-scope markdown files must
  additionally have the ``#fragment`` match a heading slug **in that target file** — not just
  resolve the path. A cross-file link to a moved or renamed section is exactly as broken as a
  dangling same-file anchor, so it is guarded the same way.

This catches the dangling-anchor class that slipped before (the old ``#querying``, fixed in
#131), broken relative paths, and cross-file fragments pointing at a section that no longer
exists under that slug. External (``scheme://`` / ``mailto:``) links are out of scope — they
need the network and are not a documentation-honesty contract; a fragment into a non-markdown
target (or a markdown file outside our scope, e.g. a directory link) is left to the path check.
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
    # Slugs cached by RESOLVED path so a cross-file link's #fragment can be checked against the
    # heading slugs of the file it actually points at (not just the linking file's own slugs).
    slug_cache = {f: _file_slugs(f.read_text()) for f in files}
    slug_by_resolved = {f.resolve(): slugs for f, slugs in slug_cache.items()}

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
            path_part, _, frag = target.partition("#")
            if path_part == "":  # pure fragment handled above; empty means nothing to check
                continue
            resolved = (f.parent / path_part).resolve()
            if not resolved.exists():
                problems.append(f"{f.relative_to(ROOT)}: missing path target '{target}'")
                continue
            # Cross-file fragment: if the target is an in-scope markdown file, the #fragment
            # must match a heading slug in THAT file (a moved/renamed section is a broken link).
            if frag and resolved in slug_by_resolved:
                if frag not in slug_by_resolved[resolved]:
                    problems.append(
                        f"{f.relative_to(ROOT)}: cross-file link '{target}' resolves, but "
                        f"'#{frag}' is not a heading in {resolved.relative_to(ROOT)}"
                    )

    assert not problems, "internal documentation links do not resolve:\n  " + "\n  ".join(problems)
