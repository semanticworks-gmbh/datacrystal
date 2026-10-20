"""Property-based codec invariant: any scalar/container tree round-trips."""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from datacrystal._records import decode_payload, encode_payload

scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=False),
    st.text(max_size=50),
    st.binary(max_size=50),
)


def _containers_of(children: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    return st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=10), children, max_size=5),
    )


values = st.recursive(scalars, _containers_of, max_leaves=20)


def _refuse_refs(entity: object) -> int:
    raise AssertionError("scalar trees must not hit the ref path")


@given(st.lists(values, max_size=5))
def test_scalar_trees_survive_an_encode_decode_roundtrip(payload_values: list[Any]) -> None:
    """Given any list of scalar and container trees
    When it is encoded to a record payload and decoded again
    Then the decoded list equals the original
    """
    decoded = decode_payload(encode_payload(payload_values, _refuse_refs))

    assert decoded == payload_values
