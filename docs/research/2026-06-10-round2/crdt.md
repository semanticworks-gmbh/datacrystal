# Round-2 finding: crdt

_Round-2 research, 2026-06-10. Confidence: high. Cross-examined verdicts in [cross-examination.md](cross-examination.md)._

## Summary

CRDTs do not solve pyrsistance's stated multi-writer gap ('uvicorn --workers 4'): that is a same-machine coordination problem where coordination is cheap (shared disk, IPC in microseconds) and where users want ONE consistent view with enforced invariants — exactly what CRDTs by design give up. CRDTs solve the opposite problem (merge without coordination across devices/offline), which DOES match the local-first persona, but only for specific data shapes (collaborative text, sets, movable trees), and even local-first leaders (Figma, Linear, ElectricSQL post-pivot) converged on LWW-per-field with a central authority instead of full CRDTs. The 2026 Python CRDT ecosystem has exactly one production-grade option (pycrdt/yrs, battle-tested in Jupyter); automerge-py is pre-1.0 and loro-py is official but thinly adopted; cr-sqlite — the closest analog to 'CRDT inside an embedded DB' — has been dormant since Oct 2024. Recommendation: keep the single-writer core forever; answer multi-worker with read-only secondary processes plus an optional writer-IPC queue; add CRDT later as an opt-in annotated field type (CRDT doc stored as a blob inside a normal record, merged by pycrdt/loro, indexed via the existing commit-delta/watermark pipeline) to enable device sync and collaborative text without contaminating the core data model.

## Key findings

### 2026 library survey: healthy Rust cores, thin Python bindings

automerge core is very active (6.3k stars, pushed 2026-06-09); Automerge 3.0 (Aug 2025) moved its columnar compression format into memory, cutting RAM ~10x (Moby Dick doc: 700MB -> 1.3MB, load 17h -> 9s). But automerge-py is early: 102 stars, latest stable PyPI release 0.1.2, only 0.2.0.devN pre-releases through Apr 2026. loro is very active (5.7k stars, 1.x stable, shallow snapshots = git-shallow-clone-style history truncation, Eg-walker, no tombstones in state, movable list/tree types); loro-py exists and is current (PyPI 1.10.3, Dec 2025) but has 27 stars — official yet barely adopted. json-joy is active but TypeScript-only, irrelevant for Python.

### pycrdt is the only production-proven Python CRDT binding

pycrdt (y-crdt/jupyter-server orgs, maintained by David Brochart) binds yrs 0.26 via pyo3 0.28.3, latest release 0.13.1 (May 2026), repo pushed 2026-06-09, requires Python >=3.10 with wheels compatible with 3.14. It powers Jupyter's real-time collaboration stack (jupyter-ydoc, pycrdt-websocket), making it the only Python CRDT library with large-scale production usage. If pyrsistance ever embeds a CRDT, pycrdt is the lowest-risk dependency; loro is the better data-model fit (movable tree mirrors object graphs) but riskier on binding adoption.

### Cautionary tale 1: cr-sqlite is dormant

vlcn-io/cr-sqlite — the most-cited 'CRDT inside an embedded SQL DB' project (3.7k stars) — had its last release v0.16.3 in Jan 2024 and last commit Oct 2024; the maintainer moved on (Rocicorp/Zero). Its own constraints documentation is a concise statement of what CRDT-ification costs a database: on CRR tables 'Foreign keys and joins are allowed but the constraint cannot be checked', 'Unique constraints that are not the primary key' are prohibited, and multi-column CHECK constraints are prohibited — i.e., exactly the invariants an object-graph DB with unique bitmap indexes and FK-like refs needs.

### Cautionary tale 2: ElectricSQL, Figma, Linear all walked away from full CRDTs

ElectricSQL (founded by CRDT researchers) scrapped its CRDT-based active-active through-the-database write path in the July 2024 'Electric Next' rewrite: 'The complexity of the stack has provided a wide surface for bugs'; it is now a read-path sync engine (Shapes over HTTP) with writes delegated to your own API. Figma explicitly rejected CRDTs — 'CRDTs are designed for decentralized systems where there is no single central authority... There is some unavoidable performance and memory overhead' — and uses server-authoritative LWW per object property. Linear's sync engine uses a central server assigning a total order (incremental sync id) with LWW; CRDTs are used only for issue descriptions (rich text). Pattern: with any central authority available, per-field LWW + authority beats full CRDTs.

### What CRDTs cost an object-graph database (honest ledger)

Buy: convergence without coordination, offline merge, principled merge semantics for text/lists/counters/maps, causal versioning. Cost: (1) global invariants become unenforceable — unique indexes (two replicas insert the same email, both locally valid, merge violates uniqueness), FK-like refs (concurrent delete-vs-reference yields dangling refs or resurrection; add-wins vs remove-wins is app-specific), bounded counters (PN-counters can go negative; fixing this needs escrow = coordination again); (2) per-field metadata (actor IDs + logical clocks per register, per-character IDs for text) would multiply the ~600B/object envelope and is incompatible with plain msgspec records; (3) tombstone/history growth requires either coordinated GC or library-specific mitigation (automerge 3 columnar compression, loro shallow snapshots); (4) the semantic-merge problem: convergence is not intention preservation — two individually valid replicas can merge into a state no user ever saw or approved (Kleppmann, 'CRDTs: The Hard Parts').

### Direct answer: CRDTs are the wrong tool for 'uvicorn --workers 4'

Four workers on one machine share a disk and a kernel and want a single, immediately consistent view with enforced invariants — a coordination problem where coordination costs microseconds. CRDTs are engineered for the opposite regime (coordination expensive or impossible): adopting them here means each worker holds a divergent in-memory graph between merges, reads go stale, unique indexes and refs cannot be enforced, and you pay full metadata/tombstone cost while gaining zero availability benefit. Correct tools: (a) document 'run one process' for v1 (matches persona); (b) read-only secondary processes that open the SQLite blob store read-only and refresh snapshots at commit watermarks; (c) a writer-process + IPC command queue (mutations over unix socket to the single writer) — the same answer EclipseStore gives (single-process core; clustering is a separate enterprise product).

### CRDTs DO fit the actual persona — but only as a sync feature, not a storage model

Local-first/desktop/agent-memory apps eventually want cross-device sync and collaborative text — the genuine CRDT sweet spot. But the Figma/Linear evidence says most app data syncs fine with per-field LWW + a simple authority (or even file-level sync), reserving real CRDTs for text/sequence/tree fields where merge semantics genuinely matter. So CRDT belongs in pyrsistance as a targeted capability for specific fields/subgraphs, not as the answer to concurrency.

### Design sketch A (recommended): CRDT as opt-in annotated field type

Annotated[Text, pyr.Crdt] (or pyr.CrdtDoc[T]) stores a pycrdt/loro document as a bytes blob inside a normal msgspec record. Dirty tracking hooks the doc's version vector; the commit-delta/watermark pipeline already treats indexes as rebuildable derived data, so FTS5 can index the extracted plaintext and the sync layer can export 'updates since version vector X' per subgraph — the watermark design maps one-to-one onto CRDT incremental update export. Zero core changes, one optional dependency, single-writer invariants preserved for the rest of the graph. This pattern works TODAY with no framework support (user stores doc.export() in a bytes field), so v1 only needs documentation.

### Design sketch B (rejected): CRDT as the core data model

Making every object a CRDT map of LWW registers would: replace slots-dataclasses + msgspec with automerge/loro document objects (killing the canonical-object-form decision and GraphQL/dataclass ergonomics), blow the 600B/object x 1-5M envelope with per-register metadata, make unique bitmap indexes and ref integrity unenforceable, force dirty-tracking and the Arrow mirror pipeline to be rebuilt on CRDT changesets, and chain a solo maintainer to automerge-py (pre-1.0) or loro-py (27 stars) for the system's innermost loop. Every comparable production system that had a central authority chose LWW-plus-authority instead. Never.

## Recommendation

Do not use CRDTs for multi-writer — v1 stays single-writer ('run one process'), v1.x adds read-only secondary processes at commit watermarks, and an optional writer-IPC command queue extension covers same-machine multi-process if demand materializes; ship CRDT only as a post-v1 opt-in extension (Annotated[Text, pyr.Crdt] field storing a pycrdt — fallback loro — doc as a blob, indexed/synced through the existing commit-delta/watermark pipeline) to serve the persona's real need (cross-device sync, collaborative text); CRDT as the core data model: never — it destroys the 600B/object envelope, unique indexes, and ref integrity, and every production system with a central authority (Figma, Linear, post-pivot ElectricSQL) chose LWW-with-authority instead.

## Sources

- https://automerge.org/blog/automerge-3/
- https://github.com/automerge/automerge
- https://github.com/automerge/automerge-py
- https://pypi.org/project/automerge/
- https://github.com/y-crdt/pycrdt
- https://pypi.org/project/pycrdt/
- https://y-crdt.github.io/pycrdt/
- https://github.com/loro-dev/loro
- https://github.com/loro-dev/loro-py
- https://pypi.org/project/loro/
- https://loro.dev/blog/v1.0
- https://github.com/streamich/json-joy
- https://github.com/vlcn-io/cr-sqlite
- https://vlcn.io/docs/cr-sqlite/constraints
- https://electric.ax/blog/2024/07/17/electric-next
- https://www.figma.com/blog/how-figmas-multiplayer-technology-works/
- https://github.com/wzhudev/reverse-linear-sync-engine
- https://martin.kleppmann.com/2020/07/06/crdt-hard-parts-hydra.html
- https://news.ycombinator.com/item?id=44777086
