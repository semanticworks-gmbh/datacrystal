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

## Two-process live playground — watch changes, poke a follower yourself

`demo.py` is a one-shot proof; for **interactive** testing run the coordinator and follower as two
real processes. The coordinator seeds a cabinet and **ticks a `heartbeat` counter every few seconds**
so you can watch a record change propagate.

**Terminal 1 — the coordinator** (single writer, serves `/v1`, ticks the counter):

```bash
uv run python examples/federation/coordinator.py                      # :8848, ticks every 3s
uv run python examples/federation/coordinator.py --port 9000 --interval 5 --path /tmp/coord
```

**Terminal 2 — a follower.** `watch` opens a real local replica and prints the heartbeat each second
as it climbs; `repl` drops you into a Python REPL with helpers loaded so you can test by hand:

```bash
uv run python examples/federation/follower.py watch                   # poll + print the heartbeat
uv run python examples/federation/follower.py repl                    # interactive: edge, sync(), bump(), …
```

In the `repl` the follower is `edge` (a real `Store`); `show()` prints the current state, `sync()`
pulls the coordinator's latest, `bump('Q1')` contributes an increment via `committing()` (retries on
conflict), and `contribute('Q9','Topaz',8.0)` adds a new mineral. Try `sync(); bump('Q1'); sync()`
and watch it land — then run a second follower in another terminal to see it converge. Edit the
helpers in `follower.py` (or just type in the REPL) to probe whatever you like. Both scripts declare
the **same** `@entity` model and are run as scripts (so both are `__main__` and the persisted
typenames match) — a follower can only resolve types it defines under the same module path.
