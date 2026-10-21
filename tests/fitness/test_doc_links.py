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
from functools import cache
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


@cache
def _slugs_by_path() -> dict[Path, set[str]]:
    return {f.resolve(): _file_slugs(f.read_text()) for f in _doc_files()}


@cache
def _internal_links() -> list[tuple[Path, str]]:
    """Every non-external link target with the file that holds it."""
    links: list[tuple[Path, str]] = []
    for f in _doc_files():
        targets = (m.group(1).strip() for m in _LINK.finditer(f.read_text()))
        links.extend((f, target) for target in targets if not _EXTERNAL.match(target))
    return links


def _dangling_anchors() -> list[str]:
    return [
        f"{f.relative_to(ROOT)}: dangling anchor '{target}'"
        for f, target in _internal_links()
        if target.startswith("#") and target[1:] not in _slugs_by_path()[f.resolve()]
    ]


def _relative_path_links() -> list[tuple[Path, str, Path, str]]:
    """Each relative link as (file, target, resolved path, fragment)."""
    found = []
    for f, target in _internal_links():
        path_part, _, fragment = target.partition("#")
        if path_part:
            found.append((f, target, (f.parent / path_part).resolve(), fragment))
    return found


def _missing_path_targets() -> list[str]:
    return [
        f"{f.relative_to(ROOT)}: missing path target '{target}'"
        for f, target, resolved, _ in _relative_path_links()
        if not resolved.exists()
    ]


def _stale_cross_file_fragments() -> list[str]:
    return [
        f"{f.relative_to(ROOT)}: cross-file link '{target}' resolves, but "
        f"'#{fragment}' is not a heading in {resolved.relative_to(ROOT)}"
        for f, target, resolved, fragment in _relative_path_links()
        if fragment and resolved in _slugs_by_path() and fragment not in _slugs_by_path()[resolved]
    ]


def test_same_file_anchor_links_match_a_heading() -> None:
    """Given every ](#slug) link in README.md, CLAUDE.md and docs/**/*.md
    When each slug is looked up among its own file's heading slugs
    Then none is dangling
    """
    problems = _dangling_anchors()

    assert problems == [], "dangling anchors:\n  " + "\n  ".join(problems)


def test_relative_path_links_point_at_existing_paths() -> None:
    """Given every ](relative/path) link in README.md, CLAUDE.md and docs/**/*.md
    When each path is resolved relative to the linking file
    Then none is missing on disk
    """
    problems = _missing_path_targets()

    assert problems == [], "missing path targets:\n  " + "\n  ".join(problems)


def test_cross_file_fragments_match_a_heading_in_the_target() -> None:
    """Given every ](relative/path#fragment) link into one of our markdown files
    When each fragment is looked up among the target file's heading slugs
    Then none points at a moved or renamed section
    """
    problems = _stale_cross_file_fragments()

    assert problems == [], "stale cross-file fragments:\n  " + "\n  ".join(problems)
