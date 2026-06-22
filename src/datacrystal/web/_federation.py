"""``datacrystal[web]`` federation surface — the coordinator's federation endpoints.

:func:`federation_router` mounts the LOCKED
[FEDERATION-WIRE-v1](../../../docs/design/FEDERATION-WIRE-v1.md) contract
(ROADMAP item 21, epic #146): ``GET /v1/head`` (the watermark probe a follower
polls), ``GET /v1/deltas?after=<tid>`` (COMMIT-DELTA-v1 frames — byte-for-byte
the length-prefixed frame the :class:`~datacrystal.deltalog.DeltaLog` already
writes, re-encoded from :meth:`~datacrystal.deltalog.DeltaLog.replay`), and
``POST /v1/submit`` (contribute: a follower's writes fanned into the single writer
via ``store.submit``, ADR-001). The fail-closed guards on ``/v1/submit``
(cid-lineage, OCC, idempotency) are added by their own stories.

You bring your own authn/z: pass FastAPI ``dependencies=[Depends(...)]`` and they
apply to every federation route — nothing here is exempt from your auth (the same
seam as the rest of the extra).

``fastapi`` is imported only inside this submodule, so a bare ``import
datacrystal`` never pulls it (the dep-isolation fitness gate).
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, HTTPException, Query, Response

from datacrystal._entity import TYPES_BY_NAME, oid_of
from datacrystal.contract.applier import CONTRACT_VERSION, FORMAT_MARKER, encode_delta
from datacrystal.web._pydantic import entity_model, from_pydantic

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
    """Build the federation router (``/v1/head`` + ``/v1/deltas`` + ``/v1/submit``).

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

    async def submit(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Fan a contribution into the single writer (FEDERATION-WIRE-v1 Shape 3).

        Body is ``{idem?, ops: [{type, key, fields, base?}]}``. Each op rebuilds a
        live ``@entity`` from its create-face ``fields`` and is ``upsert``-ed by
        its natural ``key``; the whole batch is one ``store.submit`` closure → one
        ``commit`` (all-or-nothing). Returns ``{applied_tid, keys}`` mapping each
        natural-key value to its OID (newly minted or the merged survivor's).

        The fail-closed guards (cid-lineage ``SchemaSkewError``, the OCC ``base``
        check, idempotency) are added by their own stories; ``base``/``idem`` are
        accepted here but not yet enforced.
        """
        prepared: list[tuple[type, str, Any]] = []
        for op in body.get("ops", []):
            info = TYPES_BY_NAME.get(op["type"])
            if info is None:  # unknown shape — cid-lineage guard hardens this (#154)
                raise HTTPException(422, detail=f"unknown type {op['type']!r}")
            dto = entity_model(info.cls, face="create").model_validate(op["fields"])
            prepared.append((info.cls, op["key"], dto))

        def fan_in() -> dict[str, Any]:
            survivors: dict[Any, Any] = {}
            for cls, key, dto in prepared:
                survivor = store.upsert(from_pydantic(dto, cls, store=store), key=key)
                survivors[getattr(survivor, key)] = survivor
            applied_tid = store.commit()
            return {
                "applied_tid": applied_tid,
                "keys": {value: oid_of(obj) for value, obj in survivors.items()},
            }

        return await asyncio.wrap_future(store.submit(fan_in))

    # add_api_route (not the @router.get decorator) so the handlers are
    # *referenced* — the decorator form trips strict reportUnusedFunction.
    router.add_api_route("/head", head, methods=["GET"])
    router.add_api_route("/deltas", deltas, methods=["GET"])
    router.add_api_route("/submit", submit, methods=["POST"])
    return router
