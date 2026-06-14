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

import msgspec.msgpack

_CACHE_FORMAT = 1


class IndexCache:
    """Read/write the per-typename index blobs in one sidecar file."""

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self, watermark: int) -> dict[str, Any] | None:
        """The ``{typename: index-blob}`` map IF the cache is at ``watermark`` and
        the current format — else ``None`` (rebuild). Never raises on a missing,
        truncated, or foreign-format cache: it is never authoritative."""
        try:
            raw = self._path.read_bytes()
        except OSError:
            return None
        try:
            decoded = msgspec.msgpack.decode(raw)
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
        return cast("dict[str, Any]", classes)

    def write(self, watermark: int, blobs: dict[str, Any]) -> None:
        """Stamp the built index blobs at ``watermark`` (temp file + atomic
        rename, so a crash mid-write leaves the prior valid cache or none)."""
        payload = msgspec.msgpack.encode(
            {"format": _CACHE_FORMAT, "watermark": watermark, "classes": blobs}
        )
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(self._path)
