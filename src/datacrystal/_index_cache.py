"""Index cache sidecar (ADR-005 / #12): persist the built in-memory indexes to a
watermark-stamped file beside the store so the first query after a restart loads
them instead of rebuilding from an O(corpus) scan.

The cache is rebuildable derived data and **never the source of truth** (invariant
11, amended by ADR-005): it is read only when its stamped watermark matches the
store's, its format matches, and — per class — the live class's index markers
still match (``ClassIndexes.load`` enforces the last). Any mismatch, corruption,
or a newer format is silently ignored and the index rebuilds from the records.
Written outside the commit transaction; a partial write never lands (temp file +
atomic rename).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import msgspec

from datacrystal._records import decode_scalar_tree, encode_scalar_tree

# Bumped when admitting datetime/date keys: the codec now routes temporal leaves
# through the record ext codes (#106) rather than msgspec's default datetime
# handling, which lossily encoded a naive datetime as a bare str. A pre-#106
# sidecar (format 1) is silently ignored and the index rebuilds — never
# authoritative (invariant 11), so a format bump is just a one-time rebuild.
_CACHE_FORMAT = 2


class IndexCache:
    """Read/write the per-typename index blobs in one sidecar file."""

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self, watermark: int) -> dict[str, Any] | None:
        """``{"classes": {typename: blob}, "reverse": blob|None}`` IF the cache is
        at ``watermark`` and the current format — else ``None`` (rebuild). Never
        raises on a missing, truncated, or foreign-format cache: it is never
        authoritative. ``reverse`` is optional (#63) — a pre-#63 sidecar simply
        lacks it, so the reverse index rebuilds on demand as before."""
        try:
            raw = self._path.read_bytes()
        except OSError:
            return None
        try:
            decoded = decode_scalar_tree(raw)
        except (msgspec.DecodeError, ValueError, EOFError):
            return None
        if not isinstance(decoded, dict):
            return None
        doc = cast("dict[str, Any]", decoded)
        if doc.get("format") != _CACHE_FORMAT or doc.get("watermark") != watermark:
            return None
        classes = doc.get("classes")
        if not isinstance(classes, dict):
            return None
        return {"classes": cast("dict[str, Any]", classes), "reverse": doc.get("reverse")}

    def write(self, watermark: int, blobs: dict[str, Any],
              reverse: dict[str, Any] | None = None) -> None:
        """Stamp the built forward index blobs (and the reverse index, if built —
        #63) at ``watermark`` (temp file + atomic rename, so a crash mid-write
        leaves the prior valid cache or none)."""
        payload = encode_scalar_tree(
            {"format": _CACHE_FORMAT, "watermark": watermark,
             "classes": blobs, "reverse": reverse}
        )
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(self._path)
