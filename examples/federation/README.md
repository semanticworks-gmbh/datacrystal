# Fractal followers — a live coordinator + edge followers

A runnable proof of datacrystal's replication shape (ROADMAP item 21,
[FEDERATION-WIRE-v1](../../docs/design/FEDERATION-WIRE-v1.md)): **one writer** (the
*coordinator*) plus any number of read-mostly **followers** at the edge, each a real local
datacrystal store that bootstraps from the coordinator, reads at full local speed, and contributes
writes back through the single writer.

```bash
uv run python examples/federation/demo.py
```

This stands up a **real uvicorn coordinator** on a background thread and two `dc.Store.follower`
replicas (one in-memory, one sqlite-backed) talking to it over localhost TCP, then walks the whole
surface and prints `✓`/`✗` for each behaviour (exit code `0` ⇔ every check passed):

1. **Bootstrap** — each follower replays the coordinator's COMMIT-DELTA-v1 stream from TID 0 into a
   real local store; cross-entity references resolve locally.
2. **Contribute** — a follower's `upsert(...)` + `commit()` fans into the coordinator's single
   writer; the other follower sees it after `sync()`.
3. **Cross-entity contribute** — a reference to an already-committed entity rides the wire as its
   coordinator OID and resolves on every replica.
4. **Concurrent edit of the same object** — optimistic concurrency rejects the stale write
   (`ConflictError`, never last-writer-wins), and the documented recovery loop
   `discard()` → `sync()` → re-read → re-apply → `commit()` converges with no lost update.
5. **Fail-closed guards** — a `dc.Blob` field, an `@entity` nested in a bare container, an
   intra-batch new→new reference, and a malformed `/v1/submit` envelope are each rejected loudly
   (the unsafe write never crosses the wire).
6. **Convergence** — the coordinator and both followers agree on the whole graph.

It needs `datacrystal[web]` + `uvicorn` (coordinator) and `datacrystal[follower]` (follower);
`uv sync --all-extras` provides everything. See the
[federation how-to](../../docs/how-to/federation.md) for the production deployment shape (a
standalone `uvicorn coordinator:app --workers 1`, your own authn/z, retention policy).
