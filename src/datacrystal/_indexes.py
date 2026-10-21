"""In-memory secondary indexes: pyroaring bitmaps + the unique key map.

Indexes are rebuildable derived data (ROADMAP item 4): built lazily per class, then folded in at
P3 of each commit; they may be cached on disk (ADR-005) but never join the commit transaction.
Not a commit-delta consumer (decided at M4): each keeps its own ``oid → last-indexed-values``
memory (COMMIT-DELTA-v2.md, section 8). OIDs live above 2**32, hence ``BitMap64``.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import decimal as _decimal
from bisect import bisect_left, bisect_right, insort
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any, cast

from pyroaring import BitMap64

from datacrystal._conditions import And, Condition, Or, Pred
from datacrystal._entity import TypeInfo
from datacrystal._errors import (
    MixedTemporalIndexError,
    SchemaMismatchError,
    UniqueViolationError,
)
from datacrystal._permissions import PERM_LEGACY_FILLS, VIEWER, WORLD, is_root
from datacrystal._records import RefToken, decode_payload
from datacrystal._storage.protocol import StorageBackend, StoredRecord

if TYPE_CHECKING:
    from _typeshed import SupportsRichComparison

    from datacrystal._actors import Principal


def _guard_temporal_comparable(field: str, new_key: object, existing_key: object) -> None:
    """Reject a SortedIndex datetime field that mixes naive and aware values (#106, ADR-004 §4).

    A mixed run would raise a bare ``TypeError`` deep in ``bisect``; this names it at the door.
    Only ``datetime`` carries an offset, so other key types pass.
    The incoming key is compared with one existing key, since a run shares one convention.
    """
    if (isinstance(new_key, _dt.datetime) and isinstance(existing_key, _dt.datetime)
            and (new_key.tzinfo is None) != (existing_key.tzinfo is None)):
        raise MixedTemporalIndexError(
            f"SortedIndex field {field!r} mixes timezone-naive and timezone-aware "
            "datetimes — they are not mutually orderable; store every value with "
            "the same convention (aware, e.g. datetime.now(timezone.utc), is "
            "recommended)"
        )


def _sorted_run(field: str, keys: Iterable[Any]) -> list[Any]:
    """Sort a sorted field's distinct non-None keys into its run (#106, ADR-004 §4).

    A naive-vs-aware datetime mix becomes a :class:`MixedTemporalIndexError`, not a bare
    ``TypeError``. Used where the whole run is sorted at once: ``finalize_build``, cache ``load``.
    """
    try:
        return sorted(keys)
    except TypeError as exc:
        raise MixedTemporalIndexError(
            f"SortedIndex field {field!r} mixes timezone-naive and timezone-aware "
            "datetimes — they are not mutually orderable; store every value with "
            "the same convention (aware, e.g. datetime.now(timezone.utc), is "
            "recommended)"
        ) from exc
    except _decimal.InvalidOperation as exc:
        raise ValueError(
            f"{field} is a SortedIndex field and cannot hold Decimal NaN: NaN is not "
            "orderable (use None for 'no value')"
        ) from exc


@dataclasses.dataclass(frozen=True, slots=True)
class QueryPlan:
    """The deterministic execution plan ``store.explain()`` reports.

    No optimizer sits behind it, only three rules: ``==``/``.in_()`` on an indexed field answer
    from bitmaps, ``>=``/``>``/``<=``/``<`` on a ``SortedIndex`` field from a sorted range slice
    (ADR-004), and everything else is a Python residual.
    The analytics-tier planner is DuckDB over the ``[arrow]`` mirror, never core.
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


def explain_plan(typename: str, ci: ClassIndexes,
                 cond: Condition | None,
                 readable: BitMap64 | None = None) -> QueryPlan:
    """Build the :class:`QueryPlan` for one (class extent, condition) pair.

    The live store and snapshots share it.
    ``readable`` (ADR-008 R12/D8) post-filters only the reported ``candidates`` and ``extent``
    counts; ``None`` means no filter (root, an unprotected class, or a snapshot caller).
    The planner itself, ``condition``, ``residual`` and ``indexed`` are untouched.
    """
    extent_bm = ci.extent if readable is None else (ci.extent & readable)
    extent = len(extent_bm)
    if cond is None:
        return QueryPlan(typename, None, False, None, extent, extent)
    bitmap, residual = plan(cond, ci)
    cand_bm = bitmap if bitmap is not None else ci.extent
    candidates = len(cand_bm) if readable is None else len(cand_bm & readable)
    return QueryPlan(
        typename, repr(cond), bitmap is not None,
        repr(residual) if residual is not None else None,
        candidates, extent,
    )


class ClassIndexes:
    """All index structures for one entity class in one store."""

    __slots__ = (
        "_building",
        "_last_values",
        "_needs_lv_rebuild",
        "_unique_fields",
        "eq",
        "extent",
        "list_fields",
        "sorted_fields",
        "sorted_keys",
        "unique",
    )

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
        # A cache load() defers the O(corpus) _last_values reconstruction to the
        # first write (#12 Design A): a read-only reopen never pays it. A from-
        # records build maintains the map incrementally, so this stays False.
        self._needs_lv_rebuild = False

    def begin_bulk(self) -> None:
        """Enter bulk-build mode: ``insert()`` defers each sorted field's insort
        (it would be O(K^2) over the lineage) to a single sort in
        :meth:`finalize_build`.
        """
        self._building = True

    def finalize_build(self) -> None:
        """After a bulk build, derive each sorted field's sorted run from its eq
        keys in one O(K log K) sort (incremental updates after this insort).
        """
        for field in self.sorted_fields:
            self.sorted_keys[field] = _sorted_run(
                field, (k for k in self.eq[field] if k is not None)
            )
        self._building = False

    def _rebuild_last_values(self) -> None:
        """Reconstruct the per-oid un-index memory from the already-loaded postings
        + unique map — deferred from a cache ``load()`` to the first write (#12), so
        a read-only reopen pays nothing. Rebuilds from the index (not the records),
        so additive schema evolution is already baked in and no fill logic is
        needed. From-records builds maintain the map incrementally and never call
        this.
        """
        last: dict[int, dict[str, Any]] = {}
        for field, col in self.eq.items():
            is_list = field in self.list_fields
            for key, bm in col.items():
                for oid in bm:
                    entry = last.setdefault(oid, {})
                    if is_list:
                        if key is not None:
                            entry.setdefault(field, []).append(key)
                    else:
                        entry[field] = key
        for field in self._unique_fields:
            if field not in self.eq:  # a pure-Unique field: its memory is the map
                for value, oid in self.unique[field].items():
                    last.setdefault(oid, {})[field] = value
        self._last_values = last
        self._needs_lv_rebuild = False

    def committed_labels(self, oid: int) -> tuple[int, list[int], int] | None:
        """Return the committed ``(owner, groups, read_floor)`` of ``oid``, or None if absent.

        Read O(1) from the index's last-indexed-values memory, no backend I/O.
        Committed labels, never staged ones, are the read-enforcement truth (ADR-008 W3 D1);
        callers treat None as absent (fail closed).
        It triggers the deferred (#12) rebuild after a cache load, like the first write.
        """
        if self._needs_lv_rebuild:
            self._rebuild_last_values()
        row = self._last_values.get(oid)
        if row is None:
            return None
        groups = row.get("_dc_groups")
        return row["_dc_owner"], (list(groups) if groups else []), row["_dc_read_floor"]

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
            eq_col = self.eq.get(field)  # None for a pure-Unique field (#12: no eq postings)
            if eq_col is not None:
                postings = eq_col.get(value)
                if postings is not None:
                    postings.discard(oid)
            if field in self._unique_fields and value is not None:
                holder = self.unique[field]
                if holder.get(value) == oid:
                    del holder[value]

    def insert(self, oid: int, values: dict[str, Any]) -> None:
        if self._needs_lv_rebuild:  # first write after a cache load (#12)
            self._rebuild_last_values()
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
                self._index_list_field(oid, field, value)
                snapshot[field] = None if value is None else list(value)
            else:
                self._index_scalar_field(oid, field, value)
                snapshot[field] = value
        self._last_values[oid] = snapshot

    def _index_list_field(self, oid: int, field: str, value: list[Any] | None) -> None:
        if value is None:
            return
        postings_map = self.eq[field]
        for elem in set(value):
            postings_map.setdefault(elem, BitMap64()).add(oid)

    def _index_scalar_field(
        self, oid: int, field: str, value: SupportsRichComparison | None
    ) -> None:
        # A pure-Unique field (Unique, not also Index/SortedIndex) carries NO
        # eq postings (#12): its ==/in_ are answered from the unique map, so
        # the per-key single-element bitmaps were dead weight. Route it to the
        # unique map only; Index/SortedIndex (incl. Unique+Index) keep eq.
        eq_col = self.eq.get(field)
        if eq_col is not None:
            postings = eq_col.get(value)
            if postings is None:
                self._enter_sorted_run(field, value)
                postings = BitMap64()
                eq_col[value] = postings
            postings.add(oid)
        if field in self._unique_fields and value is not None:
            self.unique[field][value] = oid

    def _enter_sorted_run(self, field: str, value: SupportsRichComparison | None) -> None:
        # a new key on a sorted field enters the sorted run (None never does);
        # a bulk build defers the insort to finalize_build() (O(K^2) → one sort).
        # A datetime field mixing naive and aware values is rejected before
        # insort's comparison fails (#106 / ADR-004 §4).
        if field not in self.sorted_fields or value is None or self._building:
            return
        run = self.sorted_keys[field]
        if run:
            _guard_temporal_comparable(field, value, run[0])
        insort(run, value)

    def remove(self, oid: int) -> None:
        """Un-index a committed delete (ADR-003) from the index's own
        ``last_values`` memory — never a store read (invariant 11).
        """
        if self._needs_lv_rebuild:  # first write after a cache load (#12)
            self._rebuild_last_values()
        old = self._last_values.pop(oid, None)
        if old is not None:
            self._unindex(oid, old)
        self.extent.discard(oid)

    def seal(self) -> None:
        """Drop the incremental-maintenance memory (oid → last-indexed
        values). For a consumer that will never fold in another commit —
        the frozen snapshot views — that map is pure O(extent) waste.
        """
        self._last_values.clear()
        self._needs_lv_rebuild = False  # a sealed view never folds a commit

    def dump(self) -> dict[str, Any]:
        """Serialize to a msgpack-able structure for the index cache (ADR-005 /
        #12): bitmaps as ``BitMap64`` bytes; keys live in lists (never as map
        keys) so any scalar key type round-trips. A pure-Unique field carries NO
        eq postings (#12) — only the flat ``unique`` value→oid map. ``sorted_keys``
        reconstruct from the postings on load; the ``_last_values`` memory is not
        stored and is rebuilt lazily on the first write after a load.
        """
        return {
            "extent": self.extent.serialize(),
            "eq": [[f, [[k, bm.serialize()] for k, bm in posts.items()]]
                   for f, posts in self.eq.items()],
            "unique": [[f, list(holders.items())] for f, holders in self.unique.items()],
            "list_fields": sorted(self.list_fields),
            "sorted_fields": sorted(self.sorted_fields),
        }

    @classmethod
    def load(cls, blob: dict[str, Any], ti: TypeInfo) -> ClassIndexes | None:
        """Rebuild from a cached ``dump()`` — but ONLY if the cached index
        surface still matches the live class's markers (else ``None`` → the
        caller rebuilds from records; the cache is never authoritative). The
        sorted runs reconstruct from the postings here; the ``_last_values``
        un-index memory is deferred to the first write (a read-only reopen never
        pays for it), so a loaded index still supports further commits.
        """
        eq_names = [f for f, _ in blob["eq"]]
        unique = [f for f, _ in blob["unique"]]
        list_fields = list(blob["list_fields"])
        sorted_fields = list(blob["sorted_fields"])
        # eq-membership is Index/SortedIndex only (#12); a pure-Unique field is in
        # `unique`, never `eq` — so the marker-check compares the two separately.
        if (sorted(eq_names) != sorted(_eq_index_fields(ti))
                or sorted(unique) != sorted(s.name for s in ti.specs if s.unique)
                or sorted(list_fields) != sorted(s.name for s in ti.specs if s.multivalued)
                or sorted(sorted_fields) != sorted(s.name for s in ti.specs if s.sorted)):
            return None  # the code's index markers changed → rebuild
        ci = cls(eq_names, unique, list_fields, sorted_fields)
        ci.extent = BitMap64.deserialize(blob["extent"])
        for field, posts in blob["eq"]:
            col = ci.eq[field]
            for key, bm_bytes in posts:
                col[key] = BitMap64.deserialize(bm_bytes)
        for field, items in blob["unique"]:
            ci.unique[field] = dict(items)
        for field in ci.sorted_fields:
            ci.sorted_keys[field] = _sorted_run(
                field, (k for k in ci.eq[field] if k is not None)
            )
        # Defer the O(corpus) _last_values reconstruction to the first write (#12
        # Design A): a read-only reopen never pays it; the first insert()/remove()
        # rebuilds it from these postings + the unique map.
        ci._needs_lv_rebuild = True
        return ci


def _eq_index_fields(ti: TypeInfo) -> list[str]:
    """Fields that carry eq (bitmap) postings: Index and SortedIndex — NOT a
    pure-Unique field, whose ``==``/``in_`` answer from the value→oid unique map
    (#12). The SINGLE source of eq-membership; build, the cache marker-check, and
    plan()'s unique fallback all agree on it so a Unique-only field can't be in
    ``eq`` on one path and absent on another.
    """
    return [s.name for s in ti.specs if s.indexed or s.sorted]


def _maintained_fields(ti: TypeInfo) -> list[str]:
    """Every field the index touches — eq fields ∪ unique fields (#12). A pure-
    Unique field is maintained (it has a unique map and an un-index memory) but
    carries no eq postings.
    """
    return [s.name for s in ti.specs if s.indexed or s.sorted or s.unique]


def _schema_evolution_fill(
    ti: TypeInfo, persisted: list[str], maintained: list[str], unique: frozenset[str]
) -> tuple[dict[str, Any], str | None]:
    """Fill the maintained fields an old record shape lacks; also name a colliding Unique one.

    The colliding field is a Unique one that defaults to non-None: an error only if old
    records exist, so the caller raises it on the first scanned record.
    """
    fill: dict[str, Any] = {}
    colliding: str | None = None
    for name in maintained:
        if name in persisted:
            continue
        if ti.protected and name in PERM_LEGACY_FILLS:
            # R7 legacy fill (ADR-008): index maintained _dc_ columns of
            # pre-protection records with the legacy values (the sorted
            # _dc_read_floor run would otherwise be poisoned for legacy
            # rows). Same shared constant as the two decode sites.
            fill[name] = PERM_LEGACY_FILLS[name]()
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
    return fill, colliding


def build_class_indexes(
    ti: TypeInfo,
    lineage: list[tuple[int, list[str]]],
    scan_type: Callable[[int], Iterable[StoredRecord]],
) -> ClassIndexes:
    """Build one class's indexes by scanning its whole lineage (additive schema evolution).

    Per cid, indexed fields map to that shape's positions; fields the old shape lacked take
    the class defaults. ``scan_type`` is the seam: the live store scans its backend, a snapshot
    its pinned read view (ADR-002).
    """
    specs = ti.specs
    eq_fields = _eq_index_fields(ti)
    unique = frozenset(s.name for s in specs if s.unique)
    maintained = _maintained_fields(ti)  # eq fields + the pure-Unique fields
    list_fields = [s.name for s in specs if s.multivalued]
    sorted_fields = [s.name for s in specs if s.sorted]
    ci = ClassIndexes(eq_fields, list(unique), list_fields, sorted_fields)
    ci.begin_bulk()  # defer the sorted-run insort to one sort at the end
    for cid, persisted in lineage:
        if not maintained:
            for rec in scan_type(cid):
                ci.extent.add(rec.oid)
            continue
        position = {n: persisted.index(n) for n in maintained if n in persisted}
        fill, colliding = _schema_evolution_fill(ti, persisted, maintained, unique)
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
    recursion) so a deeply-nested within-record structure can't blow the stack.
    """
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
    global reverse-reference index, #20).
    """

    def __init__(self, backend: StorageBackend,
                 lineage_for: Callable[[TypeInfo], list[tuple[int, list[str]]]],
                 all_cids: Callable[[], Iterable[int]],
                 cache_blobs: dict[str, Any] | None = None,
                 reverse_blob: dict[str, Any] | None = None) -> None:
        self._backend = backend
        self._lineage_for = lineage_for
        self._all_cids = all_cids
        self._by_cls: dict[type, ClassIndexes] = {}
        # Index cache (ADR-005 / #12): per-typename blobs loaded from the sidecar
        # at boot IF its watermark matched the store's; consumed (lazily, per
        # class) by ensure(). None = no usable cache → build from records.
        self._cache_blobs = cache_blobs
        # Reverse-reference index (#20): target OID → referrer OIDs, plus each
        # referrer's own outgoing set for incremental diffing. Global (cross
        # class), rebuildable; cached on the same sidecar (#63). None = not built.
        self._reverse: dict[int, BitMap64] | None = None
        self._reverse_refs: dict[int, BitMap64] = {}
        # The cached reverse postings, loaded at boot at the doc watermark and
        # materialized lazily by ensure_reverse(). Invalidated (→ None) by any
        # commit that lands before it materializes (the #71 contract, for the
        # reverse index): ensure_reverse() then rebuilds from current records.
        self._reverse_blob = reverse_blob
        # Only `_reverse` (target→referrers, what incoming() reads) is cached; the
        # `_reverse_refs` diff memory (referrer→targets, needed only to fold a
        # commit) would be N single-element bitmaps — the same cardinality
        # pathology #12 fixed — so it is rebuilt from `_reverse` on the first fold
        # after a cache load. A read-only reopen never pays it.
        self._reverse_refs_dirty = False

    def ensure(self, ti: TypeInfo) -> ClassIndexes:
        ci = self._by_cls.get(ti.cls)
        if ci is None:
            if self._cache_blobs is not None:
                blob = self._cache_blobs.get(ti.typename)
                if blob is not None:
                    ci = ClassIndexes.load(blob, ti)  # None if the markers changed
            if ci is None:
                ci = build_class_indexes(ti, self._lineage_for(ti),
                                         self._backend.scan_type)
            self._by_cls[ti.cls] = ci
        return ci

    def dump_for_cache(self) -> dict[str, Any]:
        """Return the built per-class indexes as ``{typename: blob}`` for the sidecar.

        Only the forward indexes; the reverse index travels through :meth:`dump_reverse`.
        """
        from datacrystal._entity import type_info  # lazy: _entity imports us

        return {type_info(cls).typename: ci.dump() for cls, ci in self._by_cls.items()}

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

    def check_sorted_temporal(
        self, entries: list[tuple[int, TypeInfo, dict[str, Any]]]
    ) -> None:
        """P1 validation: a SortedIndex datetime field may not mix naive and aware values (#106).

        Raising before the TID is allocated keeps the sequence gapless (invariant 5) and never
        half-mutates the live index.
        It checks the batch against itself and against built indexes, without forcing a build;
        an unbuilt index carries the same guard in ``finalize_build``.
        """
        first: dict[tuple[type, str], Any] = {}  # one already-seen key per field
        for _oid, ti, values in entries:
            sorted_fields = [s.name for s in ti.specs if s.sorted]
            if not sorted_fields:
                continue
            ci = self._by_cls.get(ti.cls)  # only an already-built index — never build
            for field in sorted_fields:
                value = values.get(field)
                if isinstance(value, _decimal.Decimal) and value.is_nan():
                    raise ValueError(
                        f"{ti.cls.__name__}.{field} is a SortedIndex field and cannot "
                        "hold Decimal NaN: NaN is not orderable (use None for 'no value')"
                    )
                if not isinstance(value, _dt.datetime):
                    continue
                key = (ti.cls, field)
                existing = first.get(key)
                if existing is None and ci is not None:
                    run = ci.sorted_keys.get(field)
                    if run:
                        existing = run[0]
                if existing is not None:
                    _guard_temporal_comparable(
                        f"{ti.cls.__name__}.{field}", value, existing
                    )
                else:
                    first[key] = value

    def _invalidate_stale_blob(self, ti: TypeInfo) -> None:
        """Drop the boot-loaded cache blob for a class whose records changed before its build (#71).

        A later ``ensure()`` then rebuilds from current records; a stale blob would resurrect a
        deleted OID or miss a new one.
        """
        if self._cache_blobs is not None:
            self._cache_blobs.pop(ti.typename, None)

    def apply(self, entries: list[tuple[int, TypeInfo, dict[str, Any]]]) -> None:
        """P3: fold a committed batch into every already-built index."""
        if entries and self._reverse is None and self._reverse_blob is not None:
            self._reverse_blob = None  # #63/#71: a write before the reverse index
            #                            materializes makes the cached blob stale
        for oid, ti, values in entries:
            ci = self._by_cls.get(ti.cls)
            if ci is None:
                self._invalidate_stale_blob(ti)  # #71: don't let ensure() load a pre-commit blob
                continue  # not built yet; a later build scans these records
            maintained = set(_maintained_fields(ti))  # eq fields + unique fields
            ci.insert(oid, {f: v for f, v in values.items() if f in maintained})

    def apply_deletes(self, deletes: list[tuple[int, TypeInfo]]) -> None:
        """P3: drop committed deletions from every already-built index
        (unbuilt indexes scan the post-delete records and never see them).
        """
        for oid, ti in deletes:
            ci = self._by_cls.get(ti.cls)
            if ci is None:
                self._invalidate_stale_blob(ti)  # #71: a stale blob would resurrect the OID
            else:
                ci.remove(oid)

    @property
    def reverse_built(self) -> bool:
        return self._reverse is not None

    def ensure_reverse(self) -> dict[int, BitMap64]:
        """Build the global reverse-reference postings lazily from one scan of all records (#20).

        Same derived-data contract as the forward indexes (invariant 11, ADR-005).
        Unlike ``build_class_indexes`` it is global and decodes every field of every record.
        """
        if self._reverse is not None:
            return self._reverse
        if self._reverse_blob is not None:
            # Served from the cache (#63): no O(corpus) re-harvest of every field
            # of every record. The blob was watermark-validated at boot and would
            # have been invalidated by any commit since (apply/remove_reverse).
            blob = self._reverse_blob
            self._reverse_blob = None
            self._reverse = {t: BitMap64.deserialize(b) for t, b in blob["rev"]}
            self._reverse_refs = {}
            self._reverse_refs_dirty = True  # rebuilt from _reverse on the first fold
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

    def dump_reverse(self) -> dict[str, Any] | None:
        """Return the built reverse postings (target → referrers) for the sidecar (#63).

        Return None if they were never built this session.
        Only this direction is cached; the referrer → targets memory is rebuilt on the first fold.
        """
        if self._reverse is None:
            return None
        return {"rev": [[t, bm.serialize()] for t, bm in self._reverse.items()]}

    def _rebuild_reverse_refs(self) -> None:
        """Reconstruct the referrer → targets diff memory by inverting the loaded
        ``_reverse`` (target → referrers) — deferred from a cache load to the
        first fold (#63), so a read-only reopen never pays it. No record decode.
        """
        refs: dict[int, BitMap64] = {}
        for target, referrers in (self._reverse or {}).items():
            for referrer in referrers:
                refs.setdefault(referrer, BitMap64()).add(target)
        self._reverse_refs = refs
        self._reverse_refs_dirty = False

    def apply_reverse(self, ref_entries: list[tuple[int, set[int]]]) -> None:
        """P3: fold a committed batch's outgoing refs into the reverse postings,
        diffing old-vs-new per referrer (like the multi-valued index). Skips when
        the reverse index isn't built — a later ``ensure_reverse`` scans these
        now-committed records (spec §5: an unwatched store pays nothing).
        """
        rev = self._reverse
        if rev is None:
            return
        if self._reverse_refs_dirty:  # first fold after a cache load (#63)
            self._rebuild_reverse_refs()
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
        ADR-003 waited for). Skips when the reverse index isn't built.
        """
        if deleted_oids and self._reverse is None and self._reverse_blob is not None:
            self._reverse_blob = None  # #63/#71: a delete before the reverse index
            #                            materializes makes the cached blob stale
        rev = self._reverse
        if rev is None:
            return
        if self._reverse_refs_dirty:  # first fold after a cache load (#63)
            self._rebuild_reverse_refs()
        for d in deleted_oids:
            old = self._reverse_refs.pop(d, None)
            if old is not None:
                for t in old:
                    posting = rev.get(t)
                    if posting is not None:
                        posting.discard(d)


def _range_slice(
    ci: ClassIndexes, field: str, op: str, value: SupportsRichComparison | None
) -> BitMap64:
    """Return the OIDs whose SortedIndex ``field`` key satisfies ``op value`` (ADR-004, #18).

    Bisect the sorted run and union the matching eq postings.
    None is never in the run, and a None bound matches nothing, as in :meth:`Pred.evaluate`.
    """
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


def readable_bitmap(p: Principal, ci: ClassIndexes) -> BitMap64 | None:
    """Return the OIDs of ``ci``'s extent readable by ``p`` (ADR-008 W3-1), None for root (R9).

    The bitmap twin of :func:`datacrystal._permissions.can_read_row`: owner postings, union
    per held membership the group postings whose read floor is at most that level.
    Only call it for a protected class (zero-cost invariant, #21).
    Iterating just ``p``'s memberships plus the implicit ``WORLD`` one is exact (proof:
    docs/design/ADR-008-permissions-contract.md, "Read surfaces").
    """
    if is_root(p):
        return None
    acc = BitMap64()
    if p.uid != 0:  # R7a: uid 0 is the anonymous principal and owns nothing
        owned = ci.eq["_dc_owner"].get(p.uid)
        if owned is not None:
            acc |= owned
    memberships = dict(p.memberships)
    if WORLD not in memberships:  # the normative implicit world membership
        memberships[WORLD] = VIEWER
    group_postings = ci.eq["_dc_groups"]
    for g, lvl in memberships.items():
        in_group = group_postings.get(g)
        if not in_group:
            continue
        acc |= in_group & _range_slice(ci, "_dc_read_floor", "<=", lvl)
    return acc


_ORDERING_OPS = (">=", ">", "<=", "<")
_STRING_MATCHERS: dict[str, Callable[[str, str], bool]] = {
    "contains": lambda needle, key: needle in key,
    "startswith": lambda needle, key: key.startswith(needle),
}
_STRING_MATCH_OPS = tuple(_STRING_MATCHERS)

_Planned = tuple[BitMap64 | None, Condition | None]


def _matches_string_key(op: str, needle: str, key: object) -> bool:
    """Report whether an index key is a string that contains / starts with ``needle``."""
    if not isinstance(key, str):
        return False
    return _STRING_MATCHERS[op](needle, key)


def _plan_list_field(cond: Pred, ci: ClassIndexes) -> _Planned:
    # Multi-valued (inverted) index (#13): eq[field] keys are the
    # list's elements, so `.contains(x)` is exact element membership
    # — an O(1) posting lookup, no record reads, no residual. ==/in/
    # startswith over a whole list can't be answered from an element
    # index → residual (evaluate() compares the actual list).
    if cond.op == "contains":
        postings = ci.eq[cond.field].get(cond.value)
        return (postings.copy() if postings is not None else BitMap64()), None
    return None, cond


def _plan_eq_indexed(cond: Pred, ci: ClassIndexes) -> _Planned | None:
    """Answer ``cond`` from the eq postings of an Index / SortedIndex field, or ``None``."""
    if cond.op in _ORDERING_OPS and cond.field in ci.sorted_fields:
        # ADR-004 (#18): the THIRD rule — an ordering comparison on a
        # SortedIndex field answers from the sorted run as a range slice
        # (a `between` is an And of two of these, composed below). Still
        # deterministic and rule-based: no cost model, no plan search.
        return _range_slice(ci, cond.field, cond.op, cond.value), None
    if cond.field in ci.list_fields:
        return _plan_list_field(cond, ci)
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
    if cond.op in _STRING_MATCH_OPS:
        # KICKOFF M4: string matching on an indexed field iterates
        # the index's DISTINCT keys and ORs the matching postings —
        # O(distinct values), never a record load.
        acc = BitMap64()
        for key, postings in ci.eq[cond.field].items():
            if _matches_string_key(cond.op, cond.value, key):
                acc |= postings
        return acc, None
    return None


def _plan_unique_only(cond: Pred, ci: ClassIndexes) -> _Planned | None:
    """Answer ``cond`` from the value→oid map of a pure-Unique field, or ``None``.

    A pure-Unique field has no eq postings (#12); ``contains`` / ``startswith`` iterate the
    distinct keys (O(distinct), no record load).
    A Unique+Index or Unique+SortedIndex field is in ``ci.eq`` and takes the other path.
    """
    holder = ci.unique[cond.field]
    if cond.op == "==":
        oid = holder.get(cond.value)
        return (BitMap64([oid]) if oid is not None else BitMap64()), None
    if cond.op == "in":
        acc = BitMap64()
        for value in cond.value:
            oid = holder.get(value)
            if oid is not None:
                acc.add(oid)
        return acc, None
    if cond.op in _STRING_MATCH_OPS:
        acc = BitMap64()
        for key, oid in holder.items():
            if _matches_string_key(cond.op, cond.value, key):
                acc.add(oid)
        return acc, None
    return None


def _plan_pred(cond: Pred, ci: ClassIndexes) -> _Planned:
    if cond.field in ci.eq:
        planned = _plan_eq_indexed(cond, ci)
        if planned is not None:
            return planned
    if cond.field in ci.unique:
        planned = _plan_unique_only(cond, ci)
        if planned is not None:
            return planned
    return None, cond


def _plan_and(cond: And, ci: ClassIndexes) -> _Planned:
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


def _plan_or(cond: Or, ci: ClassIndexes) -> _Planned:
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


def plan(cond: Condition, ci: ClassIndexes) -> tuple[BitMap64 | None, Condition | None]:
    """Split a condition into (bitmap candidates, residual predicate).

    ``==`` and ``in_`` on indexed fields resolve to bitmaps; AND combines
    bitmaps and residuals independently; OR uses bitmaps only when every
    branch is fully indexed; everything else stays a residual evaluated on
    hydrated candidates.
    """
    if isinstance(cond, Pred):
        return _plan_pred(cond, ci)
    if isinstance(cond, And):
        return _plan_and(cond, ci)
    if isinstance(cond, Or):
        return _plan_or(cond, ci)
    return None, cond


def order_via_index(ci: ClassIndexes, matched: BitMap64, field: str,
                    descending: bool) -> list[int]:
    """Order ``matched`` OIDs by an **indexed** ``field`` straight from its postings (#25).

    No record decode: a ``SortedIndex`` field's keys are already sorted (ADR-004), any other
    indexed field sorts its distinct keys once.
    NULLs sort last, and OIDs ascend within a key, the tiebreak offset paging requires.
    """
    postings = ci.eq[field]
    if field in ci.sorted_fields:
        keys = list(ci.sorted_keys[field])
        if descending:
            keys.reverse()
    else:
        keys = sorted((k for k in postings if k is not None), reverse=descending)
    out: list[int] = []
    for key in keys:
        out.extend(postings[key] & matched)
    none_posting = postings.get(None)
    if none_posting is not None:
        out.extend(none_posting & matched)  # NULLs last, both directions
    return out


def windowed_index_order(ci: ClassIndexes, matched: BitMap64, field: str,
                         descending: bool, limit: int | None, offset: int) -> list[int]:
    """Take the ``(offset, limit)`` window of ``matched`` ordered by an **indexed** ``field`` (#66).

    With a ``limit`` it stops after ``offset + limit`` OIDs, O(offset + limit + keys_touched).
    The order equals :func:`order_via_index` sliced, without touching keys past the window.
    NULLs come last, reached only if non-None keys leave the window unfilled.
    """
    if limit is None:  # no window to stop at → the full order, offset-sliced
        ordered = order_via_index(ci, matched, field, descending)
        return ordered[offset:] if offset else ordered
    stop = offset + limit
    postings = ci.eq[field]
    if field in ci.sorted_fields:
        keys: Iterator[Any] = (reversed(ci.sorted_keys[field]) if descending
                               else iter(ci.sorted_keys[field]))
    else:
        keys = iter(sorted((k for k in postings if k is not None), reverse=descending))
    out: list[int] = []
    for key in keys:
        out.extend(postings[key] & matched)
        if len(out) >= stop:
            return out[offset:stop]
    none_posting = postings.get(None)
    if none_posting is not None and len(out) < stop:
        out.extend(none_posting & matched)  # NULLs last, only if the window needs them
    return out[offset:stop]


def iter_oids(bm: BitMap64) -> Iterator[int]:
    return iter(bm)
