"""``datacrystal[web]`` federation surface — the coordinator's read endpoints.

:func:`federation_router` mounts the read shapes of the LOCKED
[FEDERATION-WIRE-v1](../../../docs/design/FEDERATION-WIRE-v1.md) contract
(ROADMAP item 21, epic #146): ``GET /v1/head`` (the watermark probe a follower
polls) and ``GET /v1/deltas?after=<tid>`` (COMMIT-DELTA-v1 frames — byte-for-byte
the length-prefixed frame the :class:`~datacrystal.deltalog.DeltaLog` already
writes, re-encoded from :meth:`~datacrystal.deltalog.DeltaLog.replay`). The write
shape (``POST /v1/submit``) is added by #152.

You bring your own authn/z: pass FastAPI ``dependencies=[Depends(...)]`` and they
apply to every federation route — nothing here is exempt from your auth (the same
seam as the rest of the extra).

``fastapi`` is imported only inside this submodule, so a bare ``import
datacrystal`` never pulls it (the dep-isolation fitness gate).
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, Response

from datacrystal.contract.applier import CONTRACT_VERSION, FORMAT_MARKER, encode_delta

if TYPE_CHECKING:
    from datacrystal._store import Store
    from datacrystal.deltalog import DeltaLog

# The on-the-wire frame: an 8-byte big-endian length prefix + one ``encode_delta``
# blob, repeated in strict TID order — identical to what the DeltaLog persists
# (``deltalog.py``) and LOCKED by FEDERATION-WIRE-v1. A change here is a new
# contract version, never an edit.
_FRAME = struct.Struct(">Q")

__all__ = ["federation_router"]


def federation_router(
    store: Store,
    deltalog: DeltaLog,
    *,
    dependencies: Sequence[Any] | None = None,
) -> APIRouter:
    """Build the federation **read** router (``/v1/head`` + ``/v1/deltas``).

    Args:
        store: the coordinator's store (the single writer); its
            :attr:`~datacrystal.Store.last_tid` feeds ``/v1/head``.
        deltalog: the :class:`~datacrystal.deltalog.DeltaLog` attached to
            ``store`` — :meth:`~datacrystal.deltalog.DeltaLog.replay` feeds
            ``/v1/deltas``. (The store exposes no delta-log accessor, so it is
            passed explicitly; attach it with ``store.attach(deltalog)`` first.)
        dependencies: FastAPI ``Depends(...)`` applied to every route — you bring
            authn/z.

    Returns:
        An :class:`fastapi.APIRouter` (prefix ``/v1``) to mount with
        ``app.include_router(...)``.
    """
    router = APIRouter(prefix="/v1", dependencies=list(dependencies or []))

    def head() -> dict[str, Any]:
        """The watermark probe: ``{tid, format, version}`` (liveness + lag)."""
        return {
            "tid": store.last_tid,
            "format": FORMAT_MARKER,
            "version": CONTRACT_VERSION,
        }

    def deltas(after: int = Query(0, ge=0)) -> Response:
        """COMMIT-DELTA-v1 frames with ``tid > after``, in strict TID order.

        The body is zero or more ``[>Q length][encode_delta(delta)]`` frames —
        the exact bytes a follower applies through the reference applier. ``after``
        defaults to 0 (a from-genesis bootstrap).
        """
        chunks: list[bytes] = []
        for delta in deltalog.replay(after_tid=after):
            encoded = encode_delta(delta)
            chunks.append(_FRAME.pack(len(encoded)) + encoded)
        return Response(
            content=b"".join(chunks), media_type="application/octet-stream"
        )

    # add_api_route (not the @router.get decorator) so the handlers are
    # *referenced* — the decorator form trips strict reportUnusedFunction.
    router.add_api_route("/head", head, methods=["GET"])
    router.add_api_route("/deltas", deltas, methods=["GET"])
    return router
