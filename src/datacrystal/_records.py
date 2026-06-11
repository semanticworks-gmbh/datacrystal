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
"""

from __future__ import annotations

import dataclasses
import struct
import zlib
from typing import Any, Callable

import msgspec

from datacrystal._entity import is_entity
from datacrystal._lazy import Lazy

REF_EXT_CODE = 1
_OID_STRUCT = struct.Struct(">q")

_SCALARS = (type(None), bool, int, float, str, bytes)


class RefToken:
    """A decoded-but-unresolved entity reference (an OID placeholder)."""

    __slots__ = ("oid",)

    def __init__(self, oid: int) -> None:
        self.oid = oid

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
        target = value.peek()
        if target is not None:
            return _ref_ext(oid_for(target))
        if value.oid is None:
            raise TypeError("unloaded Lazy reference without an OID cannot be stored")
        return _ref_ext(value.oid)
    if isinstance(value, (list, tuple)):
        return [swizzle(item, oid_for) for item in value]
    if isinstance(value, dict):
        return {key: swizzle(item, oid_for) for key, item in value.items()}
    if dataclasses.is_dataclass(value):
        raise TypeError(
            f"{type(value).__name__} is a plain dataclass, not an @entity — "
            "it would round-trip as a dict; make it an entity or a scalar"
        )
    if isinstance(value, (set, frozenset)):
        raise TypeError("sets are not persistable in v0.1 — use a list")
    return value  # datetime & friends: msgspec handles them natively


_ENCODER = msgspec.msgpack.Encoder()


def encode_payload(values: list[Any], oid_for: Callable[[Any], int]) -> bytes:
    """Encode a field-value list (schema order) to a record payload."""
    return _ENCODER.encode([swizzle(v, oid_for) for v in values])


def _ext_hook(code: int, data: memoryview) -> Any:
    if code == REF_EXT_CODE:
        return RefToken(_OID_STRUCT.unpack(data)[0])
    return msgspec.msgpack.Ext(code, bytes(data))


_DECODER = msgspec.msgpack.Decoder(ext_hook=_ext_hook)


def decode_payload(payload: bytes) -> list[Any]:
    """Decode a record payload to its field-value list (refs as RefTokens)."""
    return _DECODER.decode(payload)


def crc(payload: bytes) -> int:
    return zlib.crc32(payload) & 0xFFFFFFFF
