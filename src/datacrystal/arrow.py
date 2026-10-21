"""``datacrystal[arrow]``: persistent parquet mirrors of the object graph (ROADMAP item 7).

A COMMIT-DELTA-v2 consumer keeping one Arrow table per mirrored type, persisted as parquet segments
under an atomic ``manifest.json``; ``compact()`` leaves a plain parquet datalake.
A mirror of a protected class is plaintext on disk (ADR-008 R13), and a mirror directory has one
owner process and one owner thread for ``apply()`` and ``table()`` (docs/design/DESIGN.md,
"Arrow mirror"). Storage model and schema lattice: docs/how-to/analytics.md.
"""

from __future__ import annotations

import datetime as _dt
import decimal as _decimal
import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Self, cast

import msgspec

from datacrystal._entity import TYPES_BY_NAME, type_info
from datacrystal._errors import DataCrystalError, ReadDeniedError
from datacrystal._permissions import PERM_FIELDS, is_root
from datacrystal._records import (
    _OID_STRUCT,  # codec primitive, parquet mirror
    DATE_EXT_CODE,
    NAIVE_DATETIME_EXT_CODE,
    REF_EXT_CODE,
    TIME_EXT_CODE,
    RefToken,
    _ext_hook,  # codec primitive, parquet mirror
    decimal_ext,
    decode_payload,
)
from datacrystal._snapshot import Ref
from datacrystal.contract.applier import (
    CONTRACT_VERSION,
    DeltaFormatError,
    DeltaGapError,
    check_delta_header,
)

try:
    import pyarrow as pa
    import pyarrow.compute as _pc
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover — core-only installs
    raise ImportError(
        "datacrystal.arrow requires the [arrow] extra — install with "
        "`pip install 'datacrystal[arrow]'` (adds pyarrow)"
    ) from exc

# Compute kernels are registered dynamically; pyarrow's bundled stubs do not
# know them, so fetch by name (runtime-identical to pc.invert).
_invert: Any = _pc.invert  # pyrefly: ignore[missing-attribute] - pyarrow.compute builds it at runtime

# ``pyarrow.parquet`` ships no ``.pyi`` (only untyped ``.py``), so its public
# functions arrive partially-unknown; fetch by name (like _invert above) and
# bind typed aliases matching the real runtime signatures (runtime-identical).
_write_table = cast(
    "Callable[[pa.Table, str | Path], None]", pq.write_table
)
_read_table = cast("Callable[[str | Path], pa.Table]", pq.read_table)

__all__ = ["ArrowMirror", "MirrorConfigError", "decode_fallback"]

MIRROR_FORMAT = "datacrystal-arrow-mirror"
MIRROR_VERSION = 1

_OID_COL = "__oid__"
_DELETED_COL = "__deleted__"
_TAGS_META = b"datacrystal_tags"

_TOMBSTONE: Any = object()  # pending-row sentinel: this OID is deleted


class MirrorConfigError(DataCrystalError):
    """The mirror directory contradicts this configuration — its content
    would be stale for the new settings; rebuild rather than guess
    (invariant 11: sidecars are rebuildable derived data).
    """


# -- value lattice -------------------------------------------------------------
#
# Tags are the persisted truth about a column's meaning (parquet types alone
# cannot distinguish an int64 OID reference from an int64 quantity). They are
# stamped into each segment's schema metadata and unified in the manifest.

_NUMERIC_RANK = {"bool": 0, "int": 1, "float": 2}
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def _tag_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int" if _INT64_MIN <= value <= _INT64_MAX else "fallback"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "bytes"
    if isinstance(value, (RefToken, Ref)):
        return "ref"
    if isinstance(value, _dt.datetime):  # before date: a datetime IS a date
        return "ts" if value.tzinfo is None else "ts_utc"
    if isinstance(value, _dt.date):
        return "date"
    if isinstance(value, _dt.time):
        # ISO offset times survive the codec but have no arrow type — fallback
        return "time" if value.tzinfo is None else "fallback"
    if isinstance(value, _decimal.Decimal):
        return _decimal_tag(value)
    if isinstance(value, (list, tuple)):
        inner = "null"
        for item in cast("tuple[object, ...] | list[object]", value):
            inner = _unify_tags(inner, _tag_of(item))
        return "fallback" if inner == "fallback" else f"list<{inner}>"
    return "fallback"  # dicts/mappings and anything exotic


_DECIMAL_PREFIX = "decimal:"
_DECIMAL_MAX_PRECISION = 38  # Arrow decimal128


def _decimal_tag(value: _decimal.Decimal) -> str:
    """``decimal:<precision>:<scale>`` fitting ``value`` exactly; a NaN/Infinity
    or a value beyond Arrow decimal128's 38 digits takes the exact-text fallback.
    """
    if not value.is_finite():
        return "fallback"
    _sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    scale = max(-exponent, 0)
    integer_digits = max(len(digits) + exponent, 1)
    if integer_digits + scale > _DECIMAL_MAX_PRECISION:
        return "fallback"
    return f"{_DECIMAL_PREFIX}{integer_digits + scale}:{scale}"


def _decimal_parts(tag: str) -> tuple[int, int]:
    precision, scale = tag[len(_DECIMAL_PREFIX):].split(":")
    return int(precision), int(scale)


def _unify_decimal_tags(a: str, b: str) -> str:
    """Return the narrowest decimal tag covering both, or "fallback" past 38 digits."""
    (pa_, sa), (pb, sb) = _decimal_parts(a), _decimal_parts(b)
    scale = max(sa, sb)
    precision = max(pa_ - sa, pb - sb) + scale
    if precision > _DECIMAL_MAX_PRECISION:
        return "fallback"
    return f"{_DECIMAL_PREFIX}{precision}:{scale}"


def _unify_tags(a: str, b: str) -> str:
    if a == b:
        return a
    if a.startswith(_DECIMAL_PREFIX) and b.startswith(_DECIMAL_PREFIX):
        return _unify_decimal_tags(a, b)
    if a == "null":
        return b
    if b == "null":
        return a
    if a in _NUMERIC_RANK and b in _NUMERIC_RANK:
        return a if _NUMERIC_RANK[a] >= _NUMERIC_RANK[b] else b
    if a.startswith("list<") and b.startswith("list<"):
        inner = _unify_tags(a[5:-1], b[5:-1])
        return "fallback" if inner == "fallback" else f"list<{inner}>"
    return "fallback"


def _arrow_type(tag: str) -> pa.DataType:
    if tag.startswith(_DECIMAL_PREFIX):
        precision, scale = _decimal_parts(tag)
        return pa.decimal128(precision, scale)
    if tag.startswith("list<"):
        return pa.list_(_arrow_type(tag[5:-1]))
    return {
        "null": pa.null(),
        "bool": pa.bool_(),
        "int": pa.int64(),
        "float": pa.float64(),
        "str": pa.string(),
        "bytes": pa.binary(),
        "ref": pa.int64(),
        "ts": pa.timestamp("us"),
        "ts_utc": pa.timestamp("us", tz="UTC"),
        "date": pa.date32(),
        "time": pa.time64("us"),
        "fallback": pa.binary(),
    }[tag]


def _storable(value: Any, tag: str) -> Any:
    """One decoded value → what pyarrow stores under ``tag``."""
    if value is None:
        return None
    if tag == "ref":
        return value.oid
    if tag == "fallback":
        return _encode_fallback(value)
    if tag.startswith("list<"):
        inner = tag[5:-1]
        return [_storable(item, inner) for item in value]
    if tag == "float" and isinstance(value, int):
        return float(value)
    if tag == "int" and isinstance(value, bool):
        return int(value)
    if tag == "bytes":
        return bytes(value)
    return value


def _semantic(value: Any, tag: str) -> Any:
    """Return the decoded value for an arrow-read ``value``; the inverse of :func:`_storable`."""
    if value is None:
        return None
    if tag == "ref":
        return RefToken(value)
    if tag == "fallback":
        return decode_fallback(value)
    if tag.startswith("list<"):
        inner = tag[5:-1]
        return [_semantic(item, inner) for item in value]
    return value


def _array(values: list[Any], tag: str) -> pa.Array:
    return pa.array([_storable(v, tag) for v in values], _arrow_type(tag))


def _field(name: str, tag: str) -> pa.Field:
    return pa.field(name, _arrow_type(tag), metadata={"datacrystal.tag": tag})


# -- fallback codec --------------------------------------------------------------
#
# Fallback bytes re-use the record codec's extension vocabulary (refs as
# ext-1 OIDs, temporals as ext-2/3/4 ISO text), so they stay pickle-free and
# decode with the same hook the engine uses.

_FALLBACK_ENCODER = msgspec.msgpack.Encoder()
_FALLBACK_DECODER = msgspec.msgpack.Decoder(ext_hook=_ext_hook)


def _reswizzle(value: Any) -> Any:
    if isinstance(value, (RefToken, Ref)):
        return msgspec.msgpack.Ext(REF_EXT_CODE, _OID_STRUCT.pack(value.oid))
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            return msgspec.msgpack.Ext(
                NAIVE_DATETIME_EXT_CODE, value.isoformat().encode()
            )
        return value  # aware: msgspec's native msgpack timestamp ext
    if isinstance(value, _dt.date):
        return msgspec.msgpack.Ext(DATE_EXT_CODE, value.isoformat().encode())
    if isinstance(value, _dt.time):
        return msgspec.msgpack.Ext(TIME_EXT_CODE, value.isoformat().encode())
    if isinstance(value, _decimal.Decimal):
        return decimal_ext(value)
    if isinstance(value, (list, tuple)):
        return [_reswizzle(item) for item in cast("tuple[object, ...] | list[object]", value)]
    if isinstance(value, Mapping):
        return {
            key: _reswizzle(item)
            for key, item in cast("Mapping[object, object]", value).items()
        }
    return value


def _encode_fallback(value: Any) -> bytes:
    return _FALLBACK_ENCODER.encode(_reswizzle(value))


def decode_fallback(buf: bytes) -> Any:
    """Decode one msgpack-fallback cell back to its value (refs come back
    as ``RefToken``s, temporals as their datetime types).
    """
    return _FALLBACK_DECODER.decode(buf)


def _safe_dirname(typename: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", typename)
    return f"{stem}-{hashlib.sha1(typename.encode()).hexdigest()[:8]}"


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _TableState:
    """Mutable bookkeeping for one mirrored type."""

    __slots__ = ("columns", "next_segment", "segments")

    def __init__(self, columns: dict[str, str] | None = None,
                 segments: list[str] | None = None, next_segment: int = 1) -> None:
        self.columns: dict[str, str] = columns or {}
        self.segments: list[str] = segments or []
        self.next_segment = next_segment


class ArrowMirror:
    """A COMMIT-DELTA-v2 consumer mirroring committed entities to parquet.

    On a fresh store attach it directly; on a lived-in store use :meth:`bootstrap` (spec §5).
    ``only`` restricts the mirrored types; ``flush_every`` batches N deltas per flush (1 = as
    durable as the store); reopening with other settings raises :class:`MirrorConfigError`.
    ``rows_flushed`` counts the last flush's rows, for O(delta) gates (fitness #9).
    ``OID_COLUMN`` is the int64 key to join a datacrystal query with DuckDB.
    """

    #: Name of the int64 primary-key column in every ``table()`` / parquet
    #: segment — the OID handoff key (see the GUIDE's analytics recipe).
    OID_COLUMN = _OID_COL

    def __init__(self, path: str | Path, *,
                 only: Iterable[type | str] | None = None,
                 flush_every: int = 1, max_segments: int = 16,
                 _wipe: bool = False) -> None:
        """Open or create the mirror directory at ``path``.

        Raises MirrorConfigError for ``flush_every`` below 1 or ``max_segments`` below 2.
        """
        if flush_every < 1:
            raise MirrorConfigError("flush_every must be >= 1")
        if max_segments < 2:
            raise MirrorConfigError("max_segments must be >= 2")
        self._only = (
            None if only is None
            else tuple(sorted(
                t if isinstance(t, str) else type_info(t).typename for t in only
            ))
        )
        self._flush_every = flush_every
        self._max_segments = max_segments
        self._dir = Path(path)
        if _wipe and self._dir.exists():
            shutil.rmtree(self._dir)
        (self._dir / "data").mkdir(parents=True, exist_ok=True)
        self._types: dict[int, tuple[str, list[str]]] = {}
        self._tables: dict[str, _TableState] = {}
        self._watermark = 0
        self._pending: dict[str, dict[int, Any]] = {}
        self._applies_since_flush = 0
        # Bootstrap streams many small batches; suppressing per-flush
        # compaction keeps it to ≤1 compaction per type (#16, no O(M²) thrash).
        self._suppress_compaction = False
        self.rows_flushed = 0  # rows written by the last flush (O(delta) evidence)
        self._load_manifest()
        self._sweep_orphans()

    # -- consumer surface (COMMIT-DELTA-v1 §4) ----------------------------------

    @property
    def watermark(self) -> int:
        """Highest TID fully applied. With ``flush_every > 1`` this can run
        ahead of the *durable* watermark in ``manifest.json`` — reopening
        resumes from the manifest.
        """
        return self._watermark

    def apply(self, delta: dict[str, Any]) -> bool:
        """Mirror one delta; return False on an idempotent skip (§4.2), True when it advanced.

        Raises DeltaFormatError for a foreign marker or a non-current version, DeltaGapError on a
        TID gap.
        """
        check_delta_header(
            delta,
            consumer="mirror",
            upgrade="upgrade datacrystal[arrow]",
            remedy="rebuild the mirror",
        )
        tid = delta["tid"]
        if tid <= self._watermark:
            return False  # §4.2: apply-twice ≡ apply-once
        if tid != self._watermark + 1:
            raise DeltaGapError(
                f"delta tid {tid} skips past watermark {self._watermark} — "
                "deltas are not retained; rebuild via ArrowMirror.bootstrap()"
            )
        new_types = {
            cid: (typename, list(fields))
            for cid, typename, fields in delta["types"]
        }
        lineage = self._types | new_types
        # Decode and validate EVERY op before mutating anything: a refused
        # delta (unknown op, unseen cid) must leave no trace (§4.4 shape).
        batch: list[tuple[str, int, Any]] = []
        for op in delta["ops"]:
            kind, oid = op["op"], op["oid"]
            if kind not in ("upsert", "delete"):
                raise DeltaFormatError(f"unknown op {kind!r} — refusing to guess")
            known = lineage.get(op["cid"])
            if known is None:
                raise DeltaFormatError(
                    f"op references cid {op['cid']} this mirror never saw — a "
                    "consumer joining mid-stream must ArrowMirror.bootstrap() "
                    "from a snapshot"
                )
            typename, persisted = known
            if not self._mirrors(typename):
                continue
            if kind == "delete":
                if op["prior"] is None:
                    raise DeltaFormatError(f"delete of oid {oid} carries no prior")
                batch.append((typename, oid, _TOMBSTONE))
            else:
                batch.append(
                    (typename, oid, self._row_values(typename, persisted,
                                                     op["payload"]))
                )
        self._types = lineage
        for typename, oid, row in batch:
            self._pending.setdefault(typename, {})[oid] = row
        self._watermark = tid
        self._applies_since_flush += 1
        if self._applies_since_flush >= self._flush_every:
            self.flush()
        return True

    # -- bootstrap (the §5 mid-life attach recipe) --------------------------------

    @classmethod
    def bootstrap(cls, path: str | Path, snapshot: Any, *,
                  only: Iterable[type | str] | None = None,
                  flush_every: int = 1, max_segments: int = 16,
                  batch: int = 50_000) -> ArrowMirror:
        """Build the mirror from one ``store.snapshot()``, replacing any directory at ``path``.

        The extent streams in ``batch``-sized chunks (#16): a store larger than RAM can be mirrored.
        The final flush alone stamps the watermark, so a crash mid-bootstrap forces a re-bootstrap.
        A protected type needs a root-bound snapshot (``dc.root_principal``, ADR-008 R9), as the
        mirror is plaintext; otherwise ReadDeniedError.
        """
        if batch < 1:
            raise MirrorConfigError("batch must be >= 1")
        cls._require_root_for_protected(snapshot, only)
        mirror = cls(path, only=only, flush_every=flush_every,
                     max_segments=max_segments, _wipe=True)
        for cid, typename, fields in snapshot.types:
            mirror._types[cid] = (typename, list(fields))
        mirror._suppress_compaction = True
        buffered = 0
        for typename in sorted({row[1] for row in snapshot.types}):
            if not mirror._mirrors(typename):
                continue
            for oid, values in snapshot._stream(typename):
                mirror._pending.setdefault(typename, {})[oid] = values
                buffered += 1
                if buffered >= batch:
                    mirror.flush()  # watermark stays 0 (deferred); no compaction
                    buffered = 0
        mirror._suppress_compaction = False
        mirror._watermark = snapshot.tid
        mirror.flush()  # the ONLY flush that stamps the real watermark
        return mirror

    @staticmethod
    def _require_root_for_protected(
        snapshot: Any, only: Iterable[type | str] | None
    ) -> None:
        """Fail closed before anything is wiped if a protected type would be mirrored without root.

        Raising up front keeps a mistaken non-root call from destroying a good mirror (``_wipe``) or
        leaving a half-written one. This is enforcement, never elevation: the caller chooses root.
        """
        principal = getattr(snapshot, "principal", None)
        if principal is not None and is_root(principal):
            return  # audited break-glass: the whole extent streams freely
        only_names = (
            None if only is None
            else {t if isinstance(t, str) else type_info(t).typename for t in only}
        )
        for _cid, typename, fields in snapshot.types:
            if only_names is not None and typename not in only_names:
                continue
            ti = TYPES_BY_NAME.get(typename)
            label_bearing = PERM_FIELDS[0] in fields  # persisted _dc_owner column
            if (ti is not None and ti.protected) or label_bearing:
                raise ReadDeniedError(
                    f"ArrowMirror.bootstrap over protected type {typename!r} "
                    "needs a root-bound snapshot: the parquet mirror is a full "
                    "plaintext copy of the extent (protected _dc_* columns and "
                    "data included), so mint it as the audited store root — "
                    "store.snapshot(principal=dc.root_principal(uid)). A "
                    "non-root bootstrap fails closed (ADR-008 W4-5)."
                )

    # -- reads ---------------------------------------------------------------------

    @property
    def typenames(self) -> tuple[str, ...]:
        """Every type this mirror holds rows or schema for."""
        return tuple(sorted(set(self._tables) | set(self._pending)))

    def table(self, cls_or_typename: type | str) -> pa.Table:
        """Return the current mirror of one type as an immutable ``pyarrow.Table``.

        One row per live entity (``__oid__`` first, then the fields by name) at this mirror's
        watermark,
        unflushed rows included; DuckDB, polars and pandas read it zero-copy.
        """
        typename = (
            cls_or_typename if isinstance(cls_or_typename, str)
            else type_info(cls_or_typename).typename
        )
        state = self._tables.get(typename, _TableState())
        columns = dict(state.columns)
        pending = self._pending.get(typename, {})
        for row in pending.values():
            if row is not _TOMBSTONE:
                for name, value in row.items():
                    self._check_reserved(name)
                    columns[name] = _unify_tags(columns.get(name, "null"),
                                                _tag_of(value))
        parts = [
            self._read_segment(typename, segment, columns)
            for segment in state.segments
        ]
        if pending:
            parts.append(self._pending_table(pending, columns))
        folded = self._fold(parts, columns)
        live = folded.filter(_invert(folded[_DELETED_COL].combine_chunks()))
        return live.drop_columns([_DELETED_COL]).sort_by(_OID_COL)

    def compact(self) -> None:
        """Collapse every mirrored type to one fold-free segment (and drop
        tombstones for good). The ``data/`` directory is afterwards directly
        readable as plain parquet — one file per type, current state only.
        """
        self.flush()
        doomed: list[Path] = []
        for typename, state in self._tables.items():
            if len(state.segments) <= 1:
                continue
            doomed += self._compact_type(typename, state)
        self._write_manifest()
        for path in doomed:
            path.unlink(missing_ok=True)

    def parquet_dir(self, cls_or_typename: type | str) -> Path:
        """Return the directory holding one type's parquet segments, for DuckDB ``read_parquet``.

        After :meth:`compact` it holds exactly one fold-free file; before, several LSM segments that
        need newest-wins folding and tombstone filtering. It exists once the type has been flushed.
        """
        typename = (
            cls_or_typename if isinstance(cls_or_typename, str)
            else type_info(cls_or_typename).typename
        )
        return self._segment_dir(typename)

    # -- durability -------------------------------------------------------------------

    def flush(self) -> None:
        """Persist pending rows as one new segment per touched type and
        commit the manifest (the durable watermark moves here — atomically,
        via temp-file + rename).
        """
        doomed: list[Path] = []
        self.rows_flushed = 0
        for typename, rows in sorted(self._pending.items()):
            if not rows:
                continue
            state = self._tables.setdefault(typename, _TableState())
            for row in rows.values():
                if row is not _TOMBSTONE:
                    for name, value in row.items():
                        self._check_reserved(name)
                        state.columns[name] = _unify_tags(
                            state.columns.get(name, "null"), _tag_of(value)
                        )
            self._write_segment(typename, state, rows)
            self.rows_flushed += len(rows)
            if (not self._suppress_compaction
                    and len(state.segments) > self._max_segments):
                doomed += self._compact_type(typename, state)
        self._write_manifest()
        for path in doomed:
            path.unlink(missing_ok=True)
        self._pending.clear()
        self._applies_since_flush = 0

    def close(self) -> None:
        """Flush the pending rows."""
        self.flush()

    def __enter__(self) -> Self:
        """Return the mirror itself."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the mirror."""
        self.close()

    def __repr__(self) -> str:
        """Return the watermark and the mirrored types."""
        return (
            f"<datacrystal.arrow.ArrowMirror watermark={self._watermark} "
            f"types={list(self.typenames)}>"
        )

    # -- internals ----------------------------------------------------------------------

    def _mirrors(self, typename: str) -> bool:
        return self._only is None or typename in self._only

    @staticmethod
    def _check_reserved(name: str) -> None:
        if name in (_OID_COL, _DELETED_COL):
            raise DeltaFormatError(
                f"field name {name!r} collides with a reserved mirror column"
            )

    def _row_values(self, typename: str, persisted: list[str],
                    payload: bytes) -> dict[str, Any]:
        """Decode one payload to ``{field: value}`` — by NAME through its
        persisted shape, missing fields filled from the live class's
        defaults exactly like snapshot materialization (fitness #13:
        incremental ≡ bootstrap), or left absent (→ null) without one.
        """
        by_name = dict(zip(persisted, decode_payload(payload)))
        ti = TYPES_BY_NAME.get(typename)
        if ti is None:
            return by_name
        values: dict[str, Any] = {}
        for name in ti.field_names:
            if name in by_name:
                values[name] = by_name[name]
            else:
                factory = ti.defaults.get(name)
                if factory is not None:
                    values[name] = factory()
        # fields dropped from the live class are ignored, exactly like
        # hydration and snapshot views (invariant 8) — bootstrap and
        # incremental mirroring must produce identical rows (fitness #13).
        return values

    def _segment_dir(self, typename: str) -> Path:
        return self._dir / "data" / _safe_dirname(typename)

    def _pending_table(self, rows: dict[int, Any], columns: dict[str, str],
                       ) -> pa.Table:
        oids = list(rows)
        deleted = [rows[oid] is _TOMBSTONE for oid in oids]
        arrays = [
            pa.array(oids, pa.int64()),
            pa.array(deleted, pa.bool_()),
        ]
        fields = [pa.field(_OID_COL, pa.int64()), pa.field(_DELETED_COL, pa.bool_())]
        for name in sorted(columns):
            tag = columns[name]
            values = [
                None if rows[oid] is _TOMBSTONE else rows[oid].get(name)
                for oid in oids
            ]
            arrays.append(_array(values, tag))
            fields.append(_field(name, tag))
        return pa.Table.from_arrays(arrays, schema=pa.schema(fields))

    def _write_segment(self, typename: str, state: _TableState,
                       rows: dict[int, Any]) -> None:
        table = self._pending_table(rows, state.columns)
        table = table.replace_schema_metadata(
            {_TAGS_META: json.dumps(state.columns, sort_keys=True).encode()}
        )
        directory = self._segment_dir(typename)
        directory.mkdir(parents=True, exist_ok=True)
        name = f"seg-{state.next_segment:06d}.parquet"
        _write_table(table, directory / name)
        # The manifest is the commit point: a segment it names must be ON
        # DISK first, or a crash leaves the manifest pointing at bytes that
        # never landed (the watermark would lie — spec §4.3).
        _fsync_path(directory / name)
        _fsync_path(directory)
        state.segments.append(name)
        state.next_segment += 1

    def _read_segment(self, typename: str, segment: str,
                      columns: dict[str, str]) -> pa.Table:
        table = _read_table(self._segment_dir(typename) / segment)
        schema_meta = cast("dict[bytes, bytes] | None", table.schema.metadata)
        meta = (schema_meta or {}).get(_TAGS_META, b"{}")
        written_tags: dict[str, str] = json.loads(meta)
        n = table.num_rows
        arrays = [
            table[_OID_COL].combine_chunks().cast(pa.int64()),
            table[_DELETED_COL].combine_chunks().cast(pa.bool_()),
        ]
        fields = [pa.field(_OID_COL, pa.int64()), pa.field(_DELETED_COL, pa.bool_())]
        for name in sorted(columns):
            tag = columns[name]
            old_tag = written_tags.get(name)
            if old_tag is None:  # column discovered after this segment
                arrays.append(pa.nulls(n, _arrow_type(tag)))
            elif old_tag == tag:
                arrays.append(table[name].combine_chunks())
            else:  # promoted since: re-read under the current tag
                values = [
                    _semantic(v, old_tag) for v in table[name].to_pylist()
                ]
                arrays.append(_array(values, tag))
            fields.append(_field(name, tag))
        return pa.Table.from_arrays(arrays, schema=pa.schema(fields))

    def _fold(self, parts: list[pa.Table], columns: dict[str, str]) -> pa.Table:
        """Newest-wins per OID across segments (and pending), oldest →
        newest in ``parts``; the result still carries tombstone rows.
        """
        if not parts:
            fields = [pa.field(_OID_COL, pa.int64()),
                      pa.field(_DELETED_COL, pa.bool_())]
            fields += [_field(name, columns[name]) for name in sorted(columns)]
            return pa.schema(fields).empty_table()
        if len(parts) == 1:
            return parts[0]
        seen: set[int] = set()
        survivors: list[pa.Table] = []
        for part in reversed(parts):  # newest first
            oids = part[_OID_COL].to_pylist()
            if seen:
                mask = pa.array([oid not in seen for oid in oids], pa.bool_())
                part = part.filter(mask)
            seen.update(oids)
            survivors.append(part)
        survivors.reverse()
        return pa.concat_tables(survivors)

    def _compact_type(self, typename: str, state: _TableState) -> list[Path]:
        """Fold a type's segments into one, dropping tombstones (nothing
        older remains for them to shadow). Returns the obsolete files —
        the caller unlinks them AFTER the manifest stops naming them.
        """
        parts = [
            self._read_segment(typename, segment, state.columns)
            for segment in state.segments
        ]
        folded = self._fold(parts, state.columns)
        live = folded.filter(_invert(folded[_DELETED_COL].combine_chunks()))
        live = live.sort_by(_OID_COL)
        live = live.replace_schema_metadata(
            {_TAGS_META: json.dumps(state.columns, sort_keys=True).encode()}
        )
        directory = self._segment_dir(typename)
        name = f"seg-{state.next_segment:06d}.parquet"
        _write_table(live, directory / name)
        obsolete = [directory / old for old in state.segments]
        state.segments = [name]
        state.next_segment += 1
        return obsolete

    # -- manifest --------------------------------------------------------------------

    def _manifest_path(self) -> Path:
        return self._dir / "manifest.json"

    def _write_manifest(self) -> None:
        manifest = {
            "format": MIRROR_FORMAT,
            "version": MIRROR_VERSION,
            "contract_version": CONTRACT_VERSION,
            "watermark": self._watermark,
            "only": list(self._only) if self._only is not None else None,
            "types": [
                [cid, typename, fields]
                for cid, (typename, fields) in sorted(self._types.items())
            ],
            "tables": {
                typename: {
                    "columns": dict(sorted(state.columns.items())),
                    "segments": state.segments,
                    "next_segment": state.next_segment,
                }
                for typename, state in sorted(self._tables.items())
            },
        }
        tmp = self._manifest_path().with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._manifest_path())
        _fsync_path(self._dir)

    def _load_manifest(self) -> None:
        path = self._manifest_path()
        if not path.exists():
            return
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("format") != MIRROR_FORMAT:
            raise MirrorConfigError(
                f"{path} is not a datacrystal arrow mirror manifest"
            )
        if manifest["version"] > MIRROR_VERSION:
            raise MirrorConfigError(
                f"mirror format version {manifest['version']} is newer than "
                f"this datacrystal[arrow] supports ({MIRROR_VERSION})"
            )
        persisted_only = (
            None if manifest["only"] is None else tuple(manifest["only"])
        )
        if persisted_only != self._only:
            raise MirrorConfigError(
                f"this mirror was built with only={persisted_only!r}, not "
                f"{self._only!r} — its content would be stale for the new "
                "configuration; rebuild via ArrowMirror.bootstrap()"
            )
        self._watermark = manifest["watermark"]
        self._types = {
            cid: (typename, list(fields))
            for cid, typename, fields in manifest["types"]
        }
        self._tables = {
            typename: _TableState(
                columns=dict(entry["columns"]),
                segments=list(entry["segments"]),
                next_segment=entry["next_segment"],
            )
            for typename, entry in manifest["tables"].items()
        }

    def _sweep_orphans(self) -> None:
        """Delete segment files the manifest does not name — debris of a
        crash between segment write and manifest commit. Their numbers will
        be reused; sweeping prevents a stale file from masquerading later.
        """
        live = {
            (_safe_dirname(typename), segment)
            for typename, state in self._tables.items()
            for segment in state.segments
        }
        for directory in (self._dir / "data").iterdir():
            if not directory.is_dir():
                continue
            for file in directory.glob("seg-*.parquet"):
                if (directory.name, file.name) not in live:
                    file.unlink()
