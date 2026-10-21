# Explanation: how datacrystal thinks

After this page you can predict what stays in memory, what a query costs, what a delete leaves behind and who may read a record.

```python
from typing import Annotated

import datacrystal as dc

@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str, dc.Index]
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None

store = dc.Store.open("cabinet.store")
if store.root is None:
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.root = {"minerals": [
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic", mohs=3.5,
                type_locality=dc.Lazy.of(tsumeb)),
        Mineral(qid="Q190063", name="malachite", crystal_system="monoclinic", mohs=3.8,
                type_locality=dc.Lazy.of(tsumeb)),
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal", mohs=7.0),
    ]}
    store.commit()

azurite, malachite, _ = store.root["minerals"]
print(azurite.type_locality.get() is malachite.type_locality.get())
print(store.root["minerals"][0] is store.get(Mineral, qid="Q193563"))
print(store.explain(Mineral.crystal_system == "monoclinic"))
print(store.explain(Mineral.mohs >= 3.6))
print(store.count(Mineral.mohs >= 3.6), store.pluck(Mineral, "name", limit=2))

locality = store.get(Locality, qid="Q571997")
print([m.name for m in store.incoming(locality)])
store.close()
```

- [Identity and memory](#identity-and-memory)
- [Query semantics: the planner, the residual, and the candidate set](#query-semantics-the-planner-the-residual-and-the-candidate-set)
- [Why deletes are unchecked in v0.x](#why-deletes-are-unchecked-in-v0x)
- [Permissions: read floors, write floors, and the masked deref](#permissions-read-floors-write-floors-and-the-masked-deref)
- [Why followers are real local stores (the fractal contract)](#why-followers-are-real-local-stores-the-fractal-contract)
- [The design philosophy](#the-design-philosophy)

## Identity and memory

There is exactly one live instance per stored object, so every path to an entity yields the same Python object, cycles included, and that holds across a restart.
What keeps an object alive is what pins its identity, so the two are one question.

- The graph reachable from `store.root` is pinned: it stays in memory and identity-stable.
- Everything else, such as query results and lazily loaded subgraphs, is garbage-collected once you drop it and rehydrates under the same OID on next access.
  That is how a dataset larger than RAM works: keep it off the pinned root.
- `dc.Lazy[T]` is the explicit cut point where pinning, loading and memory stop.
  It is a type in your model, not a heuristic, so the memory boundary is visible in your code.
  A `list[dc.Lazy[T]]` is an adjacency list whose edges hydrate one `.get()` at a time.
- Put eager references on the hot path and `dc.Lazy` on cold edges; the eager part is your memory budget, about 600 B per object.
  `lazy_timeout=` demotes idle handles, and there is no hard per-store cap; for analytics-style scans use the [Arrow mirror](how-to/analytics.md).

Recipes: [ingest and memory](how-to/ingest-and-memory.md).
How a reference becomes an OID: the [glossary](reference.md#glossary).
The concurrency contract behind single-owner identity: [ADR-001](design/ADR-001-concurrency-contract.md).

## Query semantics: the planner, the residual, and the candidate set

The query engine is rule-based and never grows an optimizer, so `explain()` tells you in advance why a query costs what it costs.
When you want a cost-based optimizer, hand the Arrow mirror to DuckDB ([analytics](how-to/analytics.md)).

- The candidate set is the OIDs a query narrows to first.
  `==` or `.in_()` on a `dc.Index` field, or a range on a `dc.SortedIndex` field, produces it from a roaring bitmap or a sorted slice without touching a record; a bare class or a non-indexed predicate has the whole extent.
- The residual is the part the indexes cannot answer, such as `>=` on an unsorted field, `!=` or `.contains()` on a non-indexed field.
  It is a Python filter over the decoded candidates.
- The plan is the split `explain()` reports, and `query()` hydrates at most `plan.candidates` entities, so the plan is the cost model.
- `count()` and `pluck()` decode records without building entities, so even their full-scan form costs decode time and no entity memory.
- A bare `order_by` plus `limit` on a `SortedIndex` field beats a `>= sentinel` for top-N because it windows lazily to about `offset + limit` rows, while the sentinel considers every match as a candidate ([recipe](how-to/querying-and-paging.md)).
- The `SortedIndex` range slice is the third deterministic planning rule ([ADR-004](design/ADR-004-sorted-range-index.md)).
  Indexes may be cached on disk but are never authoritative ([ADR-005](design/ADR-005-index-cache.md)).

Contract: [Reading API](reference.md#reading-api).

## Why deletes are unchecked in v0.x

`store.delete()` does not check whether other records still reference the entity ([ADR-003](design/ADR-003-delete-semantics.md)).
A checked delete needs a reverse-reference index, and the policy on top of it (refuse if referenced, or cascade) is the v1 work.

- A delete physically removes the record, and following a stale reference (eager hydration, `Lazy.get()`, `get_many`, a snapshot `get`) raises `DanglingRefError` at follow time, never a silent `None`.
- `strict_deletes=True` raises at the offending `commit()`, naming the referrers, before a TID is allocated.
  `debug=True` runs the same check but only warns and commits.
- `store.incoming()` already is the reverse-reference index in read-only form, and a deleted target keeps its postings, so `incoming(dead)` lists exactly the referrers a checked delete would act on.

Contract: [Deleting](reference.md#deleting), [Transactional guarantees](reference.md#transactional-guarantees-acid).

## Permissions: read floors, write floors, and the masked deref

Native permissions ([ADR-008](design/ADR-008-permissions-contract.md)) let agents write curated records next to hand-entered ones with no separate policy engine.
A `protected=True` record carries four small indexed columns (`owner`, `groups`, `read_floor`, `write_floor`), a `dc.Principal` carries a level per group, and two integer comparisons decide everything ([mechanics](reference.md#protecting-records-protectedtrue)).

- The read check is Bell–LaPadula, "no read up": you may read a record only if the highest level you hold in any group it is shared with clears its read floor.
- The write check is Biba, "no write below the floor": you may change a record, content or labels, only if your authority clears its write floor.
  The write floor binds everyone including the owner, so once a curator raises a floor, the agent that created the record can never overwrite it.
- It is not field-level (decompose into entities instead), not read-time sub-graph inheritance (a child inherits its container's labels once at write time, which classical MLS calls tranquility and which keeps labels auditable), and not a relationship graph.

### Mask on deref, filter on discovery

- Discovery surfaces (`query`, `query_iter`, `get`, `get_many(cls, key=...)`, `count`, `pluck`, `explain`, `incoming`) answer "what is out there".
  A denied row is absent, indistinguishable from one that never existed, and none of them raises, because a count that visibly dropped would itself leak existence.
- Deref surfaces (`Lazy.get()`, `get_many` over OIDs or handles you hold) answer "resolve what I point at".
  You already know it exists, so a denial returns a `dc.Redacted` twin: `isinstance` holds and traversal never raises, and only using the data raises `ReadDeniedError`.
- Masking is the default so that tightening permissions does not change call sites; the cost is that nothing marks a possible stub except the `isinstance(x, dc.Redacted)` check you make before using a record you did not just discover.

### One predicate on every surface

Every read surface applies the same predicate, `readable_bitmap()` and `can_read_row()`, compiled once and used whether the surface answers from a bitmap, a decode-level scan or a registry hit, as Postgres row-level security does inside the engine.
`tests/unit/test_read_fence_sieve.py` runs one protected record through every live-store surface, so a surface that forgets the compiler fails there.

### This is not cryptography

Labels are metadata the engine enforces, not encryption, and an embedded single-writer store cannot defend against hostile same-process code or direct access to its files.
What they buy is confused-deputy protection, correct multi-principal behavior on every existing surface (web, followers, background agents) and a stamped audit trail, since every commit records who in the delta log.
Root is a deliberate, stamped break-glass: `EXECUTIVE` in the `WORLD` group.

A mirror of protected data is protected data: the `datacrystal[fts]` sidecar, the `datacrystal[arrow]` mirror, the retained delta log and the federation `/v1/deltas` feed hold their records in the clear.
They are guarded by placement under the store root and by root-binding (`Snapshot._stream` and the mirror bootstraps require a root snapshot for a protected class), not by the row floor.
Details with each surface: [Snapshots](reference.md#snapshots), [permissions how-to](how-to/permissions.md#what-the-fence-does-and-does-not-cover), [search how-to](how-to/search.md).

## Why followers are real local stores (the fractal contract)

A follower (`Store.follower(url)`) is not a thin client or a cache in front of a remote database; it is a real local store that replays the coordinator's COMMIT-DELTA-v2 stream and then reads at local speed ([VISION.md](design/VISION.md)).
The coordinator is an ordinary single-writer store with its change-feed served over HTTP, so "coordinator" is a role.

- The read and write API is identical on a single node and a follower, and `store.committing()` is the same code in both.
- The one thing distribution adds, that a write can lose a race, surfaces as an OCC `ConflictError` that you re-read and retry, never last-writer-wins and never a remote call disguised as a local one.
- A follower contributes through `upsert` and `commit`, fanned into the single writer.
- The shape composes: a node can follow one coordinator and coordinate its own followers.

How to: [federation](how-to/federation.md).
Rationale: [SCALING.md](design/SCALING.md), [FEDERATION-WIRE-v1](design/FEDERATION-WIRE-v1.md).

## The design philosophy

Every other persistence path makes you translate: an ORM flattens your graph into tables, JSON throws away your types, pickle trusts arbitrary code on load.
datacrystal's thesis is that your data follows your code, with typed dataclasses and a place to put bytes ([VISION.md](design/VISION.md)).

- No translation layer: slots dataclasses are the canonical form, records are msgspec msgpack, and decoding cannot execute code (no pickle, invariant 1).
  Schema, indexes and search config live in the type through `Annotated[...]`, so there is no schema file to drift.
- No session and no `save()`: you mutate objects and `commit()`, with transparent dirty tracking including in-place `list` and `dict` mutation.
  With a session, the objects would be a cache in front of the database instead of the database.
- Predictable beats fast-but-mysterious: the rule-based planner, the explicit `dc.Lazy` cut point instead of a guessing pre-fetcher, and durability as a triad you pick, each with a stated loss window.
- Honest scope: single-writer by design (owner-confined plus a process lease), scaling within one process and out to many readers through snapshots ([SCALING.md](design/SCALING.md)).
  Each ACID letter is stated as strongly as a cited test or setting backs it ([Transactional guarantees](reference.md#transactional-guarantees-acid)), and a feature that does not exist is marked planned.
- Derived data is rebuildable, never authoritative: indexes, the reverse-reference index, the FTS sidecar and the Arrow mirror are watermark-validated and rebuilt from the records (invariant 11, amended by [ADR-005](design/ADR-005-index-cache.md)).

Scope authority is [ROADMAP.md](design/ROADMAP.md), and the engineering standards are in [KICKOFF.md](design/KICKOFF.md).
