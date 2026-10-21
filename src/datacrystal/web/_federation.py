"""``datacrystal[web]`` federation surface — the coordinator's federation endpoints.

:func:`federation_router` mounts the LOCKED ``docs/design/FEDERATION-WIRE-v1.md`` contract
(ROADMAP item 21, epic #146): ``GET /v1/head``, ``GET /v1/deltas`` and ``POST /v1/submit``.
You bring your own authn/z via ``dependencies``, applied to every route.
The coordinator trusts an authenticated client: the follower-side ref guards are conveniences.

"""

from __future__ import annotations

import asyncio
import struct
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

import pydantic
from fastapi import APIRouter, Body, HTTPException, Query, Response

from datacrystal._actors import Principal
from datacrystal._entity import TYPES_BY_NAME, oid_of
from datacrystal._errors import (
    ERROR_CONFLICT,
    ERROR_DANGLING_REF,
    ERROR_SCHEMA_SKEW,
    ConflictError,
    DanglingRefError,
    SchemaSkewError,
    WriteDeniedError,
)
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

# What /v1/submit contributions are stamped as (epic #168 W1): the truthful
# "identity not known" until per-follower principals land (phase 3).
_ANONYMOUS = Principal(uid=0)

__all__ = ["federation_router"]


def federation_router(
    store: Store,
    deltalog: DeltaLog,
    *,
    dependencies: Sequence[Any] | None = None,
) -> APIRouter:
    """Build the federation router (``/v1/head`` + ``/v1/deltas`` + ``/v1/submit``).

    ``store.last_tid`` feeds ``/v1/head``; ``deltalog`` (attached to ``store``, passed explicitly
    because the store exposes no accessor) feeds ``/v1/deltas``.
    ``dependencies`` apply to every route.
    Returns an :class:`fastapi.APIRouter` with prefix ``/v1``.
    """
    router = APIRouter(prefix="/v1", dependencies=list(dependencies or []))

    async def head() -> dict[str, Any]:
        """Return the watermark probe ``{tid, format, version}``.

        ``async def`` so it runs on the owner thread, like :func:`deltas`.
        """
        return {
            "tid": store.last_tid,
            "format": FORMAT_MARKER,
            "version": CONTRACT_VERSION,
        }

    async def deltas(after: int = Query(0, ge=0)) -> Response:
        """Return the COMMIT-DELTA-v2 frames with ``tid > after`` in strict TID order.

        Each frame is ``[>Q length][encode_delta(delta)]``; ``after=0`` is a from-genesis bootstrap.
        ``async def`` is load-bearing: see ``docs/design/FEDERATION-WIRE-v1.md`` (Threading).
        """
        chunks: list[bytes] = []
        for delta in deltalog.replay(after_tid=after):
            encoded = encode_delta(delta)
            chunks.append(_FRAME.pack(len(encoded)) + encoded)
        return Response(
            content=b"".join(chunks), media_type="application/octet-stream"
        )

    async def submit(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Fan a contribution into the single writer and return ``{applied_tid, keys}``.

        One batch is one ``store.submit`` closure and one all-or-nothing ``commit``.
        The fail-closed status codes (422 malformed, 409 skew or OCC mismatch) are in
        FEDERATION-WIRE-v1 §5 and the "Conflict envelope" decision.
        A retry re-merges through the natural-key upsert, so there is no server ledger.
        """
        # The fan-in runs inline only when the store's owner is this event-loop thread;
        # otherwise it would queue with no pump and hang (#155), so fail loud.
        if threading.get_ident() != store._owner:
            raise HTTPException(
                500,
                detail="coordinator store is not owned by the serving thread — open "
                "it on the event-loop thread (uvicorn --workers 1; see "
                "docs/how-to/federation.md)",
            )

        raw_ops = body.get("ops")
        if not isinstance(raw_ops, list):
            raise HTTPException(422, detail="body 'ops' must be a list")
        ops = cast("list[Any]", raw_ops)
        prepared: list[tuple[Any, str, str | None, Any]] = []
        for raw_op in ops:
            if not isinstance(raw_op, dict) or not all(
                k in raw_op for k in ("type", "key", "fields")
            ):
                raise HTTPException(422, detail="each op needs 'type', 'key', 'fields'")
            op = cast("dict[str, Any]", raw_op)
            # Shape-check before any semantic read: set("astring") would mis-fire the
            # schema-skew guard (#154/#155), so a malformed envelope is a 422, not a 409.
            op_type, op_key, op_fields = op["type"], op["key"], op["fields"]
            if not (
                isinstance(op_type, str)
                and isinstance(op_key, str)
                and isinstance(op_fields, dict)
            ):
                raise HTTPException(
                    422, detail="each op needs 'type':str, 'key':str, 'fields':object"
                )
            fields = cast("dict[str, Any]", op_fields)
            info = TYPES_BY_NAME.get(op_type)
            if info is None:
                raise HTTPException(422, detail=f"unknown type {op_type!r}")
            if info.protected:
                # ADR-008 R16 interim: anonymous can neither create protected records nor
                # clear a floor, so refuse pre-flight, before anything is buffered.
                # Per-follower principals lift this.
                raise HTTPException(403, detail={
                    "error": "write-denied", "type": op_type,
                    "message": f"{op_type} is protected=True — federation "
                               "contributions commit as anonymous until "
                               "per-follower principals land (ADR-008 R16)",
                })
            spec = info.spec(op_key)
            if spec is None or not spec.unique:  # #154: the natural key must be Unique
                raise HTTPException(
                    422, detail=f"key {op_key!r} is not a Unique field of {op_type!r}"
                )
            skew = set(fields) - set(info.field_names)
            if skew:  # #154 cid-lineage guard: never silently drop a newer field
                raise HTTPException(
                    409,
                    detail={"error": ERROR_SCHEMA_SKEW, "type": op_type,
                            "unknown_fields": sorted(skew)},
                )
            try:
                dto = entity_model(info.cls, face="create").model_validate(fields)
            except pydantic.ValidationError as exc:
                # A field map that does not form a valid create-face DTO (a missing
                # required field, a wrong/uncoercible type) is a malformed envelope
                # → 422, fail-closed, never an uncaught 500 trace leak (#154/#155).
                raise HTTPException(
                    422,
                    detail={"error": "invalid-fields", "type": op_type,
                            "errors": exc.errors(include_url=False)},
                ) from exc
            prepared.append((info, op_key, op.get("base"), dto))

        def fan_in() -> dict[str, Any]:
            # All-or-nothing: discard() on failure, or a rejected op's earlier upserts stay
            # buffered and the next /v1/submit commit() would make them durable.
            # Identity honesty (epic #168 W1): a third party's write is stamped anonymous
            # (actor=0), never with the operator's ambient principal.
            with store.acting_as(_ANONYMOUS):
                try:
                    survivors: dict[Any, Any] = {}
                    for info, key, base, dto in prepared:
                        value = getattr(dto, key)
                        # OCC (#155): the carried base must equal the current payload hash.
                        # current is None ⇔ key absent; this single check covers all cases —
                        # stale update, insert of an existing key, and update with no base.
                        current = store._payload_digest(info, key, value)
                        if current != base:
                            raise ConflictError(
                                f"{info.typename}.{key}={value!r}: base {base!r} does not "
                                f"match current {current!r} — re-read and retry",
                                key=value,
                                expected_base=base,
                                actual_base=current,
                            )
                        survivor = store.upsert(
                            from_pydantic(dto, info.cls, store=store), key=key
                        )
                        survivors[value] = survivor
                    applied_tid = store.commit()
                except BaseException:
                    store.discard()  # drop the rejected batch's buffered writes (all-or-nothing)
                    raise
            return {
                "applied_tid": applied_tid,
                "keys": {value: oid_of(obj) for value, obj in survivors.items()},
            }

        try:
            return await asyncio.wrap_future(store.submit(fan_in))
        except ConflictError as exc:
            # The LOCKED conflict envelope, so a client can re-read and retry.
            raise HTTPException(
                409,
                detail={
                    "error": ERROR_CONFLICT,
                    "key": exc.key,
                    "expected_base": exc.expected_base,
                    "actual_base": exc.actual_base,
                    "message": str(exc),
                },
            )
        except SchemaSkewError as exc:
            raise HTTPException(
                409, detail={"error": ERROR_SCHEMA_SKEW, "message": str(exc)}
            )
        except DanglingRefError as exc:
            raise HTTPException(
                409, detail={"error": ERROR_DANGLING_REF, "message": str(exc)}
            )
        except WriteDeniedError as exc:
            # Backstop for any denial that slips past the pre-flight (e.g. a
            # future op shape): a clean 403, never an uncaught 500 trace.
            raise HTTPException(
                403, detail={"error": "write-denied", "message": str(exc)}
            )

    # add_api_route (not the @router.get decorator) so the handlers are
    # *referenced* — the decorator form trips strict reportUnusedFunction.
    router.add_api_route("/head", head, methods=["GET"])
    router.add_api_route("/deltas", deltas, methods=["GET"])
    router.add_api_route("/submit", submit, methods=["POST"])
    return router
