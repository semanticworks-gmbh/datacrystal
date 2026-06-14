"""Proving ground #1 — Gene Ontology (real knowledge-graph dataset).

Runs the *real* Gene Ontology (~38k terms, ~58k is_a edges, a deep polyhierarchy)
through datacrystal as a navigational object graph, and reports honest absolute
numbers — ingest throughput, reopen cost, on-demand lazy traversal, the
backlink-without-a-reverse-index cost (the #20 motivation), and the
hydrate-vs-decode ratio.

On-demand eval, NOT a unit test (it downloads + ingests tens of MB). Run it
during an evaluation phase:

    curl -sL --create-dirs -o evals/data/go-basic.obo \
      https://current.geneontology.org/ontology/go-basic.obo
    uv run python evals/proving_grounds/gene_ontology.py

(see evals/README.md for all proving grounds + fetch commands)

Gene Ontology is CC BY 4.0 (http://geneontology.org).
"""

from __future__ import annotations

import gc
import resource
import shutil
import sys
import time
from dataclasses import field
from pathlib import Path
from typing import Annotated

import datacrystal as dc
from datacrystal._entity import oid_of

DATA = Path(__file__).resolve().parent.parent / "data"
OBO = DATA / "go-basic.obo"
STORE = DATA / "go.store"
BIOLOGICAL_PROCESS = "GO:0008150"  # the root with the most descendants


@dc.entity
class Term:
    go_id: Annotated[str, dc.Unique]
    name: str
    namespace: Annotated[str | None, dc.Index] = None
    parents: list[dc.Lazy["Term"]] = field(default_factory=list)  # is_a — lazy adjacency (#30)


def peak_rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024 / 1024 if sys.platform == "darwin" else r / 1024  # bytes vs KB


def parse_obo(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    cur: dict | None = None

    def finalize(c: dict | None) -> None:
        if c and c.get("id") and not c["obsolete"]:
            out[c["id"]] = c

    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line == "[Term]":
                finalize(cur)
                cur = {"id": None, "name": "", "namespace": None, "is_a": [], "obsolete": False}
            elif line.startswith("["):  # [Typedef] / [Instance]
                finalize(cur)
                cur = None
            elif cur is not None and line:
                if line.startswith("id: "):
                    cur["id"] = line[4:].strip()
                elif line.startswith("name: "):
                    cur["name"] = line[6:].strip()
                elif line.startswith("namespace: "):
                    cur["namespace"] = line[11:].strip()
                elif line.startswith("is_a: "):
                    cur["is_a"].append(line[6:].split("!")[0].strip())
                elif line == "is_obsolete: true":
                    cur["obsolete"] = True
        finalize(cur)
    return out


def deepest_term(parsed: dict[str, dict]) -> tuple[str, int]:
    memo: dict[str, int] = {}

    def depth(gid: str) -> int:
        if gid in memo:
            return memo[gid]
        memo[gid] = 0  # cycle guard (GO is a DAG, but be safe)
        parents = [p for p in parsed[gid]["is_a"] if p in parsed]
        memo[gid] = 1 + max((depth(p) for p in parents), default=0)
        return memo[gid]

    best = max(parsed, key=depth)
    return best, memo[best]


def walk_ancestors(term: Term) -> tuple[int, int]:
    """Follow the lazy `parents` edges up to the roots, on demand. Returns
    (max depth, distinct terms touched)."""
    seen: set[str] = set()
    stack: list[tuple[Term, int]] = [(term, 0)]
    maxd = 0
    while stack:
        t, d = stack.pop()
        if t.go_id in seen:
            continue
        seen.add(t.go_id)
        maxd = max(maxd, d)
        for parent in t.parents:            # parent is a dc.Lazy[Term]
            stack.append((parent.get(), d + 1))  # .get() hydrates one term, on demand
    return maxd, len(seen)


def main() -> None:
    if not OBO.exists():
        sys.exit(f"missing {OBO} — download go-basic.obo first (see module docstring)")
    shutil.rmtree(STORE, ignore_errors=True)
    print(f"datacrystal proving ground: Gene Ontology  ({sys.platform})\n")

    t0 = time.perf_counter()
    parsed = parse_obo(OBO)
    t_parse = time.perf_counter() - t0
    print(f"parsed OBO:            {len(parsed):>8,} terms          {t_parse:6.2f}s")

    deep_id, _ = deepest_term(parsed)

    # --- INGEST ---------------------------------------------------------------
    t0 = time.perf_counter()
    store = dc.Store.open(STORE)
    terms = {gid: Term(go_id=gid, name=d["name"], namespace=d["namespace"])
             for gid, d in parsed.items()}
    edges = 0
    for gid, d in parsed.items():
        for pid in d["is_a"]:
            if pid in terms:  # drop edges to obsolete/absent parents (orphan refs)
                terms[gid].parents.append(dc.Lazy.of(terms[pid]))
                edges += 1
    for t in terms.values():
        store.store(t)  # free-floating: not pinned to root, so memory is bounded on reopen
    store.root = {"source": "Gene Ontology go-basic", "terms": len(terms)}
    store.commit()
    t_ingest = time.perf_counter() - t0
    on_disk = sum(f.stat().st_size for f in STORE.rglob("*") if f.is_file()) / 1e6
    rss_ingest = peak_rss_mb()
    print(f"ingested:              {len(terms):>8,} terms, {edges:,} edges   "
          f"{t_ingest:6.2f}s  ({len(terms) / t_ingest:,.0f} terms/s)")
    print(f"  on disk:             {on_disk:>8.1f} MB   "
          f"peak RSS {rss_ingest:.0f} MB   ({on_disk * 1e6 / len(terms):.0f} B/term on disk)")
    store.close()
    del terms, parsed
    gc.collect()

    # --- REOPEN (cold) --------------------------------------------------------
    t0 = time.perf_counter()
    s = dc.Store.open(STORE)
    n = s.count(Term)  # forces the one-time index build (a backend scan)
    t_open = time.perf_counter() - t0
    print(f"\nreopened cold:         {n:>8,} terms          {t_open:6.2f}s "
          "(open + index build)")

    # --- LAZY ON-DEMAND TRAVERSAL (#29 + #30 on real data) --------------------
    gc.collect()
    term = s.get(Term, go_id=deep_id)
    assert term is not None
    assert all(isinstance(p, dc.Lazy) for p in term.parents), "parents must reload lazy (#30)"
    t0 = time.perf_counter()
    depth, touched = walk_ancestors(term)
    t_walk = (time.perf_counter() - t0) * 1000
    print(f"\nancestor walk of the deepest term ({deep_id}, {term.name!r}):")
    print(f"  depth {depth}, {touched} terms hydrated on demand   {t_walk:6.1f} ms")
    gc.collect()
    print(f"  live entities now:   {len(s._registry):>8,}  "  # pyright: ignore[reportPrivateUsage]
          f"(of {n:,} — lazy adjacency kept the rest off the RAM budget)")

    # --- BACKLINK WITHOUT A REVERSE INDEX (the #20 motivation) ---------------
    t0 = time.perf_counter()
    children: dict[int, list[Term]] = {}
    for t in s.query(Term):  # full-extent scan — the unavoidable cost without #20
        for p in t.parents:
            children.setdefault(p.oid, []).append(t)  # p.oid: parent OID, no .get()
    t_scan = time.perf_counter() - t0
    bp = s.get(Term, go_id=BIOLOGICAL_PROCESS)
    assert bp is not None
    bp_oid = oid_of(bp)
    assert bp_oid is not None
    seen: set[int] = set()
    frontier: list[int] = [bp_oid]
    while frontier:
        oid = frontier.pop()
        for child in children.get(oid, ()):
            coid = oid_of(child)
            if coid is not None and coid not in seen:
                seen.add(coid)
                frontier.append(coid)
    print(f"\nbacklink 'all descendants of {bp.name}': {len(seen):,} terms")
    print(f"  cost today (full edge scan, no reverse index): {t_scan:6.2f}s   "
          "→ exactly what #20 incoming() turns into an index lookup")
    del children  # free the reverse map so the hydration timing below is cold
    gc.collect()

    # --- SAME BACKLINK VIA THE REVERSE INDEX (#20 incoming()) -----------------
    t0 = time.perf_counter()
    direct = s.incoming(bp)  # first call builds the global reverse index (one scan)
    t_build = time.perf_counter() - t0
    seen2: set[int] = set()
    frontier_t: list[Term] = [bp]
    t0 = time.perf_counter()
    while frontier_t:
        node = frontier_t.pop()
        for child in s.incoming(node):  # an index lookup now, not a scan
            coid = oid_of(child)
            if coid is not None and coid not in seen2:
                seen2.add(coid)
                frontier_t.append(child)
    t_bfs = time.perf_counter() - t0
    assert seen2 == seen, "incoming() must answer the SAME set as the full scan"
    cc = s.get(Term, go_id="GO:0005575")  # cellular_component
    t0 = time.perf_counter()
    cc_children = s.incoming(cc) if cc is not None else []
    t_reuse = (time.perf_counter() - t0) * 1000
    print("\nsame backlink via incoming() (#20 reverse index):")
    print(f"  build (first call, one scan): {t_build:5.2f}s   {len(direct):,} direct children")
    print(f"  transitive descendants via incoming(): {len(seen2):,}  {t_bfs:5.2f}s   "
          "— matches the full scan exactly ✓")
    print(f"  a SECOND, unrelated backlink reuses the built index: "
          f"{len(cc_children):,} direct children in {t_reuse:.2f} ms (no re-scan)")
    gc.collect()

    # --- HYDRATE vs DECODE (the ~24x ratio on real data) ----------------------
    gc.collect()
    t0 = time.perf_counter()
    names_decode = s.pluck(Term, "name")  # decode-level, no entities
    t_pluck = time.perf_counter() - t0
    t0 = time.perf_counter()
    hydrated = s.query(Term)  # build every live entity
    t_hydrate = time.perf_counter() - t0
    print(f"\nfull extent ({len(names_decode):,} terms):")
    print(f"  pluck   (decode-level): {t_pluck:6.2f}s")
    print(f"  query   (hydrated):     {t_hydrate:6.2f}s   "
          f"({t_hydrate / t_pluck:.1f}x — use pluck/[arrow] for analytics)")

    # --- CORRECTNESS: identity across two paths -------------------------------
    a = s.get(Term, go_id=deep_id)
    assert a is not None
    via_query = next(x for x in hydrated if x.go_id == deep_id)
    assert a is via_query, "identity broken: one OID must be one live instance"
    print("\ncorrectness: one instance per OID across paths ✓ · "
          "graph reopened + traversed without RecursionError ✓")
    s.close()


if __name__ == "__main__":
    main()
