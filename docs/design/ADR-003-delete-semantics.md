# ADR-003: unchecked delete (`store.delete()`, tombstone deltas, `CommitBatch.deletes`)

Status: **accepted** (2026-06-12, M4). Scope rules being honored: "storage-protocol
growth requires an ADR" ([KICKOFF.md](KICKOFF.md) §8 risk 6) and the
[COMMIT-DELTA-v1](COMMIT-DELTA-v1.md) lock-at-tag discipline.

## Context

datacrystal deliberately decouples persistence from reachability (unlike
EclipseStore): `store.store(obj)` registers explicitly, root reachability only
controls memory pinning. The consequence is that nothing is ever "garbage" — the
store is insert/update-only, which a real sync-to-source workload (registers with
withdrawn actors, ETL against changing upstreams; the 2026-06-12 MaStR import
feedback) cannot live with.

Timing is contract-critical: COMMIT-DELTA-v1 locks at the v0.1.0 tag with the
`delete` op *reserved but never emitted*. M3's prior-value spike existed precisely
so the schema "cannot freeze wrong" — the same logic requires `delete` to be
emitted, applied, and vector-pinned **before** the tag, not retrofitted after.

What v0.x cannot deliver is referential integrity: cascade and orphan checks need
the reverse-reference index (ROADMAP item 8, v1). Waiting for it would hold the
delete shape hostage to a v1 feature.

## Decision

**Unchecked delete now; checked delete is v1 work on the reverse-reference index.**

1. **API**: `store.delete(obj)` and `store.delete(cls, **unique_key)` (the latter
   needs no hydration — the unique map yields the OID). Idempotent: deleting an
   absent key or an already-deleted entity returns `False`, never raises. Deleting
   a NEW (never-committed) entity cancels its pending insert; no record, no delta
   op. The root holder refuses deletion (assign `store.root` instead).
2. **Buffered, one commit path** (invariant 4): `delete()` only buffers; `commit()`
   executes through the same P1/P2/P3 machine. The owner-thread check fires before
   any buffer mutation (invariant 3). A failed P2 re-buffers the deletes and reuses
   the TID (invariant 5, gapless).
3. **Precedence**: a buffered delete wins over any buffered write to the same OID
   in the same commit (the write is dropped, deterministically).
4. **Entity lifecycle**: a fourth state, `DELETED`. Mutating or re-`store()`ing a
   DELETED instance raises `DeletedEntityError` (pre-mutation, like every write
   barrier); re-deleting returns `False`. Field *reads* keep working (it is a
   plain detached object). *Referencing* a DELETED instance from another entity
   is allowed and encodes as a ref to the dead OID — deliberately: rejecting it
   would be a de-facto checked delete exactly where the user cannot enumerate
   referrers (no reverse index until v1), making bulk sync commits
   un-committable. Unchecked means unchecked, uniformly; the dangle is loud at
   dereference (rule 8), never at creation.
5. **Physical removal**: `CommitBatch` grows a `deletes: list[int]` field; backends
   remove the rows inside the same atomic transaction as the batch's upserts. This
   is the protocol growth this ADR ratifies (the `apply()` signature is unchanged).
   No on-disk tombstones: the store retains no history (ROADMAP item 20 records
   that), so a tombstone row would be dead weight.
6. **Delta emission**: each delete emits
   `{"op": "delete", "oid", "cid", "payload": nil, "prior": <last payload>}` —
   exactly the shape reserved by spec §3.1 since draft rev 1. Priors for deletes
   are read in P1 under the same rules as update priors (only while consumers
   watch, strictly before the TID allocation). Within one delta, ops are upserts
   in capture order, then deletes in deletion order; one OID can never appear in
   both (precedence rule + OIDs are never reallocated).
7. **Indexes** (invariant 11): P3 folds deletions into every already-built index
   (extent, eq postings, unique map) using the index's own `last_values` memory —
   never a store read. Unbuilt indexes scan post-delete records later and never
   see the OID.
8. **Dangling references raise `DanglingRefError`** at dereference time —
   hydrating an eager ref, `Lazy.get()`, `get_many()` with a dead OID, or a
   snapshot `get()` of an OID missing at its watermark. Nothing prevents creating
   the dangle (that is the *unchecked* part); following it is always loud, never
   `None`-y.
9. **Unique-key reuse**: a value freed by a buffered delete may be claimed by a
   new entity in the same commit (`check_unique` exempts pending deletions).

## Consequences

- The delta op vocabulary is fully exercised before the contract locks: replay
  vector `004-delete.bin` pins the tombstone bytes (authored additively — vectors
  001–003 are byte-identical; adding a vector for reserved-since-rev-1 behavior is
  authoring, not regeneration, so **no draft-rev bump**: consumers were required to
  be total over the vocabulary from day one, and the conformance kit has asserted
  delete totality since M3).
- Vector 004 deliberately leaves the pinned root referencing the deleted OID — the
  unchecked-delete contract, documented in bytes.
- Disk space is actually reclaimed (SQLite row deletion; file shrink still follows
  SQLite vacuum semantics — documented in the GUIDE, not engine work).
- A deleted entity's live instance, if the user still holds one, is a detached
  plain object: readable forever, write-barred, registry-evicted at P3.
- A deleted entity inside the *root graph* makes `store.root` reads raise
  `DanglingRefError` after a reopen (the eager hydration follows the dangle).
  The root setter is the recovery path: assigning `store.root` replaces the
  holder (the orphaned holder record is deleted in the same commit) — the store
  is never bricked by a dangling root.
- A failed hydration (dangling eager ref, schema mismatch) evicts the
  half-filled instance from the registry before re-raising — the identity
  contract never serves a corpse.
- "Real" checked delete (refuse-if-referenced, cascades, orphan sweeps) arrives
  with v1's reverse-reference index and is *additive* API on top of this — nothing
  here forecloses it.

## Closing note — the enumeration seam landed (2026-06-14, ROADMAP item 8 / #20)

The reverse-reference index (`store.incoming()`, and `Snapshot.incoming()` at a
pinned watermark) now ships. It deliberately stops one step short of *checked*
delete and instead provides the **enumeration** this ADR said v1 would need:

- A deleted **target** keeps its reverse postings (rule 8's dangle is intentional;
  OIDs are never reused, rule 4). So `incoming(dead_oid)` returns **exactly the
  entities still pointing at the dead OID** — the referrers a refuse-if-referenced
  or cascade policy would act on. `Snapshot.incoming(dead_oid)` answers the same at
  its watermark even though `snapshot.get(dead_oid)` raises `DanglingRefError`
  (rule 8): the seam works without the target's record.
- A deleted **referrer** drops out of the postings (its outgoing edges vanish),
  so it never appears as a phantom backlink.

Checked delete (the *policy* on top — refuse, cascade, orphan-sweep) remains
deferred and additive: this ADR's unchecked contract is unchanged, and the
reverse index is the rebuildable derived data (invariant 11) it consumes.
