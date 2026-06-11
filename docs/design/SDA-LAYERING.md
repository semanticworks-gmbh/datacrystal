# SDA × datacrystal: the layering decision

Date: 2026-06-10. Status: **ACCEPTED 2026-06-10** — Sven confirmed "the SDA concept is for the
application layer"; the roadmap deltas below are folded into [ROADMAP.md](ROADMAP.md) (marked
"SDA delta" inline). Full study:
[../research/2026-06-10-sda-layering/](../research/2026-06-10-sda-layering/) (concept reading,
sda_store implementation analysis, specs/usage, judgment).

## Verdict

SDA (Semantic Digital Asset — Sven's semiotic data model: immutable records as
sigmatics/pragmatics/semantics/syntax/asset/fingerprint/editstamp tuples, append-only emit/revoke,
multi-faceted identity, GoBD-grade auditability) is an **application layer on datacrystal**, not
part of the database concept. Every major SDA element fails at least one layering test for core
inclusion (domain knowledge the DB cannot have; no non-SDA user wants it), while everything SDA
needs from storage is already on the ratified roadmap or a one-flag generalization of it.

The existing `sda_store` is the proof-by-counterexample for datacrystal's existence: all five
measured slowness causes (per-read recursive-CTE dedup clustering, latest-version GROUP BY over
full asset text, N+1 Python reconstruction, rebuild-the-world FTS indexing, per-row commits) are
hand-rolled persistence primitives in application SQL — exactly the primitives datacrystal ships
(identity map, bitmap indexes, watermark-fed FTS sidecar, buffer-until-commit storer). Rebuilt on
datacrystal, the slowness is *structurally* fixed, not moved (details in the judgment's migration
sketch).

SDA-the-model also keeps its academic identity better this way: "SDA: a semiotic data model,
reference implementation on datacrystal" instead of dissolving into a database feature list.

## Proposed roadmap deltas (SDA as first customer)

1. **Promote**: unique secondary-key index (string alias → entity, e.g. sigmatic URIs,
   upsert-by-natural-key) — explicit v0.x commitment instead of implied EntityMap detail.
2. **Add (cheap)**: `@entity(frozen=True)` append-only entity mode — dirty tracking simply never
   arms; generic for the agent-memory persona (episodic logs, provenance), not just SDA.
3. **Resequence**: pull `datacrystal[fts]` back from "after v1 core freeze" into late v0.x — the
   watermark pipeline is "the most load-bearing undelivered component" and needs a real consumer
   as validation harness; FTS5 is the cheapest one and SDA needs it.
4. **Add to punt list**: `datacrystal[ledger]` — hash-chained commit log + Merkle
   inclusion/completeness proofs as a Tier-3 watermark consumer (GoBD/audit/agent-provenance);
   demand-driven, requires only deterministic replayable commit deltas (already promised).
5. **Add (trivial)**: batch hydration API (load-many-by-OID) — the `get_sdas_by_uris_batch`
   lesson; N+1 must never be the user's problem.
6. SDA confirms: asyncio story gets a real customer in v0.x (semantic-studio is fully async);
   ≥2 `@Vector` fields per entity must work (Triple Sigmatics dual embeddings).
7. Non-gains, for the record: SDA does **not** justify accelerating Arrow mirrors, the
   reverse-reference index, or the RDF extension, and adds zero pressure on any "Never" item.

## Hard boundaries (what must not leak into core)

Schema-free EAV as a core mode (contradicts typed-dataclasses-as-schema and the no-object-dtype
Arrow rule); semiotic axis names in core API; GoBD/compliance semantics (retention, legal hold,
Z3 export — regulatory liability); Merkle computation in the commit path (sidecar, not tax);
dedup/sameAs/redirect resolution in the read path (the `sda_union` mistake); authority shadowing;
a tenant concept in core (multi-store-per-tenant is the answer); a materialized-view DSL.

## The shape of the SDA package on datacrystal

Typed entities for stable layers (`OutlineNode`, `SkosConcept`, `Blob`, `DedupCluster`) +
one frozen `Statement` entity for the schema-free tail (the 5-tuple with bitmap-indexed axes;
emit = store, revoke = frozen revocation record) as the audit-faithful event log, with typed
entities as the maintained "latest" projection updated in the same commit. Dedup becomes
incremental union-find at write time (a commit-delta consumer) instead of a per-read view.
Tenancy: one store per org dir via `aopen()`. If interim relief is needed before datacrystal is
usable: materialize `v_clusters` into a refreshed table + add a latest-pointer table in
`sda_store` (~two-day patches that mirror the eventual architecture).
