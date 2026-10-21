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

import ast
import dataclasses
import hashlib
import pathlib
import re

from datacrystal._storage.protocol import (
    CommitBatch,
    StorageBackend,
    StorageReadView,
    StoredRecord,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = ROOT / "src" / "datacrystal"
_SQLITE_SRC = (_SRC / "_storage" / "sqlite.py").read_text()
_FED_SRC = (_SRC / "web" / "_federation.py").read_text()
_FOLLOWER_SRC = (_SRC / "_follower.py").read_text()


def _defines_delta_consumer(src: str) -> bool:
    """Say whether the module names ``DeltaConsumer`` or defines a class shaped like one.

    Shaped like one means an ``apply`` method plus a ``watermark`` member (v1 §5 "no second
    consumer").
    """
    if "DeltaConsumer" in src:
        return True
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef):
            methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
            has_watermark = "watermark" in methods or any(
                isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
                and n.target.id == "watermark"
                for n in node.body
            )
            if "apply" in methods and has_watermark:
                return True
    return False


def _field_names(dc: type) -> tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(dc))


def _protocol_methods(proto: type) -> set[str]:
    return {n for n, v in vars(proto).items() if callable(v) and not n.startswith("__")}


def test_stored_record_shape_pinned() -> None:
    """Given the StoredRecord dataclass
    When its field names are listed
    Then they are exactly oid, cid, tid and payload

    A new field would be a new per-record persisted column (e.g. an OCC version or an
    idempotency key), exactly what the v1 facade must NOT add.
    """
    assert _field_names(StoredRecord) == ("oid", "cid", "tid", "payload")


def test_commit_batch_channels_pinned() -> None:
    """Given the CommitBatch dataclass
    When its field names are listed
    Then they are exactly the channels one commit persists

    A new channel (e.g. a dedup key-ledger) would add a field here.
    """
    assert _field_names(CommitBatch) == (
        "tid",
        "records",
        "new_types",
        "meta",
        "deletes",
        "blobs",
        "blob_streams",
    )


def test_storage_backend_methods_pinned() -> None:
    """Given the StorageBackend protocol
    When its methods are listed
    Then they are exactly the seven the engine writes through

    Storage-protocol growth needs an ADR (ADR-002 precedent). Federation is a facade above
    this seam and adds nothing here.
    """
    assert _protocol_methods(StorageBackend) == {
        "boot",
        "load_many",
        "scan_type",
        "load_blob",
        "apply",
        "read_view",
        "close",
    }


def test_storage_read_view_methods_pinned() -> None:
    """Given the StorageReadView protocol
    When its methods are listed
    Then they are exactly the six a snapshot reads through
    """
    assert _protocol_methods(StorageReadView) == {
        "boot",
        "load_many",
        "scan_type",
        "load_blob",
        "open_blob_stream",
        "close",
    }


def test_federation_module_defines_no_delta_consumer() -> None:
    """Given the federation facade web/_federation.py
    When it is searched for a DeltaConsumer or a class shaped like one
    Then there is none

    FEDERATION-WIRE-v1 "Idempotency" (#157): exactly-once rides the natural-key upsert + OCC,
    so no second DeltaConsumer or dedup ledger ships in v1 (§5 cut).
    """
    assert not _defines_delta_consumer(_FED_SRC), "web/_federation.py adds a DeltaConsumer"


def test_follower_module_defines_no_delta_consumer() -> None:
    """Given the follower client _follower.py
    When it is searched for a DeltaConsumer or a class shaped like one
    Then there is none
    """
    assert not _defines_delta_consumer(_FOLLOWER_SRC), "_follower.py adds a DeltaConsumer"


def test_sqlite_table_set_pinned() -> None:
    """Given the SQLite backend's source
    When its CREATE TABLE statements are collected
    Then the tables are exactly meta, types, objects and blobs
    """
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", _SQLITE_SRC))

    assert tables == {"meta", "types", "objects", "blobs"}, (
        f"SQLite table set changed: {tables}. A new durable table (e.g. a "
        "federation idempotency-key ledger) is a §5-cut — it needs an ADR; v1 "
        "federation adds none."
    )


def test_sqlite_schema_ddl_pinned() -> None:
    """Given the SQLite backend's schema text, whitespace-normalized
    When its SHA-256 digest is taken
    Then it equals the pinned digest

    This catches column-level changes a new-table check would miss (e.g. an idem_key column
    on `objects`); normalizing whitespace keeps a reflow from tripping it.
    """
    match = re.search(r'_SCHEMA = """(.*?)"""', _SQLITE_SRC, re.DOTALL)
    assert match is not None, "could not locate _SCHEMA in sqlite.py"
    normalized = " ".join(match.group(1).split())

    digest = hashlib.sha256(normalized.encode()).hexdigest()

    assert digest == "7ecf6749251f5b40818dfc278995fec1b799a2ff29ff92cf3ce24a83a9641c9d", (
        f"SQLite DDL changed (now {digest}). If this is a real schema change, "
        "update the pin and add/cite the ADR in the same PR."
    )
