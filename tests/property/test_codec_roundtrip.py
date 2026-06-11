"""Property-based codec invariant: any scalar/container tree round-trips."""

from __future__ import annotations

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

values = st.recursive(
    scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=10), children, max_size=5),
    ),
    max_leaves=20,
)


def _no_refs(obj):
    raise AssertionError("scalar trees must not hit the ref path")


@given(st.lists(values, max_size=5))
def test_roundtrip(payload_values):
    assert decode_payload(encode_payload(payload_values, _no_refs)) == payload_values
