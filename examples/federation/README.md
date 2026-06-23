# Fractal followers — a live coordinator + edge followers

A runnable, hands-on demo of datacrystal's replication shape (ROADMAP item 21,
[FEDERATION-WIRE-v1](../../docs/design/FEDERATION-WIRE-v1.md)): **one writer** (the *coordinator*)
plus any number of read-mostly **followers** at the edge. Each follower is a *real local
datacrystal store* that bootstraps from the coordinator, reads at full local speed, and contributes
writes back through the single writer.

Run it as two processes in two terminals — the coordinator seeds a small mineral cabinet and **ticks
a `heartbeat` counter every few seconds**, so you can watch a record change ride the wire and poke at
a follower yourself.

## Terminal 1 — the coordinator

The single writer: opens a store, serves the `/v1` federation surface, and increments `heartbeat`
every `--interval` seconds.

```bash
uv run python examples/federation/coordinator.py                      # :8848, ticks every 3s
uv run python examples/federation/coordinator.py --port 9000 --interval 5 --path /tmp/coord
```

Stop it with Ctrl-C — a clean shutdown releases the single-writer lease, so an immediate restart on
the same `--path` reclaims it at once. (Needs `datacrystal[web]` + an ASGI server, `pip install
uvicorn`.)

## Terminal 2 — a follower

`watch` opens a real local replica and prints the `heartbeat` each second as it climbs; `repl`
connects and drops you into a Python REPL with helpers loaded so you can test by hand.

```bash
uv run python examples/federation/follower.py watch                   # poll + print the heartbeat
uv run python examples/federation/follower.py repl                    # interactive: edge, sync(), bump(), …
uv run python examples/federation/follower.py watch --url http://127.0.0.1:9000
```

In the `repl` the follower is `edge` (a real `Store`):

- `show()` — print the heartbeat + minerals at the current local watermark
- `sync()` — pull the coordinator's latest, then `show()`
- `bump('Q1')` — increment a mineral's `mohs` and contribute it back via `committing()` (retries on conflict)
- `contribute('Q9', 'Topaz', 8.0)` — add a new mineral to the coordinator

Try `sync(); bump('Q1'); sync()` and watch it land — then start a **second** follower in another
terminal (or a second `repl`) and `sync()` there to see it converge. Edit the helpers in
`follower.py`, or just type in the REPL, to probe whatever you like.

## What it shows

- **Bootstrap** — a follower replays the coordinator's COMMIT-DELTA-v1 stream from TID 0 into a real
  local store; cross-entity references resolve locally.
- **Propagation** — the coordinator's `heartbeat` ticks; each `sync()` pulls the new commits.
- **Contribute** — a follower's `upsert(...)` + `commit()` fans into the single writer.
- **Optimistic concurrency** — two followers editing the same record don't last-writer-win:
  `committing()` re-runs the block against fresh state on a `ConflictError` and converges with no
  lost update — *the same code on a single-node store and a follower* (the fractal contract).
- **Convergence** — the coordinator and every follower agree on the whole graph.

Both scripts declare the **same** `@entity` model inline and are run as scripts (so both are
`__main__` and the persisted typenames match) — a follower can only resolve types it defines under
the same module path. Needs `datacrystal[web]` + `uvicorn` (coordinator) and `datacrystal[follower]`
(follower); the repo's `uv sync` provides everything.

The automated, asserted version of every behaviour above (over both backends, with the fail-closed
guards) is the conformance test `tests/web/test_federation_conformance.py`. For the production
deployment shape — a standalone `uvicorn coordinator:app --workers 1`, your own authn/z, retention —
see the [federation how-to](../../docs/how-to/federation.md).
