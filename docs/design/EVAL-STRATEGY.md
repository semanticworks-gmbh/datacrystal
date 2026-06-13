# Eval Strategy — proving grounds & the autonomous feedback loop

> **Status: SPIKE / proposal (2026-06-13), not yet ratified.** Direction & boundaries still
> defer to [VISION.md](VISION.md) / [ROADMAP.md](ROADMAP.md). This doc proposes *how we keep the
> library honest against its vision while developing — ideally autonomously, without drift.*

## TL;DR

The best issues in the backlog (11 carry `eval-feedback`: #13, #14, #15, #16, #18, #29, #30 …)
came from running **real workloads**, not from reading the code. That loop works — but it lives
*outside the repo*, runs by hand, and reaches the backlog only because a human re-types findings
into issues. **This proposes making the frontier sensor a first-class, in-repo, repeatable
harness — the "proving grounds" — and curating a portfolio of real datasets that challenge the
vision from every side.**

The principle the maintainer set: **real datasets define the *shape*; a fast synthetic generator
reproduces that shape for unit/fitness tests; the real datasets run as the "wild" proving
grounds.** Unit tests stay fast and focused — but their cardinalities, fan-out, depth, and
cycle-rate stop being toy-shaped.

**The single loudest finding from the dataset survey:** three independent facets
(corporate-ownership graphs, software-dependency graphs, GitHub event graphs) **converge on the
same break** — the navigational graph model is *unusable on real data today* because of
**#29** (deep/cyclic eager reads `RecursionError`) and **#30** (`list[Lazy[T]]` reloads eager, so
opening one node drags its whole reachable component into RAM and onto the C-stack). These are
**load-bearing, not corner cases.** Fixing them first unlocks the entire digital-twin persona;
**#20** (reverse-ref) then turns the backlink queries from O(edges) scans into index lookups.

## The three feedback layers

| Layer | Role | Status |
|---|---|---|
| **Correctness ratchet** — `tests/` + `tests/fitness/` (mineral cabinet, both backends, invariants) | pins what's *correct* | ✅ strong |
| **Shape ratchet** — perf gates over `benchmarks/_gen.py` (Zipf hubs + cycles, same-run ratios) | pins memory / op-count *shape* | ✅ strong |
| **Frontier sensor** — real-workload proving grounds | finds what the lib *can't do* on real data | ⚠️ external/ad-hoc → **this doc** |

Ratchets stop regressions. The frontier sensor sets direction. We have two; we're formalizing the third.

## The loop (and why it's safe to run autonomously)

```
SENSE   run the proving grounds → works / friction / broken
TRIAGE  Gandalf scores each finding vs VISION persona + ROADMAP scope (in/out)
REFINE  ground + reproduce against live code (see #29/#30 refinement)
BUILD   implement → the scenario's oracle BECOMES a regression test
RATCHET fitness/perf gate locks the win → re-run → next frontier
```

Four forces keep an autonomous agent on-track:

- **Compass = VISION + ROADMAP.** Every pulled item must trace to a persona *and* be in-scope
  (the `Punted`/`Never` lists are the anti-drift guardrail). Builds the *right* thing.
- **Ratchet = the gates.** Correctness / shape / memory can't regress (CI-enforced).
- **Brake = `needs-owner-decision`.** Genuine forks (design rulings, anything touching the frozen
  API, scope changes) stop and wait for the owner. (#30 sits here now.)
- **Frontier = the proving grounds.** Always surfaces the next *real* gap, so work is pulled by
  the vision's workloads, not pushed by what's locally convenient.

The loop runs end-to-end only for in-scope, no-ruling, gate-verifiable work. Correctness and perf
findings are machine-detectable (crash, wrong result, RAM blow-up); **DX friction** ("this was
awkward to write") needs a periodic judgment pass (owner or LLM-as-eval). Build the objective half
first.

## The dataset portfolio (real, open, multi-sided — grounded 2026-06-13)

One top pick per facet, each chosen for **accessibility × adversarial-shape × vision-relevance**.
Synthetic-generator params let a *fast* fitness test reproduce the shape without the real download.

| Facet (persona) | Top pick | License · scale | Challenges (what would break/slow) | → generator shape |
|---|---|---|---|---|
| **Knowledge-graph / org-digital-twin** | **GLEIF "Who Owns Whom"** (LEI L1+L2) | CC0 · ~3M entities, ~650K edges, ~35 MB | **#20** hub backlinks ("all subsidiaries of HSBC"), **#29** deep+cyclic ownership closure, **#30** `subsidiaries: list[Lazy]`; ~20% dangling parents (ADR-003); **3×/day delta feed = deltalog replay**; versioned RR-CDF (#26) | Zipf ultimate-parent in-degree s≈1.1; chain depth median 3 / p95 8; cycle ~0.1%; 20% dangling |
| **Dependency / software graph** (the #29 reproducer) | **deps.dev** (npm/Go, by Google) | CC-BY 4.0 · BigQuery 1 TB/mo free or public API · carve any size | **literal #29** (npm/Go graphs are *cyclic* → fix must be iterative **and** cycle-aware), **#30** hub closure too big to hydrate eager, **#20** 1e4–1e5 dependents = densest reverse bitmap; ships `MinimumDepth` + inverted `Dependents` | depth 30–100+, cycle-rate npm-realistic, Zipf s≈1.2 |
| **SOR / metadata at scale** | **MaStR** (energy registry) | DL-DE-BY-2.0 · 22M entries · ~240 MB ZIP · *already our wild probe* | **#13** `market_actors_and_roles` multi-valued (extent-scan without the inverted index), **#26** revision-numbered exports = real schema drift, ADR-003 deleted-units tables; the f(hits)-not-f(extent) gate under real load | 22M rows, multi-valued roles, 2 schema versions |
| **Search (FTS)** | **MIRACL German** + arXiv | Apache-2.0 / CC0 · 15.9M passages + **305 judged queries / 10.5k qrels**; arXiv 1.7M abstracts / 4.3 GB | **real relevance oracle** ("search X → judged-Y in top N"); German exercises Snowball-de + fold-stem symmetry (a stemming bug = measurable recall loss); arXiv = single-writer index-build throughput | skewed multi-label + compound-word/fold-variant prose + sparse qrels |
| **Timeseries** (#18) | **ENTSO-E load/gen** (+ London smart-meter for CI-size) | CC-BY-4.0 · tens of M points (ENTSO-E) / 167 M (London) | **#18** sorted time + numeric range index — every "load between t0..t1" / "MW>X" is a full residual scan today; DST / out-of-range timestamps are correctness traps | regular grid, per-zone cadence, numeric range |
| **TB-scale + schema-evolution** | **GH Archive** (+ **Backblaze Drive Stats** for #26, **Common Crawl** as the *boundary* litmus) | open · multi-TB hourly HTTPS (turnkey **#16** bootstrap) | GH Archive cross-stresses #29/#30/#13/#20/#26 at TB scale; **Backblaze 40→62 widening columns = the cleanest real "data follows code" (#26)**; **Common Crawl = the litmus that the engine must *defer* to DuckDB** (proves the "never grow an optimizer" boundary) | heterogeneous events, widening schema, Zipf hubs |

**Secondary picks worth keeping:** Gene Ontology (CC-BY, deepest real polyhierarchy — best *fast*
#29 fitness corpus), Open Ownership BODS (edge-as-record #20/#30, higher cycle rate),
MusicBrainz (CC0 — documented v30 migrations incl. `tracks→recordings` demote and
`country-string→area-reference` **retype**: the case that *productively breaks* additive evolution
and pinpoints where #26 needs an explicit `migrate`), NYC TLC (larger-than-RAM #16), Software
Heritage (millions-deep Merkle DAG — extreme #29 depth).

## What the portfolio *proves* (new signal beyond the current backlog)

1. **#29 + #30 are the gate to the whole graph persona** — confirmed independently by GLEIF,
   deps.dev, and GH Archive. The graph model is unusable on real data until both land. This
   empirically ranks them above new graph *features*: a model that crashes beats a model that's
   missing a feature only in the wrong direction.
2. **#26 "data follows code" has a real edge** — MusicBrainz retype + Backblaze widening are
   dated, non-synthetic schema changes. The widening case validates additive type-lineage; the
   **retype/demote case will break it on purpose** and show exactly where the `migrate`/glue
   sub-stories (#26 b/c) are mandatory, not optional. That's a finding, not a failure.
3. **Common Crawl is the boundary test** — it exists to be scanned columnar; trying to serve it
   from the engine is the anti-pattern the architecture already forbids. A *negative* proving
   ground that proves `explain()`'s deliberate two-rule limit (defer to the `[arrow]`/DuckDB tier).
4. **GLEIF/MaStR delta feeds are ready-made replay corpora** for `datacrystal.deltalog` (item 23,
   shipped) — real change-feeds, not synthetic.

## Sized story set (the spike's deliverable)

| # | Story | Type · concerns | Notes |
|---|---|---|---|
| E1 | **Proving-grounds harness** — `evals/` runner; each scenario emits a `works / friction / broken` report; ❌/⚠️ → draft `eval-feedback` issues | Story · 3c | reuses `store_factory`; the SENSE step |
| E2 | **Dataset profiler → generator params** — profile a sampled real dataset, extract shape (Zipf s, depth dist, cycle-rate, multi-valued, schema versions), feed `benchmarks/_gen.py` | Story · 3c | the "real defines shape" bridge; keeps unit tests fast |
| E3 | **Proving-ground #1: graph / digital-twin** (GLEIF-shaped, laptop slice) — oracles **double as the #29/#30 regression tests** | Story · 3c | fails first → motivates the fixes; passes after |
| E4 | **Schema-evolution replay corpus** (MusicBrainz/Backblaze) — the #26 `migrate`/`verify` test bed | Story · 2c | pulls when #26 b/c are pulled |
| E5 | Per-facet scenarios (SOR/MaStR · search/MIRACL · timeseries/ENTSO-E · TB/GH Archive · boundary/Common Crawl) | Epic — just-in-time | one issue each when pulled |

**Recommended order, driven by the convergence finding:**
fix **#29** (iterative read path) + **#30** (bless `list[Lazy[T]]`) → build **E3** (GLEIF proving
ground validates the digital-twin persona end-to-end) → **#20** (reverse-ref turns the backlinks
into index lookups) → E1/E2 harness generalizes it to the other facets.

## Decisions for the owner

1. **Synthetic-only, or also a wild dataset in CI?** Recommend: **layered** — synthetic
   (mineral-cabinet, shape-matched to real) is the deterministic, CI-gateable ratchet; one real
   dataset (GLEIF first; it's laptop-scale + CC0) runs as a *manual/nightly* wild probe. Synthetic
   *gates*; wild *explores*.
2. **Autonomy boundary** — confirm the loop may run SENSE→BUILD→RATCHET unattended for in-scope,
   no-ruling, gate-verifiable work, and must stop at forks / scope / the frozen API.
3. **Where the harness lives** — `evals/` (proposed), separate from `tests/` (fast/focused) and
   `benchmarks/` (perf gates).
