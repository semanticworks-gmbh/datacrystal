"""``open_follower`` — a local replica synced from a coordinator (ROADMAP item 21).

A facade over the engine: it replays the coordinator's COMMIT-DELTA-v2 stream
(``GET /v1/deltas``, see ``docs/design/FEDERATION-WIRE-v1.md``) through the
``ReferenceApplier`` into a real local ``Store``, keeping the coordinator's own OIDs and TIDs.
``httpx`` and ``sqlite3`` are imported lazily to keep a bare ``import datacrystal``
inside the ``{msgspec, pyroaring}`` dependency budget.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast

from datacrystal._entity import TypeInfo, is_entity, oid_of, type_info
from datacrystal._errors import (
    ERROR_DANGLING_REF,
    ERROR_SCHEMA_SKEW,
    ConflictError,
    DanglingRefError,
    SchemaSkewError,
)
from datacrystal._ids import CID_BASE, FORMAT_VERSION, OID_BASE, TID_BASE
from datacrystal._lazy import Lazy
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import CommitBatch, StorageBackend, StoredRecord
from datacrystal._store import Store
from datacrystal.contract.applier import (
    DeltaGapError,
    ReferenceApplier,
    check_delta_header,
    decode_delta,
)

if TYPE_CHECKING:
    from pathlib import Path

_FRAME = struct.Struct(">Q")
_FRAME_HEADER_SIZE = _FRAME.size

__all__ = ["HttpClient", "open_follower"]


class _PostResponse(Protocol):
    """The slice of ``httpx.Response`` that a ``/v1/submit`` reply is read through."""

    @property
    def content(self) -> bytes: ...

    @property
    def status_code(self) -> int: ...

    def json(self) -> object: ...


class _GetResponse(Protocol):
    """The slice of ``httpx.Response`` that a ``/v1/deltas`` reply is read through."""

    @property
    def content(self) -> bytes: ...

    def raise_for_status(self) -> object: ...


class _Getter(Protocol):
    def get(self, url: str, /, *, params: dict[str, int]) -> _GetResponse: ...


class _Poster(Protocol):
    def post(self, url: str, /, *, json: dict[str, Any]) -> _PostResponse: ...


class HttpClient(_Getter, _Poster, Protocol):
    """An ``httpx.Client``-compatible object (e.g. a ``TestClient``) that owns the base URL."""


def _iter_frames(blob: bytes) -> Iterator[dict[str, Any]]:
    """Decode the length-prefixed ``>Q`` frames of a ``/v1/deltas`` body."""
    offset = 0
    while offset < len(blob):
        (size,) = _FRAME.unpack_from(blob, offset)
        offset += _FRAME_HEADER_SIZE
        yield decode_delta(blob[offset : offset + size])
        offset += size


def _delta_to_batch(
    delta: dict[str, Any], max_oid: int, max_cid: int, root: int | None
) -> tuple[CommitBatch, int, int, int | None]:
    """Reverse one COMMIT-DELTA-v2 delta into a CommitBatch, advancing the high-water marks.

    The synthesized meta sets the follower's watermark (``next_tid - 1``, invariant 5).
    """
    tid = int(delta["tid"])
    records: list[StoredRecord] = []
    deletes: list[int] = []
    for op in delta["ops"]:
        cid = int(op["cid"])
        oid = int(op["oid"])
        max_cid = max(max_cid, cid)
        if op["op"] == "upsert":
            records.append(StoredRecord(oid=oid, cid=cid, tid=tid, payload=op["payload"]))
            max_oid = max(max_oid, oid)
        else:  # delete (ADR-003 unchecked) — the row is removed atomically
            deletes.append(oid)
    new_types: list[tuple[int, str, list[str]]] = []
    for cid_row, typename, fields in delta["types"]:
        new_types.append((int(cid_row), str(typename), [str(f) for f in fields]))
        max_cid = max(max_cid, int(cid_row))
    if delta["root"] is not None:
        root = int(delta["root"])
    meta = {
        "next_oid": str(max_oid + 1),
        "next_cid": str(max_cid + 1),
        "next_tid": str(tid + 1),
        "root_oid": "" if root is None else str(root),
        "format_version": str(FORMAT_VERSION),
    }
    batch = CommitBatch(
        tid=tid, records=records, new_types=new_types, deletes=deletes, meta=meta
    )
    return batch, max_oid, max_cid, root


def _bootstrap_backend(
    backend: StorageBackend, deltas: Iterable[dict[str, Any]]
) -> ReferenceApplier:
    """Validate ``deltas`` from genesis and persist each advancing one into ``backend``.

    Each delta passes the ``ReferenceApplier`` before the backend is touched,
    so a gap never half-applies.
    """
    applier = ReferenceApplier()
    max_oid, max_cid, root = OID_BASE - 1, CID_BASE - 1, None
    for delta in deltas:
        if not applier.apply(delta):  # False = idempotent skip; gap/format raise
            continue
        batch, max_oid, max_cid, root = _delta_to_batch(delta, max_oid, max_cid, root)
        backend.apply(batch)
    return applier


def _apply_catchup(backend: StorageBackend, deltas: Iterable[dict[str, Any]]) -> int:
    """Apply deltas after the backend's current watermark and return the new watermark.

    No prior state is loaded, so it checks the marker, the exact contract version
    (COMMIT-DELTA-v2 §4.5) and gapless ordering instead of the full ``prior`` check.
    The OID/CID/root bases come from the backend's meta, so a re-applied delta never regresses them.
    """
    meta = backend.boot().meta
    watermark = int(meta.get("next_tid", TID_BASE)) - 1
    max_oid = int(meta.get("next_oid", OID_BASE)) - 1
    max_cid = int(meta.get("next_cid", CID_BASE)) - 1
    root_meta = meta.get("root_oid", "")
    root: int | None = int(root_meta) if root_meta else None
    for delta in deltas:
        check_delta_header(
            delta,
            consumer="follower",
            upgrade="upgrade datacrystal",
            remedy="re-bootstrap this follower from a v2 coordinator",
        )
        tid = int(delta["tid"])
        if tid <= watermark:
            continue  # idempotent skip (apply-twice ≡ apply-once)
        if tid != watermark + 1:
            raise DeltaGapError(
                f"follower at watermark {watermark} got delta {tid}: history is "
                "missing — resync from 0"
            )
        batch, max_oid, max_cid, root = _delta_to_batch(delta, max_oid, max_cid, root)
        backend.apply(batch)
        watermark = tid
    return watermark


def _fetch_deltas(
    url: str, *, after: int, api_key: str | None, client: _Getter | None
) -> bytes:
    """GET the ``/v1/deltas`` body (lazy ``httpx`` unless a ``client`` is given)."""
    if client is not None:
        resp = client.get("/v1/deltas", params={"after": after})
        resp.raise_for_status()
        return resp.content
    try:
        import httpx  # lazy: kept out of a bare ``import datacrystal`` (dep budget)
    except ImportError as exc:  # pragma: no cover — exercised only without httpx
        raise ImportError(
            "open_follower needs an HTTP transport: install httpx "
            "(`pip install httpx`) or pass a client="
        ) from exc

    headers = {"x-api-key": api_key} if api_key else None
    with httpx.Client(base_url=url, headers=headers) as owned:
        resp = owned.get("/v1/deltas", params={"after": after})
        resp.raise_for_status()
        return resp.content


def _post_submit(
    url: str, ops: list[dict[str, Any]], *, api_key: str | None, client: _Poster | None
) -> tuple[int, object]:
    """POST a contribution batch to ``/v1/submit``; return ``(status, json)``."""
    body = {"ops": ops}
    if client is not None:
        resp = client.post("/v1/submit", json=body)
        return resp.status_code, (resp.json() if resp.content else None)
    try:
        import httpx  # lazy: kept out of a bare ``import datacrystal`` (dep budget)
    except ImportError as exc:  # pragma: no cover — exercised only without httpx
        raise ImportError(
            "open_follower contribute needs httpx (`pip install datacrystal[follower]`)"
        ) from exc
    headers = {"x-api-key": api_key} if api_key else None
    with httpx.Client(base_url=url, headers=headers) as owned:
        resp = owned.post("/v1/submit", json=body)
        return resp.status_code, (resp.json() if resp.content else None)


def _holds_entity(value: object) -> bool:
    """Report whether ``value`` is or recursively contains a live ``@entity`` or ``Lazy`` ref."""
    if is_entity(value) or isinstance(value, Lazy):
        return True
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_holds_entity(item) for item in cast("Iterable[Any]", value))
    if isinstance(value, dict):
        return any(_holds_entity(v) for v in cast("dict[Any, Any]", value).values())
    return False


def _container_holds_entity(value: object) -> bool:
    """Report whether ``value`` is a container (list/dict/tuple/set) holding an ``@entity``.

    ``from_pydantic`` rebinds only scalar refs and ``list`` edges, so such a ref
    would land as a bare OID int on the coordinator.
    The caller excludes list edges via ``list_ref_target``; a scalar ref returns ``False``.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_holds_entity(item) for item in cast("Iterable[Any]", value))
    if isinstance(value, dict):
        return any(_holds_entity(v) for v in cast("dict[Any, Any]", value).values())
    return False


def _refs_local_new(value: object, new_oids: set[int]) -> bool:
    """Report whether a serialized create-face field references an OID in ``new_oids``."""
    if isinstance(value, bool):  # bool is an int subclass — never an OID
        return False
    if isinstance(value, int):
        return value in new_oids
    if isinstance(value, list):
        return any(_refs_local_new(item, new_oids) for item in cast("list[Any]", value))
    if isinstance(value, dict):
        items = cast("dict[Any, Any]", value).values()
        return any(_refs_local_new(item, new_oids) for item in items)
    return False


def _natural_key_of(ti: TypeInfo) -> str:
    """Return the ``dc.Unique`` field name that anchors idempotency, or fail loud."""
    key = next((spec.name for spec in ti.specs if spec.unique), None)
    if key is None:
        raise ValueError(
            f"{ti.typename} has no dc.Unique natural key — cannot contribute it "
            "(the natural key is the idempotency anchor; add a dc.Unique field)"
        )
    return key


def _reject_blob_fields(ti: TypeInfo) -> None:
    """Fail loud on a ``dc.Blob`` field: its out-of-line bytes have no ``/v1/submit`` shape."""
    if any(spec.blob for spec in ti.specs):  # §5 cut: out-of-line bytes
        raise NotImplementedError(
            f"{ti.typename} has a dc.Blob field — blob bytes live out-of-line "
            "and have no /v1/submit wire shape (v0); contribute on the "
            "coordinator, or model the bytes inline"
        )


def _scanned_field_names(ti: TypeInfo) -> tuple[str, ...]:
    """Return the field names whose container content must be scanned for entities.

    That is every field except a handled ``list[Lazy[T]]`` / ``list[T]`` edge, which round-trips.
    """
    # lazy: needs pydantic — _reflect is framework-free, but the web package it lives in
    # lazy-imports fastapi/strawberry, so a datacrystal[follower] install suffices.
    from datacrystal.web._reflect import list_ref_target, reflect

    _, descriptors = reflect(ti.cls)
    return tuple(d.name for d in descriptors if list_ref_target(d.core_type) is None)


def _reject_unsupported_containers(
    obj: object, ti: TypeInfo, scan_fields: dict[type, tuple[str, ...]]
) -> None:
    """Fail loud on an ``@entity`` nested in a container the OID-int boundary cannot rebind.

    It would land as a bare int on the coordinator (silent corruption, §5 cut).
    ``scan_fields`` memoizes the names per class: ``reflect`` is uncached and not free.
    """
    names = scan_fields.get(ti.cls)
    if names is None:
        names = _scanned_field_names(ti)
        scan_fields[ti.cls] = names
    for name in names:
        if _container_holds_entity(getattr(obj, name)):
            raise NotImplementedError(
                f"{ti.typename}.{name} holds an @entity inside an unsupported "
                "container shape — only scalar refs (Lazy[T] / @entity) and "
                "list edges (list[Lazy[T]] / list[T]) federate (v0)"
            )


def _submit_op(
    obj: object,
    base: str | None,
    new_oids: set[int],
    scan_fields: dict[type, tuple[str, ...]],
) -> dict[str, Any]:
    """Build one ``/v1/submit`` op from a buffered entity, failing loud on the v0 cuts."""
    from datacrystal.web._pydantic import to_pydantic  # lazy: needs pydantic (to_pydantic)

    ti = type_info(obj)
    key = _natural_key_of(ti)
    _reject_blob_fields(ti)
    _reject_unsupported_containers(obj, ti, scan_fields)
    fields = to_pydantic(obj, face="create").model_dump(mode="json")
    if _refs_local_new(fields, new_oids):
        raise NotImplementedError(
            f"{ti.typename} references a not-yet-committed entity — intra-batch "
            "new→new references are unsupported (v0); contribute the referenced "
            "entity first, then reference it by its coordinator OID"
        )
    return {"type": ti.typename, "key": key, "fields": fields, "base": base}


def _raise_rejection(body: dict[str, Any]) -> NoReturn:
    """Raise the typed local exception for a coordinator 409 body."""
    raw = body.get("detail", body)
    detail = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    message = str(detail.get("message", "rejected"))
    error = detail.get("error")
    if error == ERROR_SCHEMA_SKEW:
        raise SchemaSkewError(message)
    if error == ERROR_DANGLING_REF:  # faithful, not folded into ConflictError
        raise DanglingRefError(message)
    # carry the LOCKED conflict envelope into the typed error so a caller can
    # read exc.actual_base/expected_base to drive its re-read (the fields the
    # coordinator sent — ConflictError's documented contract).
    raise ConflictError(
        message,
        key=detail.get("key"),
        expected_base=detail.get("expected_base"),
        actual_base=detail.get("actual_base"),
    )


def _contribute(
    items: list[tuple[Any, str | None]],
    *,
    url: str,
    api_key: str | None,
    client: _Poster | None,
) -> int | None:
    """Send buffered ``(entity, base)`` items to the coordinator and return its applied TID.

    Returns ``None`` when the batch committed nothing (an idempotent re-send).
    A coordinator 409 becomes the matching typed local exception, to re-read and retry.
    The v0 cuts (blob field, unsupported container ref, new-to-new ref) fail loud before the wire:
    see ``docs/how-to/federation.md`` (The v0 limits) and FEDERATION-WIRE-v1 §5.
    """
    # the local OIDs of the NEW entities in this batch (base is None). A ref to
    # one of these is an intra-batch new→new ref — its follower-local OID is
    # meaningless on the coordinator (it could collide with an unrelated entity),
    # so it MUST fail loud here, never cross the wire (FEDERATION-WIRE-v1 §5 cut).
    new_oids = {
        oid for obj, base in items if base is None and (oid := oid_of(obj)) is not None
    }
    scan_fields: dict[type, tuple[str, ...]] = {}
    ops = [_submit_op(obj, base, new_oids, scan_fields) for obj, base in items]

    status, payload = _post_submit(url, ops, api_key=api_key, client=client)
    body = cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}
    if status == HTTPStatus.CONFLICT:
        _raise_rejection(body)
    if status != HTTPStatus.OK:
        raise RuntimeError(f"/v1/submit returned {status}: {payload!r}")
    # applied_tid is null when the coordinator committed nothing (an idempotent
    # re-send / value-equal no-op); pass that through as None, never int(None).
    applied = body.get("applied_tid")
    return int(applied) if applied is not None else None


def open_follower(
    url: str,
    *,
    api_key: str | None = None,
    path: str | Path | None = None,
    client: HttpClient | None = None,
) -> Store:
    """Open a local replica synced from a coordinator's federation endpoint.

    The function form of :meth:`Store.follower`; it bootstraps by replaying from TID 0.
    ``api_key`` is sent as ``x-api-key``; a ``path`` makes the replica sqlite-backed.
    ``client`` is an ``httpx.Client``-compatible object (e.g. a ``TestClient``) that owns ``url``.
    ``sync`` catches up; ``upsert`` + ``commit`` contribute (needs ``datacrystal[follower]``).
    """
    if path is None:
        backend: StorageBackend = MemoryBackend()
    else:
        # lazy: importing the sqlite backend pulls sqlite3 — keep it out of a
        # bare ``import datacrystal`` (invariant 2, the lazy-sqlite gate).
        from datacrystal._storage.sqlite import SqliteBackend

        backend = SqliteBackend(path)
    backend.boot()
    blob = _fetch_deltas(url, after=0, api_key=api_key, client=client)
    _bootstrap_backend(backend, _iter_frames(blob))
    # same-package engine cooperation: the no-lock backend constructor is the
    # right entry for a replica we have already populated via backend.apply.
    store = Store._from_backend(backend)

    def _sync(after: int) -> int:
        """Fetch + apply the coordinator's deltas after ``after`` (Store.sync)."""
        frames = _iter_frames(
            _fetch_deltas(url, after=after, api_key=api_key, client=client)
        )
        return _apply_catchup(backend, frames)

    def _contribute_fn(items: list[tuple[Any, str | None]]) -> int | None:
        """Serialize + POST the buffered items (Store._contribute calls this)."""
        return _contribute(items, url=url, api_key=api_key, client=client)

    # the follower hooks Store.sync()/commit() call (#151/#153); same-package.
    store._sync_fn = _sync
    store._contribute_fn = _contribute_fn
    return store
