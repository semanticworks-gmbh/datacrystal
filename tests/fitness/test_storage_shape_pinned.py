"""Fitness (Sprint 13 / #157, gate C13): the durable storage shape is pinned.

The fractal-followers federation facade promises **no new persistence**: the OCC
base token is computed in transit (never stored), idempotency rides the existing
natural-key ``upsert`` (no consumed-key ledger), and the wire adds no
storage-protocol method. This test pins the durable shape so any growth goes RED
until an ADR under ``docs/design/`` accounts for it — the same ADR-gated culture
that governs every other storage-protocol change (ADR-002).

Change one of these on purpose? Update the pin **and** add/cite the ADR in the
same PR. That is the signal, not a nuisance.
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import re
from typing import Any

from datacrystal._storage.protocol import (
    CommitBatch,
    StorageBackend,
    StorageReadView,
    StoredRecord,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
_SQLITE_SRC = (ROOT / "src" / "datacrystal" / "_storage" / "sqlite.py").read_text()


def _field_names(dc: Any) -> tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(dc))


def _protocol_methods(proto: type) -> set[str]:
    return {n for n, v in vars(proto).items() if callable(v) and not n.startswith("__")}


def test_stored_record_shape_pinned() -> None:
    # A new field here would be a new per-record persisted column (e.g. an OCC
    # version or an idempotency key) — exactly what the v1 facade must NOT add.
    assert _field_names(StoredRecord) == ("oid", "cid", "tid", "payload")


def test_commit_batch_channels_pinned() -> None:
    # The set of things one commit persists. A new channel (e.g. a dedup
    # key-ledger) would add a field here.
    assert _field_names(CommitBatch) == (
        "tid",
        "records",
        "new_types",
        "meta",
        "deletes",
        "blobs",
        "blob_streams",
    )


def test_storage_protocol_methods_pinned() -> None:
    # Storage-protocol growth needs an ADR (ADR-002 precedent). Federation is a
    # facade above this seam and adds nothing here.
    assert _protocol_methods(StorageBackend) == {
        "boot",
        "load_many",
        "scan_type",
        "load_blob",
        "apply",
        "read_view",
        "close",
    }
    assert _protocol_methods(StorageReadView) == {
        "boot",
        "load_many",
        "scan_type",
        "load_blob",
        "open_blob_stream",
        "close",
    }


def test_sqlite_table_set_pinned() -> None:
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", _SQLITE_SRC))
    assert tables == {"meta", "types", "objects", "blobs"}, (
        f"SQLite table set changed: {tables}. A new durable table (e.g. a "
        "federation idempotency-key ledger) is a §5-cut — it needs an ADR; v1 "
        "federation adds none."
    )


def test_sqlite_schema_ddl_pinned() -> None:
    # Catches column-level changes a new-table check would miss (e.g. sneaking an
    # idem_key column onto `objects`). Whitespace-normalized so reflow alone
    # doesn't trip it.
    match = re.search(r'_SCHEMA = """(.*?)"""', _SQLITE_SRC, re.S)
    assert match is not None, "could not locate _SCHEMA in sqlite.py"
    normalized = " ".join(match.group(1).split())
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    pinned = "7ecf6749251f5b40818dfc278995fec1b799a2ff29ff92cf3ce24a83a9641c9d"
    assert digest == pinned, (
        f"SQLite DDL changed (now {digest}). If this is a real schema change, "
        "update the pin and add/cite the ADR in the same PR."
    )
