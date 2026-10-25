"""Transports and wire data for the follower tests: a fake coordinator behind ``dc.HttpClient``.

A follower reads ``GET /v1/deltas`` frames and contributes with ``POST /v1/submit``.
These helpers serve and record both without a server, so a test drives
``dc.open_follower(url, client=...)`` through its public path.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Iterable
from typing import Any

import datacrystal as dc
from datacrystal.contract.applier import decode_delta, encode_delta

_FRAME = struct.Struct(">Q")

Delta = dict[str, Any]


def encode_frames(deltas: Iterable[Delta]) -> bytes:
    """Encode ``deltas`` as the length-prefixed ``>Q`` frames of a ``/v1/deltas`` body."""
    return b"".join(_FRAME.pack(len(raw)) + raw for raw in map(encode_delta, deltas))


def decode_frames(blob: bytes) -> list[Delta]:
    """Decode the length-prefixed ``>Q`` frames of a ``/v1/deltas`` body."""
    deltas: list[Delta] = []
    offset = 0
    while offset < len(blob):
        (size,) = _FRAME.unpack_from(blob, offset)
        offset += _FRAME.size
        deltas.append(decode_delta(blob[offset : offset + size]))
        offset += size
    return deltas


def occ_base_of(deltas: Iterable[Delta], oid: int) -> str | None:
    """Return the OCC base of ``oid``: the hex SHA-256 of its latest upserted payload.

    FEDERATION-WIRE-v1 defines the base as the hash of the very bytes that travel as ``payload``.
    """
    base = None
    for delta in deltas:
        for op in delta["ops"]:
            if op["op"] == "upsert" and op["oid"] == oid:
                base = hashlib.sha256(op["payload"]).hexdigest()
    return base


class FakeResponse:
    """The slice of ``httpx.Response`` a follower reads."""

    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content

    def raise_for_status(self) -> None:
        """Do nothing: the fake only answers successfully on ``GET``."""

    def json(self) -> object:
        """Return the decoded JSON body."""
        return json.loads(self.content)


class FakeCoordinator:
    """A ``dc.HttpClient`` serving ``deltas`` on ``GET`` and answering ``POST`` with a fixed reply.

    ``deltas`` may be reassigned between calls; ``posts`` records every ``/v1/submit`` body.
    """

    def __init__(
        self,
        deltas: Iterable[Delta] = (),
        *,
        submit_status: int = 200,
        submit_body: object = None,
    ) -> None:
        self.deltas = list(deltas)
        self.submit_status = submit_status
        self.submit_body = submit_body if submit_body is not None else {"applied_tid": 1, "keys": {}}
        self.posts: list[dict[str, Any]] = []

    def get(self, url: str, /, *, params: dict[str, int]) -> FakeResponse:
        """Serve the frames of every delta after ``params["after"]``."""
        after = params["after"]
        served = [delta for delta in self.deltas if int(delta["tid"]) > after]
        return FakeResponse(200, encode_frames(served))

    def post(self, url: str, /, *, json: dict[str, Any]) -> FakeResponse:
        """Record the submitted body and answer the configured reply."""
        self.posts.append(json)
        return FakeResponse(self.submit_status, _dump(self.submit_body))


class FrameDroppingClient:
    """Wrap a client and, once ``drop_first_frame`` is set, hide the first frame of each ``GET``."""

    def __init__(self, inner: dc.HttpClient) -> None:
        self.inner = inner
        self.drop_first_frame = False

    def get(self, url: str, /, *, params: dict[str, int]) -> FakeResponse:
        """Forward the ``GET``, dropping the first frame of its body when armed."""
        reply = self.inner.get(url, params=params)
        blob = reply.content
        if self.drop_first_frame:
            blob = encode_frames(decode_frames(blob)[1:])
        return FakeResponse(200, blob)

    def post(self, url: str, /, *, json: dict[str, Any]) -> FakeResponse:
        """Forward the ``POST`` untouched."""
        reply = self.inner.post(url, json=json)
        return FakeResponse(reply.status_code, reply.content)


def _dump(body: object) -> bytes:
    return json.dumps(body).encode()
