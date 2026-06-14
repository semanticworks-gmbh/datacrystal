# ADR-004: Sorted/range index (`dc.SortedIndex`, range-query planning)

Status: **accepted** (2026-06-14, Sven). Grows the live query planner and adds frozen-api public
surface → v0.2+. The storage protocol is **unaffected** — indexes remain rebuildable derived data;
caching them on disk is governed separately by [ADR-005](ADR-005-index-cache.md). (ROADMAP item 18 /
GitHub #18.)

## Context

datacrystal's live query layer has exactly **two planning rules** (the KICKOFF/CLAUDE.md doctrine
*"two rules, never an optimizer — DuckDB over the mirror owns clever"*):

1. `==` / `.in_()` on a `dc.Index`/`dc.Unique` field → roaring-bitmap lookup (O(hits));
2. everything else → a Python residual over the bitmap candidates (or the full extent).

Measured on the **MaStR proving ground** (6.2M real records, eval #4): indexed equality/AND answer
in 1–2 ms, but a **range or non-indexed predicate is a full-extent scan** — `count(Bruttoleistung
>= 1000)` = **20.4 s**, ~10,000× the bitmap path. The SOR/metadata and timeseries personas live on
exactly these queries ("capacity > 1 MW", "registered 2020–2023", "load between t0..t1"). Pushing
all ranges to DuckDB-over-the-Arrow-mirror would force the `[arrow]` extra + a cross-engine hop on
every interactive filter — far too heavy for an OLTP read the application runs constantly.

## Decision

**Add a first-class sorted index to the live OLTP layer, opt-in per field, answering range
predicates from the index — a third deterministic planning rule, not an optimizer.**

1. **Marker — `dc.SortedIndex`.** A bare field marker (like `dc.Index`/`dc.Unique`), declared
   `Annotated[T, dc.SortedIndex]`. **Opt-in per field**: the application decides which fields need
   range queries, exactly as it decides `dc.Index` — so an app pays the sorted-index cost only where
   its access pattern needs it. Valid on orderable scalar shapes (`int`, `float`, `str`, naive
   date/time, optionally `| None`); a list / `Lazy` / entity-ref field **rejects it loudly at
   `@entity`** (eager validation, like the other markers).

2. **The third planning rule.** An ordering comparison (`>=`, `>`, `<`, `<=`; `between` is their
   conjunction) on a `dc.SortedIndex` field is answered from the sorted index as a **range slice** —
   the OID set whose key falls in the interval — then composed with the bitmap rules (`And`/`Or`/`Not`)
   exactly like an `==` posting. It stays **deterministic and rule-based: no cost model, no
   statistics, no plan search.** The doctrine becomes *"three rules, still never an optimizer."*
   `==`/`.in_()` on a `dc.SortedIndex` field also answers from it (a point lookup in the sorted
   structure), so a field never needs both `Index` and `SortedIndex`.

3. **In-memory first (invariant 11 untouched).** The first cut is an in-RAM sorted structure (a
   sorted run of `(key, OID)`, or a B-tree), built lazily on first use by the same per-class scan
   that builds the bitmaps, and maintained incrementally at P3 like every other index. This ADR
   **never persists it** — persistence for *all* index types is ADR-005's single mechanism — so it
   changes nothing in the storage protocol or invariant 11.

4. **Semantics.** `None` never matches an ordering comparison (SQL-style — unchanged from today's
   residual). String ordering is Unicode codepoint order (documented; linguistic collation stays out
   of scope, like FTS). Mixed-type comparisons never match. Order is otherwise the field's natural
   order; ties break on ascending OID (deterministic, matching `limit`/`offset`).

5. **`order_by` is a follow-on, not this ADR.** A sorted index trivially yields ordered iteration,
   so `order_by=` over a `dc.SortedIndex` field (ROADMAP #25) becomes a natural additive story; this
   ADR scopes only the range *filter*.

6. **Explain stays inspectable.** `store.explain()` reports a sorted-index range as a distinct plan
   node — the QueryPlan now names three rule kinds (bitmap-eq, sorted-range, residual), so the
   planner remains fully inspectable and the "no optimizer" claim stays auditable.

## Consequences

- Range/date/numeric filters move from **O(extent) scan to O(log n + hits)** — the 20 s MaStR range
  becomes interactive; the timeseries persona (#18's original motivation) is served in the live
  layer, not only via DuckDB.
- New frozen-api public surface (`dc.SortedIndex`, exported in `__all__`) → v0.2+.
- The planner gains **one rule and one index type**; it does **not** become a cost-based optimizer
  (no stats, no plan search) — the doctrine is preserved verbatim except the rule count.
- A `dc.SortedIndex` field carries a second in-RAM structure (the sorted run) → more index RAM, on
  the same rebuildable terms as the bitmaps; ADR-005's cache covers persisting it.
- **Converges with ADR-005.** A *persisted* sorted index is sorted runs + **zone-maps** (min/max per
  segment, for range-skipping) + **bloom filters** (per-segment, for point lookups) — the
  Bigtable/HBase/Accumulo SSTable shape — riding the `arrow.py`/`deltalog.py` manifest-segment
  substrate. This ADR does not require that; it makes it the natural next step.
- Fitness: a range query's cost is f(hits + log n), not f(extent) — assertable as an operation-count
  gate (invariant 12) and provable on the MaStR proving ground (the 20 s scan → an index slice).
