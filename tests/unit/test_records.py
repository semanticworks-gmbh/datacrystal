"""Codec invariants: swizzling, scalars, loud rejections, checksums."""

from __future__ import annotations

import dataclasses

import pytest

import datacrystal as dc
from datacrystal._records import RefToken, crc, decode_payload, encode_payload
from tests.conftest import Mineral


def _no_refs(obj: object) -> int:
    raise AssertionError("oid_for must not be called for pure scalars")


def _oid_4242(obj: object) -> int:
    return 4242


def _oid_7(obj: object) -> int:
    return 7


def test_scalars_survive_an_encode_decode_roundtrip() -> None:
    """Given scalars, nested lists and a dict of scalars
    When they are encoded and decoded
    Then the decoded values equal the originals
    """
    values = [None, True, 42, 3.14, "text", b"bytes", [1, [2, 3]], {"k": [True, None]}]

    decoded = decode_payload(encode_payload(values, _no_refs))

    assert decoded == values


def test_tuples_decode_as_lists() -> None:
    """Given a payload holding a tuple
    When it is encoded and decoded
    Then the tuple comes back as a list
    """
    decoded = decode_payload(encode_payload([(1, 2)], _no_refs))

    assert decoded == [[1, 2]]


def test_a_direct_entity_swizzles_to_a_ref_token() -> None:
    """Given a payload holding a mineral directly
    When it is encoded and decoded
    Then the mineral comes back as a RefToken with its oid
    """
    quartz = Mineral(qid="Q1", name="quartz")

    (direct,) = decode_payload(encode_payload([quartz], _oid_4242))

    assert isinstance(direct, RefToken)
    assert direct.oid == 4242


def test_an_entity_in_a_list_swizzles_to_a_ref_token() -> None:
    """Given a payload holding a mineral inside a list
    When it is encoded and decoded
    Then the list holds a RefToken with the mineral's oid
    """
    quartz = Mineral(qid="Q1", name="quartz")

    (in_list,) = decode_payload(encode_payload([[quartz]], _oid_4242))

    assert in_list[0].oid == 4242


def test_an_entity_in_a_dict_swizzles_to_a_ref_token() -> None:
    """Given a payload holding a mineral inside a dict
    When it is encoded and decoded
    Then the dict holds a RefToken with the mineral's oid
    """
    quartz = Mineral(qid="Q1", name="quartz")

    (in_dict,) = decode_payload(encode_payload([{"k": quartz}], _oid_4242))

    assert in_dict["k"].oid == 4242


def test_a_lazy_swizzles_to_a_ref_token_of_its_target() -> None:
    """Given a payload holding a Lazy pointing at a mineral
    When it is encoded and decoded
    Then it comes back as a RefToken with the target's oid
    """
    quartz = Mineral(qid="Q1", name="quartz")

    (token,) = decode_payload(encode_payload([dc.Lazy.of(quartz)], _oid_7))

    assert isinstance(token, RefToken)
    assert token.oid == 7


def test_plain_dataclasses_are_rejected_loudly() -> None:
    """Given a payload holding a plain dataclass that is not an entity
    When it is encoded
    Then encoding raises TypeError naming "plain dataclass"
    """

    @dataclasses.dataclass
    class NotAnEntity:
        x: int = 1

    with pytest.raises(TypeError, match="plain dataclass"):
        encode_payload([NotAnEntity()], _no_refs)


def test_sets_are_rejected_loudly() -> None:
    """Given a payload holding a set
    When it is encoded
    Then encoding raises TypeError naming "sets"
    """
    with pytest.raises(TypeError, match="sets"):
        encode_payload([{1, 2}], _no_refs)


def test_crc_changes_when_the_payload_is_corrupted() -> None:
    """Given an encoded payload and its checksum
    When the last byte is overwritten
    Then the checksum of the corrupted payload differs
    """
    payload = encode_payload(["important"], _no_refs)
    checksum = crc(payload)

    corrupted_checksum = crc(payload[:-1] + b"\x00")

    assert corrupted_checksum != checksum
