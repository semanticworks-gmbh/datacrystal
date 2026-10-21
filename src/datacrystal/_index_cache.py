"""Index cache sidecar (ADR-005 / #12): persist the built in-memory indexes to a
watermark-stamped file beside the store so the first query after a restart loads
them instead of rebuilding from an O(corpus) scan.

The cache is rebuildable derived data and **never the source of truth** (invariant 11, ADR-005):
a watermark, format or class-marker mismatch, or corruption, is ignored and the index rebuilds.
It is written outside the commit transaction, by temp file and rename.
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
        """Return ``{"classes": {typename: blob}, "reverse": blob|None}`` if the cache is current.

        Return ``None`` (rebuild) when the cache is at another watermark or format, and never
        raise on a missing, truncated or foreign cache.
        ``reverse`` is optional (#63): a pre-#63 sidecar lacks it and the reverse index rebuilds.
        """
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
        """Stamp the built forward blobs (and the reverse index, if built, #63) at ``watermark``.

        A temp file plus atomic rename leaves the prior valid cache, or none, after a crash.
        """
        payload = encode_scalar_tree(
            {"format": _CACHE_FORMAT, "watermark": watermark,
             "classes": blobs, "reverse": reverse}
        )
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(self._path)
