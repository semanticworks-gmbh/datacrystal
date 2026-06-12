"""In-memory secondary indexes: pyroaring bitmaps + the unique key map.

ROADMAP item 4 (bitmap indexes + Condition AST) and the SDA delta (unique
secondary-key index). v0.1 indexes are **rebuildable derived data**: built
lazily per class from a backend scan at first use, then maintained
incrementally from each commit (the in-process forerunner of the public
commit-delta consumer they become at M3/M4). They are never persisted and
never participate in the commit transaction.

Un-indexing on update needs the *prior* values; the index keeps its own
``oid → last-indexed-values`` map rather than requiring deltas to carry old
values — the public-contract question that raises is documented in
KICKOFF.md (M3 prior-value spike).

OIDs live above 2**32, hence ``BitMap64``.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from pyroaring import BitMap64

from datacrystal._conditions import And, Condition, Or, Pred
from datacrystal._entity import TypeInfo
from datacrystal._errors import SchemaMismatchError, UniqueViolationError
from datacrystal._records import decode_payload
from datacrystal._storage.protocol import StorageBackend


class ClassIndexes:
    """All index structures for one entity class in one store."""

    __slots__ = ("extent", "eq", "unique", "_last_values", "_unique_fields")

    def __init__(self, indexed_fields: list[str], unique_fields: list[str]) -> None:
        self.extent = BitMap64()
        self.eq: dict[str, dict[Any, BitMap64]] = {f: {} for f in indexed_fields}
        self.unique: dict[str, dict[Any, int]] = {f: {} for f in unique_fields}
        self._unique_fields = frozenset(unique_fields)
        self._last_values: dict[int, dict[str, Any]] = {}

    def insert(self, oid: int, values: dict[str, Any]) -> None:
        old = self._last_values.pop(oid, None)
        if old is not None:
            for field, value in old.items():
                postings = self.eq[field].get(value)
                if postings is not None:
                    postings.discard(oid)
                if field in self._unique_fields and value is not None:
                    holder = self.unique[field]
                    if holder.get(value) == oid:
                        del holder[value]
        self.extent.add(oid)
        for field, value in values.items():
            self.eq[field].setdefault(value, BitMap64()).add(oid)
            if field in self._unique_fields and value is not None:
                self.unique[field][value] = oid
        self._last_values[oid] = values

    def remove(self, oid: int) -> None:
        """Un-index a committed delete (ADR-003) from the index's own
        ``last_values`` memory — never a store read (invariant 11)."""
        old = self._last_values.pop(oid, None)
        if old is not None:
            for field, value in old.items():
                postings = self.eq[field].get(value)
                if postings is not None:
                    postings.discard(oid)
                if field in self._unique_fields and value is not None:
                    holder = self.unique[field]
                    if holder.get(value) == oid:
                        del holder[value]
        self.extent.discard(oid)


class IndexManager:
    """Lazily builds and incrementally maintains per-class indexes."""

    def __init__(self, backend: StorageBackend,
                 lineage_for: Callable[[TypeInfo], list[tuple[int, list[str]]]]) -> None:
        self._backend = backend
        self._lineage_for = lineage_for
        self._by_cls: dict[type, ClassIndexes] = {}

    def ensure(self, ti: TypeInfo) -> ClassIndexes:
        ci = self._by_cls.get(ti.cls)
        if ci is not None:
            return ci
        specs = ti.specs
        indexed = [s.name for s in specs if s.indexed or s.unique]
        unique = frozenset(s.name for s in specs if s.unique)
        ci = ClassIndexes(indexed, list(unique))
        # The build scans the type's whole lineage (additive schema
        # evolution): per cid, indexed fields map to that shape's positions;
        # fields the old shape lacked are filled from the class defaults.
        # Each OID appears under exactly one cid (updates rewrite the row).
        for cid, persisted in self._lineage_for(ti):
            if not indexed:
                for rec in self._backend.scan_type(cid):
                    ci.extent.add(rec.oid)
                continue
            position = {n: persisted.index(n) for n in indexed if n in persisted}
            fill: dict[str, Any] = {}
            colliding: str | None = None
            for name in indexed:
                if name in position:
                    continue
                factory = ti.defaults.get(name)
                if factory is None:
                    raise SchemaMismatchError(
                        f"{ti.typename}.{name} does not exist in records "
                        f"persisted with fields {persisted} and has no default "
                        "— give the new field a default value to enable "
                        "additive schema evolution"
                    )
                fill[name] = factory()
                if name in unique and fill[name] is not None:
                    colliding = name  # only an error if old records exist
            for rec in self._backend.scan_type(cid):
                if colliding is not None:
                    raise SchemaMismatchError(
                        f"{ti.typename}.{colliding}: a Unique field added by "
                        "schema evolution must default to None — a shared "
                        "non-None default would make every old record collide"
                    )
                values = decode_payload(rec.payload)
                entry = {name: values[pos] for name, pos in position.items()}
                entry.update(fill)
                ci.insert(rec.oid, entry)
        self._by_cls[ti.cls] = ci
        return ci

    def check_unique(self, entries: list[tuple[int, TypeInfo, dict[str, Any]]],
                     deleted: frozenset[int] | set[int] = frozenset()) -> None:
        """P1 validation: no commit may create a duplicate unique-key value.

        ``None`` values are exempt (SQL-style: NULL never collides). A value
        currently held by an OID in ``deleted`` (buffered deletions in the
        same commit) is free to claim — ADR-003 unique-key reuse.
        """
        seen: dict[tuple[type, str, Any], int] = {}
        for oid, ti, values in entries:
            unique_fields = [s.name for s in ti.specs if s.unique]
            if not unique_fields:
                continue
            ci = self.ensure(ti)
            for field in unique_fields:
                value = values.get(field)
                if value is None:
                    continue
                existing = ci.unique[field].get(value)
                if existing is not None and existing in deleted:
                    existing = None
                if existing is not None and existing != oid:
                    raise UniqueViolationError(
                        f"{ti.cls.__name__}.{field}={value!r} already belongs to "
                        f"another entity (oid {existing})"
                    )
                key = (ti.cls, field, value)
                prior = seen.get(key)
                if prior is not None and prior != oid:
                    raise UniqueViolationError(
                        f"two entities in this commit both set "
                        f"{ti.cls.__name__}.{field}={value!r}"
                    )
                seen[key] = oid

    def apply(self, entries: list[tuple[int, TypeInfo, dict[str, Any]]]) -> None:
        """P3: fold a committed batch into every already-built index."""
        for oid, ti, values in entries:
            ci = self._by_cls.get(ti.cls)
            if ci is None:
                continue  # not built yet; a later build scans these records
            indexed = {s.name for s in ti.specs if s.indexed or s.unique}
            ci.insert(oid, {f: v for f, v in values.items() if f in indexed})

    def apply_deletes(self, deletes: list[tuple[int, TypeInfo]]) -> None:
        """P3: drop committed deletions from every already-built index
        (unbuilt indexes scan the post-delete records and never see them)."""
        for oid, ti in deletes:
            ci = self._by_cls.get(ti.cls)
            if ci is not None:
                ci.remove(oid)


def plan(cond: Condition, ci: ClassIndexes) -> tuple[BitMap64 | None, Condition | None]:
    """Split a condition into (bitmap candidates, residual predicate).

    ``==`` and ``in_`` on indexed fields resolve to bitmaps; AND combines
    bitmaps and residuals independently; OR uses bitmaps only when every
    branch is fully indexed; everything else stays a residual evaluated on
    hydrated candidates.
    """
    if isinstance(cond, Pred):
        if cond.field in ci.eq:
            if cond.op == "==":
                postings = ci.eq[cond.field].get(cond.value)
                return (postings.copy() if postings is not None else BitMap64()), None
            if cond.op == "in":
                acc = BitMap64()
                for value in cond.value:
                    postings = ci.eq[cond.field].get(value)
                    if postings is not None:
                        acc |= postings
                return acc, None
            if cond.op in ("contains", "startswith"):
                # KICKOFF M4: string matching on an indexed field iterates
                # the index's DISTINCT keys and ORs the matching postings —
                # O(distinct values), never a record load.
                needle = cond.value
                acc = BitMap64()
                for key, postings in ci.eq[cond.field].items():
                    if not isinstance(key, str):
                        continue
                    if (needle in key if cond.op == "contains"
                            else key.startswith(needle)):
                        acc |= postings
                return acc, None
        return None, cond
    if isinstance(cond, And):
        bitmap: BitMap64 | None = None
        residuals: list[Condition] = []
        for part in cond.parts:
            sub_bm, sub_resid = plan(part, ci)
            if sub_bm is not None:
                bitmap = sub_bm if bitmap is None else bitmap & sub_bm
            if sub_resid is not None:
                residuals.append(sub_resid)
        residual: Condition | None
        if not residuals:
            residual = None
        elif len(residuals) == 1:
            residual = residuals[0]
        else:
            residual = And(tuple(residuals))
        return bitmap, residual
    if isinstance(cond, Or):
        branch_bitmaps = []
        for part in cond.parts:
            sub_bm, sub_resid = plan(part, ci)
            if sub_bm is None or sub_resid is not None:
                return None, cond
            branch_bitmaps.append(sub_bm)
        acc = BitMap64()
        for bm in branch_bitmaps:
            acc |= bm
        return acc, None
    return None, cond


def iter_oids(bm: BitMap64) -> Iterator[int]:
    return iter(bm)
