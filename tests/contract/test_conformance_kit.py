"""The conformance kit certifies honest consumers and catches evil twins.

KICKOFF M3: "conformance kit + evil twins that must fail each section
(incl. a missing-prior-value twin)". Every twin here implements exactly ONE
spec violation; the kit must name the violated section — that is the proof
the kit can detect that violation class at all.
"""

from __future__ import annotations

from typing import Any

import msgspec
import pytest

from datacrystal.contract import ReferenceApplier
from datacrystal.contract.applier import CONTRACT_VERSION, DeltaFormatError, DeltaGapError
from datacrystal.testing import CountingConsumer, check_delta_consumer

_decode = msgspec.msgpack.Decoder().decode

ALL_SECTIONS = [
    "§4.1/§4.3 ordering",
    "§4.2 idempotency",
    "§4.4 gap refusal",
    "§4.5 version refusal",
    "§3 unknown op",
    "§3 prior un-index",
    "§3.1 delete totality",
    "§2 stamp indifference",
]


# -- honest consumers must pass ------------------------------------------------

def applier_content(applier: ReferenceApplier) -> object:
    """Return the derived state of a reference applier for the kit's content checks."""
    return (dict(applier.objects), dict(applier.types))


def test_counting_consumer_passes_every_section() -> None:
    """Given the shipped CountingConsumer and its content reader
    When the conformance kit certifies it
    Then every section ran
    """
    ran = check_delta_consumer(CountingConsumer, content=content_of)

    assert ran == ALL_SECTIONS


def test_reference_applier_passes_every_section() -> None:
    """Given the ReferenceApplier and a reader of its objects and types
    When the conformance kit certifies it
    Then every section ran
    """
    ran = check_delta_consumer(ReferenceApplier, content=applier_content)

    assert ran == ALL_SECTIONS


def test_without_content_the_prior_section_is_skipped() -> None:
    """Given the shipped CountingConsumer and no content reader
    When the conformance kit certifies it
    Then the prior un-index section did not run
    """
    ran = check_delta_consumer(CountingConsumer)

    assert "§3 prior un-index" not in ran


def test_without_content_the_delete_totality_section_is_skipped() -> None:
    """Given the shipped CountingConsumer and no content reader
    When the conformance kit certifies it
    Then the delete totality section did not run
    """
    ran = check_delta_consumer(CountingConsumer)

    assert "§3.1 delete totality" not in ran


# -- a value-derived consumer (mini inverted index) -----------------------------

class _TermIndex:
    """A tiny in-memory inverted index: (oid, word) pairs over str fields.

    The smallest consumer whose derived state depends on record VALUES —
    the shape that genuinely needs ``prior`` payloads to un-index.
    """

    def __init__(self) -> None:
        self.watermark = 0
        self.terms: set[tuple[int, str]] = set()
        self.types: dict[int, str] = {}

    @staticmethod
    def _words(payload: bytes) -> set[str]:
        return {
            word
            for value in _decode(payload)
            if isinstance(value, str)
            for word in value.split()
        }

    def _forget(self, oid: int, prior: bytes) -> None:
        self.terms -= {(oid, word) for word in self._words(prior)}

    def _forget_on_update(self, oid: int, prior: bytes) -> None:
        self._forget(oid, prior)

    def apply(self, delta: dict[str, Any]) -> bool:
        if delta["v"] != CONTRACT_VERSION:  # §4.5 exactness, both directions
            raise DeltaFormatError("unsupported contract version")
        tid = delta["tid"]
        if tid <= self.watermark:
            return False
        if tid != self.watermark + 1:
            raise DeltaGapError("gap — resync")
        for cid, typename, _fields in delta["types"]:
            self.types[cid] = typename
        for op in delta["ops"]:
            kind, oid, prior = op["op"], op["oid"], op["prior"]
            if kind == "upsert":
                if prior is not None:
                    self._forget_on_update(oid, prior)
                self.terms |= {(oid, w) for w in self._words(op["payload"])}
            elif kind == "delete":
                self._forget(oid, prior)
            else:
                raise DeltaFormatError(f"unknown op {kind!r}")
        self.watermark = tid
        return True

    def content(self) -> tuple[list[tuple[int, str]], dict[int, str]]:
        return (sorted(self.terms), dict(sorted(self.types.items())))


def content_of(consumer: CountingConsumer | _TermIndex) -> object:
    """Return the consumer's derived state for the kit's content checks."""
    return consumer.content()


def test_term_index_passes_every_section() -> None:
    """Given a value-derived inverted-index consumer and its content reader
    When the conformance kit certifies it
    Then every section ran
    """
    ran = check_delta_consumer(_TermIndex, content=content_of)

    assert ran == ALL_SECTIONS


# -- evil twins: one violation each, the kit must name the section --------------

class _GapBlind(CountingConsumer):
    """§4.4 violation: pretends it saw the missing history."""

    def apply(self, delta: dict[str, Any]) -> bool:
        if delta["tid"] > self.watermark + 1:
            self.watermark = delta["tid"] - 1
        return super().apply(delta)


class _Reapplier(CountingConsumer):
    """§4.2 violation: an already-applied delta is applied again."""

    def apply(self, delta: dict[str, Any]) -> bool:
        if delta["tid"] <= self.watermark:
            for op in delta["ops"]:
                if op["op"] == "upsert":
                    typename = self._typename_by_cid[op["cid"]]
                    self.counts[typename] = self.counts.get(typename, 0) + 1
            return True
        return super().apply(delta)


class _VersionBlind(CountingConsumer):
    """§4.5 violation: quietly downgrades a newer contract version."""

    def apply(self, delta: dict[str, Any]) -> bool:
        return super().apply({**delta, "v": CONTRACT_VERSION})


class _PartialApplierOnVersion(CountingConsumer):
    """§4.5 violation, the OTHER half of the sentence ("never partially applied").

    Folds types+ops into derived state FIRST and validates the
    version LAST — raises loudly, watermark untouched, residue retained.
    The natural late-validation bug; the W1 review's PoC, pinned closed.
    """

    def apply(self, delta: dict[str, Any]) -> bool:
        if delta["v"] != CONTRACT_VERSION:
            for cid, typename, _fields in delta["types"]:
                self._typename_by_cid[cid] = typename
            for op in delta["ops"]:
                if op["op"] == "upsert":
                    typename = self._typename_by_cid.get(op["cid"], "?")
                    if op["oid"] not in self._typename_by_oid:
                        self.counts[typename] = self.counts.get(typename, 0) + 1
                    self._typename_by_oid[op["oid"]] = typename
            raise DeltaFormatError("unsupported contract version (too late)")
        return super().apply(delta)


class _OpGuesser(CountingConsumer):
    """§3 violation: guesses that an unknown op is probably an upsert."""

    def apply(self, delta: dict[str, Any]) -> bool:
        ops = [
            {**op, "op": "upsert"} if op["op"] not in ("upsert", "delete") else op
            for op in delta["ops"]
        ]
        return super().apply({**delta, "ops": ops})


class _StalePrior(_TermIndex):
    """§3 violation (THE missing-prior-value twin): an update indexes the
    new payload but never un-indexes the old one — stale terms survive.
    Deletes still work, so exactly the prior section catches fire.
    """

    def _forget_on_update(self, oid: int, prior: bytes) -> None:
        pass


class _DeleteIgnorer(_TermIndex):
    """§3.1 violation: silently drops delete tombstones."""

    def apply(self, delta: dict[str, Any]) -> bool:
        ops = [op for op in delta["ops"] if op["op"] != "delete"]
        return super().apply({**delta, "ops": ops})


def test_gap_blind_twin_fails_the_gap_section() -> None:
    """Given an evil twin that pretends it saw the missing history
    When the conformance kit certifies it
    Then the kit fails naming the gap refusal section
    """
    with pytest.raises(AssertionError) as failure:
        check_delta_consumer(_GapBlind, content=content_of)

    assert "§4.4" in str(failure.value)


def test_reapplier_twin_fails_the_idempotency_section() -> None:
    """Given an evil twin that applies an already-applied delta again
    When the conformance kit certifies it
    Then the kit fails naming the idempotency section
    """
    with pytest.raises(AssertionError) as failure:
        check_delta_consumer(_Reapplier, content=content_of)

    assert "§4.2" in str(failure.value)


def test_version_blind_twin_fails_the_version_section() -> None:
    """Given an evil twin that quietly downgrades a newer contract version
    When the conformance kit certifies it
    Then the kit fails naming the version refusal section
    """
    with pytest.raises(AssertionError) as failure:
        check_delta_consumer(_VersionBlind, content=content_of)

    assert "§4.5" in str(failure.value)


def test_partial_applier_twin_fails_the_version_section() -> None:
    """Given an evil twin that folds the delta into its state before it refuses the version
    When the conformance kit certifies it
    Then the kit fails naming the version refusal section
    """
    with pytest.raises(AssertionError) as failure:
        check_delta_consumer(_PartialApplierOnVersion, content=content_of)

    assert "§4.5" in str(failure.value)


def test_op_guesser_twin_fails_the_unknown_op_section() -> None:
    """Given an evil twin that guesses an unknown op is an upsert
    When the conformance kit certifies it
    Then the kit fails naming the unknown op section
    """
    with pytest.raises(AssertionError) as failure:
        check_delta_consumer(_OpGuesser, content=content_of)

    assert "§3 unknown op" in str(failure.value)


def test_delete_ignorer_twin_fails_the_delete_totality_section() -> None:
    """Given an evil twin that silently drops delete tombstones
    When the conformance kit certifies it
    Then the kit fails naming the delete totality section
    """
    with pytest.raises(AssertionError) as failure:
        check_delta_consumer(_DeleteIgnorer, content=content_of)

    assert "§3.1" in str(failure.value)


def test_missing_prior_value_twin_fails_the_prior_section() -> None:
    """Given the KICKOFF-named twin that ignores `prior` on update and keeps stale derived state
    When the conformance kit certifies it
    Then the kit fails naming the prior un-index section
    """
    with pytest.raises(AssertionError) as failure:
        check_delta_consumer(_StalePrior, content=content_of)

    assert "§3 prior un-index" in str(failure.value)


DELTA_WITHOUT_ANY_VERSION = {
    "f": "datacrystal-delta",
    "tid": 1,
    "actor": 0,
    "at": 0,
    "ops": [],
    "types": [],
    "root": None,
}


def test_missing_version_is_a_format_error_not_a_crash() -> None:
    """Given a delta with no `v` key at all
    When a consumer applies it
    Then it refuses with the typed DeltaFormatError, not a raw KeyError
    """
    with pytest.raises(DeltaFormatError):
        CountingConsumer().apply(DELTA_WITHOUT_ANY_VERSION)


@pytest.mark.parametrize("garbage_version", ["2", 2.000000001, None])
def test_garbage_version_is_a_format_error_not_a_crash(garbage_version: object) -> None:
    """Given a delta whose `v` is a string, a near-integer float or None
    When a consumer applies it
    Then it refuses with the typed DeltaFormatError, not a raw TypeError
    """
    delta = {**DELTA_WITHOUT_ANY_VERSION, "v": garbage_version}

    with pytest.raises(DeltaFormatError):
        CountingConsumer().apply(delta)
