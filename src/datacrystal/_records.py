"""Record codec: msgspec msgpack payloads with OID-swizzled references.

A persisted record is ``(oid, cid, tid, payload, crc)``. The payload is the
msgpack encoding of the entity's field values **in schema order** (the type
dictionary pins the order), with every entity reference — direct or wrapped
in ``Lazy`` — swizzled to a msgpack extension value carrying the 8-byte OID.

Swizzling is an explicit pre-pass over the value tree, NOT an ``enc_hook``:
msgspec encodes dataclasses natively, so a hook would never see an entity
and would silently inline it as a map. The pre-pass also rejects the two
silently-lossy shapes (non-entity dataclasses, sets) loudly.

There is **no pickle anywhere** (DESIGN.md): decoding is structurally
incapable of executing code. CRC32 per record guards against torn/corrupt
blobs (stress-test mandate). Known v0.1 mapping: tuples round-trip as lists
(msgpack has no tuple type).

Temporal values (format v2, 2026-06-12 — the MaStR feedback found naive
datetimes silently round-tripping as ISO strings): tz-*aware* ``datetime``
rides msgpack's standard timestamp extension (msgspec native; converted to
the UTC instant — the offset's identity is not preserved, the instant is).
Naive ``datetime``, ``date`` and ``time`` get datacrystal extension codes
carrying their ISO text — ``fromisoformat`` restores them exactly, and the
ext code (vs a bare string) is what makes the type survive the round trip.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import struct
import zlib
from typing import Any, Callable, cast

import msgspec

from datacrystal._entity import is_entity
from datacrystal._lazy import Lazy

REF_EXT_CODE = 1
NAIVE_DATETIME_EXT_CODE = 2  # ISO text; tz-aware datetimes use msgpack timestamps
DATE_EXT_CODE = 3            # ISO text
TIME_EXT_CODE = 4            # ISO text (naive or with offset — ISO carries both)
_OID_STRUCT = struct.Struct(">q")

_SCALARS = (type(None), bool, int, float, str, bytes)


class RefToken:
    """A decoded-but-unresolved entity reference (an OID placeholder).

    Equality is by OID so decode-level reads (``count``/``pluck`` residuals)
    can match entity-valued predicates without hydrating anything.
    """

    __slots__ = ("oid",)

    def __init__(self, oid: int) -> None:
        self.oid = oid

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RefToken) and other.oid == self.oid

    def __hash__(self) -> int:
        return hash((RefToken, self.oid))

    def __repr__(self) -> str:
        return f"RefToken({self.oid})"


def _ref_ext(oid: int) -> msgspec.msgpack.Ext:
    return msgspec.msgpack.Ext(REF_EXT_CODE, _OID_STRUCT.pack(oid))


def swizzle(value: Any, oid_for: Callable[[Any], int]) -> Any:
    """Replace entities/Lazy handles in a value tree with OID extensions.

    ``oid_for`` must return the OID of a live entity — the storer guarantees
    every reachable new entity is registered before encoding begins (P1).
    """
    if isinstance(value, _SCALARS):
        return value
    if is_entity(value):
        return _ref_ext(oid_for(value))
    if isinstance(value, Lazy):
        lazy = cast("Lazy[Any]", value)
        target = lazy.peek()
        if target is not None:
            return _ref_ext(oid_for(target))
        if lazy.oid is None:
            raise TypeError("unloaded Lazy reference without an OID cannot be stored")
        return _ref_ext(lazy.oid)
    if isinstance(value, (list, tuple)):
        return [swizzle(item, oid_for) for item in cast("list[object] | tuple[object, ...]", value)]
    if isinstance(value, dict):
        return {
            key: swizzle(item, oid_for)
            for key, item in cast("dict[Any, object]", value).items()
        }
    if isinstance(value, _dt.datetime):  # before date: datetime IS a date
        if value.tzinfo is None:
            return msgspec.msgpack.Ext(
                NAIVE_DATETIME_EXT_CODE, value.isoformat().encode()
            )
        return value  # aware: msgspec's native msgpack timestamp ext
    if isinstance(value, _dt.date):
        return msgspec.msgpack.Ext(DATE_EXT_CODE, value.isoformat().encode())
    if isinstance(value, _dt.time):
        return msgspec.msgpack.Ext(TIME_EXT_CODE, value.isoformat().encode())
    if dataclasses.is_dataclass(value):
        raise TypeError(
            f"{type(value).__name__} is a plain dataclass, not an @entity — "
            "it would round-trip as a dict; make it an entity or a scalar"
        )
    if isinstance(value, (set, frozenset)):
        raise TypeError("sets are not persistable in v0.1 — use a list")
    return value  # remaining scalars msgspec encodes natively


_ENCODER = msgspec.msgpack.Encoder()


def encode_payload(values: list[Any], oid_for: Callable[[Any], int]) -> bytes:
    """Encode a field-value list (schema order) to a record payload."""
    return _ENCODER.encode([swizzle(v, oid_for) for v in values])


def _ext_hook(code: int, data: memoryview) -> Any:
    if code == REF_EXT_CODE:
        return RefToken(_OID_STRUCT.unpack(data)[0])
    if code == NAIVE_DATETIME_EXT_CODE:
        return _dt.datetime.fromisoformat(str(data, "ascii"))
    if code == DATE_EXT_CODE:
        return _dt.date.fromisoformat(str(data, "ascii"))
    if code == TIME_EXT_CODE:
        return _dt.time.fromisoformat(str(data, "ascii"))
    return msgspec.msgpack.Ext(code, bytes(data))


_DECODER = msgspec.msgpack.Decoder(ext_hook=_ext_hook)


def decode_payload(payload: bytes) -> list[Any]:
    """Decode a record payload to its field-value list (refs as RefTokens)."""
    return _DECODER.decode(payload)


def crc(payload: bytes) -> int:
    return zlib.crc32(payload) & 0xFFFFFFFF
