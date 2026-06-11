"""Codec invariants: swizzling, scalars, loud rejections, checksums."""

from __future__ import annotations

import dataclasses

import pytest

import datacrystal as dc
from datacrystal._records import RefToken, crc, decode_payload, encode_payload
from tests.conftest import Mineral


def _no_refs(obj):
    raise AssertionError("oid_for must not be called for pure scalars")


def test_scalars_roundtrip():
    values = [None, True, 42, 3.14, "text", b"bytes", [1, [2, 3]], {"k": [True, None]}]
    assert decode_payload(encode_payload(values, _no_refs)) == values


def test_tuples_become_lists():
    assert decode_payload(encode_payload([(1, 2)], _no_refs)) == [[1, 2]]


def test_entities_swizzle_to_ref_tokens():
    m = Mineral(qid="Q1", name="quartz")
    payload = encode_payload([m, [m], {"k": m}], lambda obj: 4242)
    direct, in_list, in_dict = decode_payload(payload)
    assert isinstance(direct, RefToken) and direct.oid == 4242
    assert in_list[0].oid == 4242 and in_dict["k"].oid == 4242


def test_lazy_swizzles_to_its_target_oid():
    m = Mineral(qid="Q1", name="quartz")
    payload = encode_payload([dc.Lazy.of(m)], lambda obj: 7)
    (token,) = decode_payload(payload)
    assert isinstance(token, RefToken) and token.oid == 7


def test_plain_dataclasses_rejected_loudly():
    @dataclasses.dataclass
    class NotAnEntity:
        x: int = 1

    with pytest.raises(TypeError, match="plain dataclass"):
        encode_payload([NotAnEntity()], _no_refs)


def test_sets_rejected_loudly():
    with pytest.raises(TypeError, match="sets"):
        encode_payload([{1, 2}], _no_refs)


def test_crc_detects_corruption():
    payload = encode_payload(["important"], _no_refs)
    checksum = crc(payload)
    assert crc(payload[:-1] + b"\x00") != checksum
