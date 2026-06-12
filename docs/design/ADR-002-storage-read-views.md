# ADR-002: storage read views (`read_view()` on the storage protocol)

Status: **accepted** (2026-06-12, M3). Scope rule being honored: "storage-protocol
growth requires an ADR" ([KICKOFF.md](KICKOFF.md) §8 risk 6).

## Context

M3 ships `store.snapshot()` — frozen-DTO reads at commit watermarks, readable from
any thread *while the owner commits* (ADR-001 rider 2; ROADMAP item 3). The
existing protocol cannot serve that:

- The sqlite backend runs owner reads and commit-P2 writes through **one shared
  connection**. A foreign-thread read during P2's open transaction would see that
  transaction's uncommitted rows (same-connection reads are not isolated), tearing
  the snapshot across a commit boundary.
- The memory fake has the same property by construction (one dict, one lock).

## Decision

Add **one** method to `StorageBackend` — `read_view() -> StorageReadView` — where
the view is a read-only subset of the backend surface (`boot`/`load_many`/
`scan_type`/`close`) pinned to the latest durable commit boundary at creation:

- **sqlite**: a dedicated connection per view with `PRAGMA query_only=ON` and an
  explicitly started WAL read transaction. WAL gives per-connection snapshot
  isolation; the view sees exactly one commit boundary regardless of concurrent
  P2 writes. `close()` ends the read transaction (an open one blocks WAL
  checkpoint truncation — snapshots are context managers for this reason).
- **memory**: a shallow copy of meta/types/records taken under the backend lock
  (records are frozen dataclasses; sharing them is safe).

`read_view()` must be safe to call from any thread; the view's `boot()` only
reads meta and type rows (no DDL, no format-version repair — that work belongs to
the engine-owning `boot()` at open).

## Consequences

- `store.snapshot()` needs no owner coordination at all: any thread materializes
  a consistent view without touching live engine state. The commit path is
  untouched — invariant 11 (indexes/sidecars never inside the commit txn) holds.
- A snapshot taken while P2 has committed but P3 has not yet run on the owner may
  be **one commit ahead** of `store.last_tid`. That is honest: the commit it sees
  is durable. Documented in the GUIDE.
- The protocol stays small (5 methods + the view); the punted custom log
  (ROADMAP 14) can implement the view over its checkpoint + footer chain.
