"""In-memory secondary indexes: pyroaring bitmaps + the unique key map.

ROADMAP item 4 (bitmap indexes + Condition AST) and the SDA delta (unique
secondary-key index). v0.1 indexes are **rebuildable derived data**: built
lazily per class from a backend scan at first use, then maintained
incrementally from each commit. They are never persisted and never
participate in the commit transaction.

The KICKOFF plan sketched these as "the second commit-delta consumer";
they deliberately are NOT one (decided at M4): a DeltaConsumer would force
prior-payload reads and delta builds on EVERY commit, while spec §5
promises an unwatched store pays nothing for the pipeline. The index keeps
its own ``oid → last-indexed-values`` memory instead and is folded in
directly at P3. The pipeline's prior-value contract is validated by the
M3 FTS5 spike; the Arrow mirror becomes the first real second consumer.

Un-indexing on update needs the *prior* values; the index keeps its own
``oid → last-indexed-values`` map rather than requiring deltas to carry old
values — the public-contract question that raises is documented in
KICKOFF.md (M3 prior-value spike).

OIDs live above 2**32, hence ``BitMap64``.
"""

from __future__ import annotations

import dataclasses
from bisect import bisect_left, bisect_right, insort
from typing import Any, Callable, Iterable, Iterator, cast

from pyroaring import BitMap64

from datacrystal._conditions import And, Condition, Or, Pred
from datacrystal._entity import TypeInfo
from datacrystal._errors import SchemaMismatchError, UniqueViolationError
from datacrystal._records import RefToken, decode_payload
from datacrystal._storage.protocol import StorageBackend, StoredRecord


@dataclasses.dataclass(frozen=True, slots=True)
class QueryPlan:
    """The deterministic execution plan ``store.explain()`` reports.

    There is no optimizer behind this — exactly three deterministic rules,
    always (``==``/``.in_()`` on Index/Unique/SortedIndex fields answer from
    bitmaps; ``>=``/``>``/``<=``/``<`` on a ``SortedIndex`` field answer from a
    sorted range slice — ADR-004; everything else evaluates as a Python
    residual). No cost model, no plan search; the analytics-tier planner is
    DuckDB over the ``[arrow]`` mirror, never core. ``explain`` exists so the
    cost of a condition is inspectable, not guessed (decided 2026-06-12 with
    query()'s class-form symmetry).
    """

    typename: str
    condition: str | None   # the queried condition; None = bare class (full extent)
    indexed: bool           # True if any predicate answers from bitmaps
    residual: str | None    # the part evaluated in Python; None = fully indexed
    candidates: int         # rows considered: query() hydrates at most this many
    extent: int             # committed extent of the class

    def __str__(self) -> str:
        if self.condition is None:
            return (
                f"{self.typename}: full extent — query() hydrates all "
                f"{self.extent} entities (count()/pluck() decode instead)"
            )
        via = "bitmaps" if self.indexed else "NO index — full extent"
        out = (
            f"{self.typename}: {self.condition}\n"
            f"  candidates via {via}: {self.candidates} of {self.extent}"
        )
        if self.residual is not None:
            out += f"\n  Python residual over candidates: {self.residual}"
        return out


def explain_plan(typename: str, ci: "ClassIndexes",
                 cond: Condition | None) -> QueryPlan:
    """Build the :class:`QueryPlan` for one (class extent, condition) pair —
    shared by the live store and snapshots (same two rules on both)."""
    extent = len(ci.extent)
    if cond is None:
        return QueryPlan(typename, None, False, None, extent, extent)
    bitmap, residual = plan(cond, ci)
    candidates = len(bitmap) if bitmap is not None else extent
    return QueryPlan(
        typename, repr(cond), bitmap is not None,
        repr(residual) if residual is not None else None,
        candidates, extent,
    )


class ClassIndexes:
    """All index structures for one entity class in one store."""

    __slots__ = ("extent", "eq", "unique", "list_fields", "sorted_fields",
                 "sorted_keys", "_building", "_last_values", "_unique_fields")

    def __init__(self, indexed_fields: list[str], unique_fields: list[str],
                 list_fields: list[str] | None = None,
                 sorted_fields: list[str] | None = None) -> None:
        self.extent = BitMap64()
        self.eq: dict[str, dict[Any, BitMap64]] = {f: {} for f in indexed_fields}
        self.unique: dict[str, dict[Any, int]] = {f: {} for f in unique_fields}
        # Multi-valued (inverted) index fields (#13): eq[field] keys are the
        # list's distinct ELEMENTS, not the whole (unhashable) list.
        self.list_fields: frozenset[str] = frozenset(list_fields or ())
        # Sorted index fields (ADR-004 / #18): the same eq[field] postings, plus
        # a per-field sorted list of its distinct non-None keys — bisected for
        # range queries (>=/</between). A sorted field is also an eq field, so
        # point lookups answer from eq[field] unchanged.
        self.sorted_fields: frozenset[str] = frozenset(sorted_fields or ())
        self.sorted_keys: dict[str, list[Any]] = {f: [] for f in (sorted_fields or ())}
        # During the bulk lineage scan, insert() skips the per-key insort (which
        # would be O(K^2) over K distinct keys) — finalize_build() sorts once.
        self._building = False
        self._unique_fields = frozenset(unique_fields)
        self._last_values: dict[int, dict[str, Any]] = {}

    def begin_bulk(self) -> None:
        """Enter bulk-build mode: ``insert()`` defers each sorted field's insort
        (it would be O(K^2) over the lineage) to a single sort in
        :meth:`finalize_build`."""
        self._building = True

    def finalize_build(self) -> None:
        """After a bulk build, derive each sorted field's sorted run from its eq
        keys in one O(K log K) sort (incremental updates after this insort)."""
        for field in self.sorted_fields:
            self.sorted_keys[field] = sorted(
                k for k in self.eq[field] if k is not None
            )
        self._building = False

    def _unindex(self, oid: int, old: dict[str, Any]) -> None:
        for field, value in old.items():
            if field in self.list_fields:
                if value is None:
                    continue
                postings_map = self.eq[field]
                for elem in set(value):
                    posting = postings_map.get(elem)
                    if posting is not None:
                        posting.discard(oid)
                continue
            postings = self.eq[field].get(value)
            if postings is not None:
                postings.discard(oid)
            if field in self._unique_fields and value is not None:
                holder = self.unique[field]
                if holder.get(value) == oid:
                    del holder[value]

    def insert(self, oid: int, values: dict[str, Any]) -> None:
        old = self._last_values.pop(oid, None)
        if old is not None:
            self._unindex(oid, old)
        self.extent.add(oid)
        # Snapshot the indexed values into the last_values memory. A list field
        # carries a mutable PersistentList shared with the live entity, so we
        # copy it: an in-place mutation must not corrupt the un-index that the
        # NEXT update/delete performs against these prior values (invariant 11).
        snapshot: dict[str, Any] = {}
        for field, value in values.items():
            if field in self.list_fields:
                if value is not None:
                    postings_map = self.eq[field]
                    for elem in set(value):
                        postings_map.setdefault(elem, BitMap64()).add(oid)
                snapshot[field] = None if value is None else list(value)
                continue
            postings = self.eq[field].get(value)
            if postings is None:
                postings = BitMap64()
                self.eq[field][value] = postings
                # a genuinely new key on a sorted field enters the sorted run
                # (None never participates in ordering — SQL-NULL-like). During a
                # bulk build the insort is deferred to finalize_build() (O(K^2)→
                # one sort); incremental commits insort directly.
                if (field in self.sorted_fields and value is not None
                        and not self._building):
                    insort(self.sorted_keys[field], value)
            postings.add(oid)
            if field in self._unique_fields and value is not None:
                self.unique[field][value] = oid
            snapshot[field] = value
        self._last_values[oid] = snapshot

    def remove(self, oid: int) -> None:
        """Un-index a committed delete (ADR-003) from the index's own
        ``last_values`` memory — never a store read (invariant 11)."""
        old = self._last_values.pop(oid, None)
        if old is not None:
            self._unindex(oid, old)
        self.extent.discard(oid)

    def seal(self) -> None:
        """Drop the incremental-maintenance memory (oid → last-indexed
        values). For a consumer that will never fold in another commit —
        the frozen snapshot views — that map is pure O(extent) waste."""
        self._last_values.clear()


def build_class_indexes(
    ti: TypeInfo,
    lineage: list[tuple[int, list[str]]],
    scan_type: Callable[[int], Iterable[StoredRecord]],
) -> ClassIndexes:
    """Build one class's indexes by scanning its whole lineage (additive
    schema evolution): per cid, indexed fields map to that shape's
    positions; fields the old shape lacked are filled from the class
    defaults. Each OID appears under exactly one cid (updates rewrite the
    row). ``scan_type`` is the seam: the live store scans its backend, a
    snapshot scans its pinned read view (ADR-002) — same rules, one code
    path."""
    specs = ti.specs
    indexed = [s.name for s in specs if s.indexed or s.unique or s.sorted]
    unique = frozenset(s.name for s in specs if s.unique)
    list_fields = [s.name for s in specs if s.multivalued]
    sorted_fields = [s.name for s in specs if s.sorted]
    ci = ClassIndexes(indexed, list(unique), list_fields, sorted_fields)
    ci.begin_bulk()  # defer the sorted-run insort to one sort at the end
    for cid, persisted in lineage:
        if not indexed:
            for rec in scan_type(cid):
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
        for rec in scan_type(cid):
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
    ci.finalize_build()  # one O(K log K) sort of each sorted run
    return ci


def harvest_ref_oids(values: list[Any]) -> set[int]:
    """Every entity-OID a decoded record references — direct refs and Lazy refs
    alike decode to ``RefToken``, in scalar fields and inside list/dict
    containers. The reverse-reference index's harvest (#20). Iterative (no
    recursion) so a deeply-nested within-record structure can't blow the stack."""
    out: set[int] = set()
    stack: list[Any] = list(values)
    while stack:
        v = stack.pop()
        if isinstance(v, RefToken):
            out.add(v.oid)
        elif isinstance(v, list):
            stack.extend(cast("list[Any]", v))
        elif isinstance(v, dict):
            stack.extend(cast("dict[Any, Any]", v).values())
    return out


class IndexManager:
    """Lazily builds and incrementally maintains per-class indexes (and the
    global reverse-reference index, #20)."""

    def __init__(self, backend: StorageBackend,
                 lineage_for: Callable[[TypeInfo], list[tuple[int, list[str]]]],
                 all_cids: Callable[[], Iterable[int]]) -> None:
        self._backend = backend
        self._lineage_for = lineage_for
        self._all_cids = all_cids
        self._by_cls: dict[type, ClassIndexes] = {}
        # Reverse-reference index (#20): target OID → referrer OIDs, plus each
        # referrer's own outgoing set for incremental diffing. Global (cross
        # class), rebuildable, never persisted (invariant 11). None = not built.
        self._reverse: dict[int, BitMap64] | None = None
        self._reverse_refs: dict[int, BitMap64] = {}

    def ensure(self, ti: TypeInfo) -> ClassIndexes:
        ci = self._by_cls.get(ti.cls)
        if ci is None:
            ci = build_class_indexes(ti, self._lineage_for(ti),
                                     self._backend.scan_type)
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
            indexed = {s.name for s in ti.specs if s.indexed or s.unique or s.sorted}
            ci.insert(oid, {f: v for f, v in values.items() if f in indexed})

    def apply_deletes(self, deletes: list[tuple[int, TypeInfo]]) -> None:
        """P3: drop committed deletions from every already-built index
        (unbuilt indexes scan the post-delete records and never see them)."""
        for oid, ti in deletes:
            ci = self._by_cls.get(ti.cls)
            if ci is not None:
                ci.remove(oid)

    @property
    def reverse_built(self) -> bool:
        return self._reverse is not None

    def ensure_reverse(self) -> dict[int, BitMap64]:
        """Lazily build the global reverse-reference postings by scanning every
        committed record once and harvesting its outgoing refs (#20) — the same
        rebuildable-derived-data contract as the forward indexes (invariant 11:
        never persisted, never in the commit txn). Unlike ``build_class_indexes``
        (per-class, indexed positions only) this is global and decodes every
        field of every record."""
        if self._reverse is not None:
            return self._reverse
        rev: dict[int, BitMap64] = {}
        refs: dict[int, BitMap64] = {}
        for cid in self._all_cids():
            for rec in self._backend.scan_type(cid):
                targets = harvest_ref_oids(decode_payload(rec.payload))
                if targets:
                    refs[rec.oid] = BitMap64(targets)
                    for t in targets:
                        rev.setdefault(t, BitMap64()).add(rec.oid)
        self._reverse = rev
        self._reverse_refs = refs
        return rev

    def apply_reverse(self, ref_entries: list[tuple[int, set[int]]]) -> None:
        """P3: fold a committed batch's outgoing refs into the reverse postings,
        diffing old-vs-new per referrer (like the multi-valued index). Skips when
        the reverse index isn't built — a later ``ensure_reverse`` scans these
        now-committed records (spec §5: an unwatched store pays nothing)."""
        rev = self._reverse
        if rev is None:
            return
        for referrer, targets in ref_entries:
            old = self._reverse_refs.get(referrer)
            if old is not None:
                for t in old:
                    posting = rev.get(t)
                    if posting is not None:
                        posting.discard(referrer)
            if targets:
                self._reverse_refs[referrer] = BitMap64(targets)
                for t in targets:
                    rev.setdefault(t, BitMap64()).add(referrer)
            else:
                self._reverse_refs.pop(referrer, None)

    def remove_reverse(self, deleted_oids: list[int]) -> None:
        """P3: a committed delete (ADR-003) drops the OID as a *referrer* — its
        outgoing edges vanish from the postings — but KEEPS it as a *target*:
        entities still pointing at the dead OID are now dangling, and
        ``incoming(dead)`` names exactly them (the checked-delete enumeration
        ADR-003 waited for). Skips when the reverse index isn't built."""
        rev = self._reverse
        if rev is None:
            return
        for d in deleted_oids:
            old = self._reverse_refs.pop(d, None)
            if old is not None:
                for t in old:
                    posting = rev.get(t)
                    if posting is not None:
                        posting.discard(d)


def _range_slice(ci: ClassIndexes, field: str, op: str, value: Any) -> BitMap64:
    """The OIDs whose SortedIndex ``field`` key satisfies ``op value`` — bisect
    the sorted run for the matching key interval, union those eq postings
    (ADR-004 / #18). None is never in the run (SQL-NULL-like ordering), and a
    None bound matches nothing — mirroring :meth:`Pred.evaluate`."""
    acc = BitMap64()
    if value is None:
        return acc
    keys = ci.sorted_keys[field]
    postings = ci.eq[field]
    if op == ">=":
        lo, hi = bisect_left(keys, value), len(keys)
    elif op == ">":
        lo, hi = bisect_right(keys, value), len(keys)
    elif op == "<=":
        lo, hi = 0, bisect_right(keys, value)
    else:  # "<"
        lo, hi = 0, bisect_left(keys, value)
    for key in keys[lo:hi]:
        acc |= postings[key]
    return acc


def plan(cond: Condition, ci: ClassIndexes) -> tuple[BitMap64 | None, Condition | None]:
    """Split a condition into (bitmap candidates, residual predicate).

    ``==`` and ``in_`` on indexed fields resolve to bitmaps; AND combines
    bitmaps and residuals independently; OR uses bitmaps only when every
    branch is fully indexed; everything else stays a residual evaluated on
    hydrated candidates.
    """
    if isinstance(cond, Pred):
        if cond.field in ci.eq:
            if cond.op in (">=", ">", "<=", "<") and cond.field in ci.sorted_fields:
                # ADR-004 (#18): the THIRD rule — an ordering comparison on a
                # SortedIndex field answers from the sorted run as a range slice
                # (a `between` is an And of two of these, composed below). Still
                # deterministic and rule-based: no cost model, no plan search.
                return _range_slice(ci, cond.field, cond.op, cond.value), None
            if cond.field in ci.list_fields:
                # Multi-valued (inverted) index (#13): eq[field] keys are the
                # list's elements, so `.contains(x)` is exact element membership
                # — an O(1) posting lookup, no record reads, no residual. ==/in/
                # startswith over a whole list can't be answered from an element
                # index → residual (evaluate() compares the actual list).
                if cond.op == "contains":
                    postings = ci.eq[cond.field].get(cond.value)
                    return (postings.copy() if postings is not None
                            else BitMap64()), None
                return None, cond
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
        branch_bitmaps: list[BitMap64] = []
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
