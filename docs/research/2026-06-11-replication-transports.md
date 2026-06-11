# Replication transport options (research memo, 2026-06-11)

**Status: research input to ROADMAP punted item 21 (networked replication — to be added; the
Punted list currently ends at item 20). No commitment implied.** Multi-writer/clustering in core
remains **Never** ([ROADMAP](../design/ROADMAP.md)). The ratified scaling shape is exactly one
writer + N watermark-fed read followers + command fan-in for writes
([ADR-001](../design/ADR-001-concurrency-contract.md), Consequences; [SCALING.md](../design/SCALING.md)).
All URLs accessed 2026-06-11.

## 1. TL;DR

Because COMMIT-DELTA-v1 (ROADMAP item 3) is deterministic, idempotent, and totally ordered by
TID, **the durable log already lives in the writer's store**. The transport therefore only needs
to be a catch-up-capable ordered byte pipe — which makes the simplest options sufficient and the
cleverest ones unnecessary. Zyre/pyre answers a question we do not have (LAN discovery) and fails
the one we do (no durability, no replay, no late-joiner catch-up; binding dormant since 2021 on
PyPI). Ranking for a future `datacrystal[replica]`:

1. **Writer-served HTTP/SSE (or WebSocket)** — zero extra infrastructure; closest to the
   "no coordination server" wish, because the writer *is* the coordination point already.
2. **Object-store shipping** — largely exists today via Litestream (ROADMAP items 6/16); best
   fit for serverless followers.
3. **NATS JetStream** — when a broker is already present and fan-out is large; near-perfect
   semantic fit (replay-from-sequence ≈ replay-from-watermark).
4. **ZeroMQ Clone pattern** — the honest version of the Zyre idea; only worth it if profiling
   shows HTTP fan-out is the bottleneck.
5. Redis Streams (workable, persistence caveats); Dask-native (no — use columnar export);
   Zyre/pyre (no).

## 2. The ratified shape, and what the transport must provide

One writer holds the lease (invariant 10) and emits COMMIT-DELTA-v1 deltas; followers apply
deltas from their TID watermark forward; writes from followers travel as *commands* (fan-in) to
the writer, never as peer mutations. The transport contract is therefore modest:

- **Total order per store** — given for free if deltas are fetched from one writer; TIDs are
  gapless and sequence-derived (invariant 5), so a follower can detect any gap itself.
- **Replay from a TID watermark** — the essential operation: "give me everything after TID *n*".
- **At-least-once is fine** — apply-twice ≡ apply-once, so idempotent delivery suffices;
  exactly-once machinery is unnecessary weight.
- **Discovery is optional** — followers must find the writer once; nothing else.
- **Command fan-in** — plain request/reply with backpressure (the DBOS/Celery `concurrency=1`
  recipe in ROADMAP item 6 is the degenerate form of this already).

Any transport that cannot serve history is disqualified as the *only* channel: a late joiner
must be able to reach the current watermark. A transport that can only do the live tail forces a
second, writer-served catch-up channel — at which point the live-tail transport is decoration.

## 3. Zyre/pyre assessment (the owner's question)

**Direct answer: Zyre solves discovery and group messaging, not replication. Used here, we would
keep the hard part (the log, catch-up, ordering across restarts) and outsource only the easy
part (finding peers) — to a mechanism that does not work on the target networks.**

**What ZRE actually specifies.** RFC 36/ZRE (status: stable; Hintjens, ed.) defines UDP
**broadcast** beacons on port 5670 for discovery, plus TCP/ZMTP mailboxes for WHISPER (unicast)
and SHOUT (group) messages. Sequence numbers are per-peer-connection and strictly incrementing —
a gap means *disconnect the peer*, not retransmit. There is **no persistence, no replay buffer,
no late-joiner catch-up**; the spec even notes that "Sending to a ROUTER socket that does not
(yet) have a connection to a peer causes the message to be dropped"
(<https://rfc.zeromq.org/spec/36/>). The Zyre README's "loses no messages even when the network
is heavily loaded" is qualified in the same breath: "…unless a peer application terminates"
(<https://github.com/zeromq/zyre>) — precisely the case a replication layer exists to survive. A
SHOUT is delivered only to peers currently in the group; a node that joins late, restarts, or
was partitioned simply never sees those messages. To carry a write-request queue where "all
participants publish and all apply", we would have to build, on top of Zyre: a durable ordered
log, a catch-up protocol, and a single-sequencer rule — i.e. items the store and ADR-001 already
provide, minus the parts Zyre was bought for.

**The "all participants publish write requests" model itself** is the multi-writer shape the
Never list forecloses: without a single sequencer there is no total order, and with one (the
lease-holding writer) the model collapses back to command fan-in — which any request/reply
transport carries.

**Discovery on the target networks.** ZRE beacons are UDP *broadcast*, which is subnet-local by
design. On clouds it is simply off: AWS VPCs have no native broadcast/multicast (multicast
requires a Transit Gateway acting as router, and that is multicast, not the broadcast ZRE uses —
<https://docs.aws.amazon.com/vpc/latest/tgw/tgw-multicast-overview.html>); Azure VNets block
multicast and broadcast outright
(<https://learn.microsoft.com/en-us/azure/virtual-network/virtual-networks-faq>); Google VPCs
support unicast only (<https://cloud.google.com/vpc/docs/vpc>). Zyre's gossip fallback (CZMQ
`zgossip`, <http://czmq.zeromq.org/manual:zgossip>) works over TCP but requires connecting to a
well-known endpoint — quietly reintroducing the coordination point the beacon was meant to
avoid; the pyre README documents only beacon discovery. And on the motivating platforms,
discovery is not the hard problem: Slurm hands every job its node set in `SLURM_JOB_NODELIST`
(<https://slurm.schedmd.com/sbatch.html>), and the Dask scheduler knows all workers by
construction.

**Maintenance.** pyre's last PyPI release is **0.3.4, uploaded 2021-08-24**
(<https://pypi.org/pypi/zeromq-pyre/json>); a `0.3.4 → 0.3.5` version bump was committed
2025-01-13 but never reached PyPI; the README still recommends "Python 3.3+"; 22 open issues, no
open PRs (<https://github.com/zeromq/pyre>). Zyre (C) last released 2.0.1 on 2021-01-24
(<https://github.com/zeromq/zyre>). Neither is archived, but neither is something to anchor a
durability feature to.

Verdict: the preliminary assessment stands on all three counts, with one refinement — for HPC
the decisive argument is not fabric capability (broadcast *may* work inside one subnet) but that
the scheduler already publishes the node set, so Zyre's one genuine contribution is redundant
there.

## 4. Transport comparison

| Transport | Durable log | Total order | Replay from watermark | Extra server | Python client | Notes |
|---|---|---|---|---|---|---|
| (a) Zyre/pyre | none | per-connection only | none | no | dormant (PyPI 2021) | discovery only; everything else rebuilt on top |
| (b) NATS JetStream | file/memory, RAFT replicas | per-stream global order | by sequence number or time | yes (nats-server) | active (v2.15.0, 2026-06-05) | best broker-shaped fit |
| (c) Redis Streams | AOF/RDB, fsync caveats | by stream ID | XREAD/XRANGE from any ID | yes (Redis) | mature (redis-py) | persistence honesty required |
| (d) Writer-served HTTP/SSE/WS | the store itself | trivially (one writer) | query store by TID | **no** | stdlib/ubiquitous | recommended default |
| (e) Object-store shipping | S3 (LTX/parquet) | by file sequence | PITR / open-at-watermark | no (S3 assumed) | n/a (Litestream binary) | exists today (items 6/16) |
| (f) Dask pub-sub / actors | none | no | none | scheduler | actors: "no resilience" | wrong tool; export columnar |
| (g) ZeroMQ Clone (pyzmq) | writer-served snapshot | seq numbers added by us | snapshot + numbered updates | no | pyzmq, very active | (d) with sockets; LAN perf tier |

**(b) NATS JetStream.** Streams are persisted (file storage, optional R=3 RAFT replication),
messages "added to a stream in one global order", and consumers can replay "starting from a
specific sequence number" — a near-literal match for replay-from-watermark; publisher dedup
gives idempotent ingest for command fan-in (<https://docs.nats.io/nats-concepts/jetstream>).
nats-py is actively maintained (v2.15.0 on 2026-06-05, v2.14.0 on 2026-02-23, plus new modular
`nats-core`/`nats-jetstream` packages — <https://github.com/nats-io/nats.py/releases>). Cost: a
broker to run, and a second copy of the log to reason about (retention vs. the store's own
history). Sensible *only* where NATS already exists. Notably Litestream 0.5 added a JetStream
replica type, so option (e) can ride (b)'s infrastructure.

**(c) Redis Streams.** Append-only, IDs totally ordered, arbitrary-ID reads (XRANGE/XREAD),
consumer-group state survives restart via AOF — but the docs are blunt: "AOF must be used with a
strong fsync policy if persistence of messages is important", and async replication can drop
XADDs on failover (<https://redis.io/docs/latest/develop/data-types/streams/>,
<https://valkey.io/topics/streams-intro/>). Workable where Redis is already deployed; we would
have to document that the broker's durability is weaker than the store's. No advantage over (b).

**(d) Writer-served HTTP/SSE or WebSocket.** The writer process serves `GET /deltas?after=<tid>`
(catch-up straight from the store) and an SSE/WebSocket live tail; commands arrive as `POST
/commands` and queue onto `store.submit()` — the actor→server door ADR-001 explicitly holds
open ("out-of-process server (command transport swap, ZEO/Redis shape)"). No new process, no new
dependency of consequence, follower implementable with stdlib. This *is* the "no coordination
server" wish, fulfilled by observing that a single-writer system already has a distinguished
node. The watermark protocol doubles as resume-after-disconnect for free.

**(e) Object-store log shipping.** Already half-shipped as roadmap policy: Litestream replicates
the SQLite file to S3 today (item 6 recipe), and v0.5.0 (October 2025) brought the LTX format,
fast point-in-time restore, and **VFS read replicas that serve reads directly from S3 while
syncing** (<https://fly.io/blog/litestream-v050-is-here/>, <https://litestream.io/>). Followers
poll the bucket — no connection to the writer at all. Latency is seconds, not milliseconds;
command fan-in still needs a separate channel. The natural serverless answer.

**(f) Dask-native.** Actors carry documented caveats — "No Resilience… No Diagnostics… No Load
balancing" (<https://distributed.dask.org/en/stable/actors.html>); the pub-sub machinery has
dropped out of the current documentation and has long-standing stuck-delivery reports
(<https://github.com/dask/distributed/issues/2723>). Building replication on scheduler plumbing
couples us to Dask internals for no gain. See §5 for the better answer.

**(g) ZeroMQ Clone pattern.** The zguide is candid that plain PUB/SUB "will lose messages
arbitrarily when a subscriber is connecting, when a network failure occurs, or just if the
subscriber or network can't keep up" — and Clone fixes this with exactly the apparatus we
already own: a server-held state snapshot, sequence numbers, and late-joiner snapshot-then-
updates (<https://zguide.zeromq.org/docs/chapter5/>). Substitute "store at watermark" for
snapshot and "TID" for sequence number and Clone is (d) over sockets. Worth keeping in reserve
for high-fan-out LAN deployments; not worth the pyzmq dependency as the default.

## 5. Use-case fit

- **HPC (Slurm-class).** Node sets are known (`SLURM_JOB_NODELIST`); rank 0 opens the store and
  serves (d), or the job reads a pre-exported snapshot. Discovery is a solved non-problem;
  whether broadcast works on the fabric never has to be tested. For batch analytics, though, see
  the next point — most HPC jobs want the columnar export, not live replicas.
- **Dask-style analytics.** Replicating live object graphs to N workers means N× RAM for data
  the workers will scan, not mutate — and Python-object traversal where the ecosystem wants
  vectorised columns. The roadmap already holds the right tool: v1 Arrow mirrors (item 7) and
  parquet-on-S3 (item 16), which DuckDB/polars/Dask read natively. The framing survives
  scrutiny; replication is for *application* followers (read-mostly services), not compute
  fan-out. The one legitimate replication ask here — workers issuing writes — is command
  fan-in, which is an HTTP POST, not a peer-to-peer mesh.
- **Serverless / scaled multi-node apps.** Followers are ephemeral and firewalled; pulling from
  object storage (e) fits the lifecycle (Litestream VFS replicas exist today), with (d) for
  lower-latency tiers. The writer is necessarily a stateful singleton somewhere — serverless
  changes where it runs, not the shape.
- **LAN-edge / ad-hoc proximity (where Zyre genuinely shines).** Same-subnet machines, no
  infrastructure, peers coming and going — Zyre's home turf, and the one setting where its
  discovery has real value. Even there it would only *find* the writer; deltas and catch-up
  still need (d)/(g) underneath. None of the motivating use cases is this setting.

## 6. What this means for item 21 wording

Item 21 should be transport-agnostic about the pipe and opinionated about the contract:
networked replication = COMMIT-DELTA-v1 over any catch-up-capable ordered transport, with the
writer-served HTTP/SSE follower protocol as the reference implementation in a
`datacrystal[replica]` extra, and broker (NATS JetStream) or object-store (rides item 16)
variants demand-driven behind the same follower interface. Peer discovery is explicitly out of
scope: every motivating environment already knows its node set, and the single writer is the
natural rendezvous. Zyre/pyre should be recorded as evaluated-and-declined (no durable log, no
replay, no late-joiner catch-up; beacon discovery inoperative on cloud VPCs; binding unreleased
since 2021) rather than left open.
