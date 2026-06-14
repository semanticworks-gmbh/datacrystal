"""Proving ground #3 — deps.dev (real, CYCLIC software-dependency graph).

Runs real resolved dependency graphs from deps.dev (Google's Open Source
Insights) through datacrystal as a navigational object graph. Its distinctive
job — the one Gene Ontology (a DAG) and GLEIF (a near-acyclic ownership tree)
could not do — is to prove the graph read-path on **genuine cycles**: npm ships
circular runtime dependencies, so a resolved closure like ``gulp@4.0.2`` contains
real reference cycles (es5-ext 3-cycles, the resolve-dir↔global-modules 2-cycle).
This is the literal reproducer for the bug #29 fixed: a cyclic object graph that
commits fine must also reopen and traverse fine, with no ``RecursionError`` and
one-instance-per-OID identity preserved *through the cycle*.

It also exercises #30 (``list[Lazy]`` adjacency keeps the closure off the RAM
budget) and #20 (``incoming()`` = "packages that depend on X", the densest
real-world reverse bitmap).

Fetches the keyless deps.dev REST API on demand (BFS over a small seed set) and
**caches every response into the git-ignored ``evals/data/``**, so re-runs touch
no network. On-demand eval, NOT a unit test. Run it during an evaluation phase:

    uv run python evals/proving_grounds/deps_dev.py

Dependency data from deps.dev (Open Source Insights), Google LLC, licensed under
CC-BY 4.0 (https://creativecommons.org/licenses/by/4.0/).
"""

from __future__ import annotations

import gc
import json
import resource
import shutil
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import field
from pathlib import Path
from typing import Annotated, Any

import datacrystal as dc
from datacrystal._entity import oid_of

DATA = Path(__file__).resolve().parent.parent / "data"
STORE = DATA / "deps_dev.store"
API = "https://api.deps.dev/v3"

# Seeds chosen (verified live) to guarantee real cycles + a fan-in hub in a
# tractable fetch: gulp's closure alone carries 7 cycles; the rest add depth,
# shared nodes, and reverse-bitmap density for incoming().
SEEDS = [
    ("npm", "gulp", "4.0.2"),         # 333 nodes / 576 edges, 7 cycles
    ("npm", "liftoff", "2.5.0"),
    ("npm", "grunt", "1.6.1"),
    ("npm", "resolve-dir", "1.0.1"),  # the clean resolve-dir↔global-modules 2-cycle
    ("npm", "react", "18.2.0"),
    ("npm", "express", "4.18.2"),
    ("npm", "eslint", "8.57.0"),
    ("npm", "webpack", "5.90.0"),
    ("npm", "jest", "29.7.0"),
]

Key = tuple[str, str, str]  # (system, name, version)


@dc.entity
class PackageVersion:
    coordinate: Annotated[str, dc.Unique]  # "system/name/version" — the natural key
    system: Annotated[str, dc.Index]
    name: Annotated[str, dc.Index]
    version: str
    dependencies: list[dc.Lazy["PackageVersion"]] = field(default_factory=list)  # the adjacency


def peak_rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024 / 1024 if sys.platform == "darwin" else r / 1024  # bytes vs KB


def fetch_dependencies(system: str, name: str, version: str) -> dict[str, Any]:
    """The resolved dependency graph for one (system,name,version) — one request
    returns the whole closure (nodes + integer-indexed edges). Cached to disk."""
    safe = f"{system}-{name.replace('/', '_')}-{version}"
    cache = DATA / f"depsdev-{safe}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    url = (f"{API}/systems/{system}/packages/"
           f"{urllib.parse.quote(name, safe='')}/versions/{version}:dependencies")
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (trusted host)
        data = json.loads(resp.read())
    DATA.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


def find_cycle(adj: dict[int, list[int]]) -> list[int] | None:
    """One real cycle in the dependency graph (iterative DFS, recursion-stack
    coloring) — returns ``[a, b, …, a]`` or None. Iterative so the detector
    itself can't overflow on the deep graph it's inspecting."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(adj, WHITE)
    for root in adj:
        if color[root] != WHITE:
            continue
        stack = [(root, iter(adj[root]))]
        path = [root]
        color[root] = GRAY
        while stack:
            _, it = stack[-1]
            for nxt in it:
                if color.get(nxt) == GRAY:                 # back-edge → cycle
                    return path[path.index(nxt):] + [nxt]
                if color.get(nxt, BLACK) == WHITE:
                    color[nxt] = GRAY
                    path.append(nxt)
                    stack.append((nxt, iter(adj[nxt])))
                    break
            else:
                color[path.pop()] = BLACK
                stack.pop()
    return None


def walk_closure(start: PackageVersion) -> tuple[int, int]:
    """Follow the lazy ``dependencies`` edges over the whole reachable closure,
    on demand, visited-guarded. Returns (max depth, distinct nodes touched).
    Completes on a cyclic graph precisely because the guard + the registry's
    one-instance-per-OID break the cycle."""
    seen: set[int] = set()
    stack: list[tuple[PackageVersion, int]] = [(start, 0)]
    maxd = 0
    while stack:
        pv, d = stack.pop()
        oid = oid_of(pv)
        if oid is None or oid in seen:
            continue
        seen.add(oid)
        maxd = max(maxd, d)
        for dep in pv.dependencies:        # dep is a dc.Lazy[PackageVersion]
            stack.append((dep.get(), d + 1))  # .get() hydrates one node, on demand
    return maxd, len(seen)


def main() -> None:
    shutil.rmtree(STORE, ignore_errors=True)
    print(f"datacrystal proving ground: deps.dev dependency graph  ({sys.platform})\n")

    # --- FETCH + PARSE (cached) -----------------------------------------------
    t0 = time.perf_counter()
    nodes: dict[Key, PackageVersion] = {}
    edges: set[tuple[Key, Key]] = set()
    for system, name, version in SEEDS:
        graph = fetch_dependencies(system, name, version)
        local: list[Key] = []
        for n in graph["nodes"]:
            vk = n["versionKey"]
            key = (vk["system"], vk["name"], vk["version"])
            local.append(key)
            if key not in nodes:  # global dedup → shared deps become one identity (cycles close)
                nodes[key] = PackageVersion(coordinate="/".join(key),
                                            system=key[0], name=key[1], version=key[2])
        for e in graph["edges"]:
            fk, tk = local[e["fromNode"]], local[e["toNode"]]
            if fk != tk:
                edges.add((fk, tk))
    t_fetch = time.perf_counter() - t0
    print(f"fetched {len(SEEDS)} resolved graphs (cached): "
          f"{len(nodes):>6,} distinct package-versions, {len(edges):,} edges  {t_fetch:6.2f}s")

    # --- INGEST ---------------------------------------------------------------
    t0 = time.perf_counter()
    store = dc.Store.open(STORE)
    for fk, tk in edges:
        nodes[fk].dependencies.append(dc.Lazy.of(nodes[tk]))
    for pv in nodes.values():
        store.store(pv)  # free-floating → bounded RAM on reopen
    store.root = {"source": "deps.dev resolved graphs", "seeds": len(SEEDS)}
    store.commit()
    t_ingest = time.perf_counter() - t0
    on_disk = sum(f.stat().st_size for f in STORE.rglob("*") if f.is_file()) / 1e6
    print(f"\ningested:              {len(nodes):>6,} nodes, {len(edges):,} edges   "
          f"{t_ingest:6.2f}s  ({len(nodes) / t_ingest:,.0f} nodes/s)")
    print(f"  on disk:             {on_disk:>6.2f} MB   peak RSS {peak_rss_mb():.0f} MB")
    store.close()
    del nodes
    gc.collect()

    # --- REOPEN (cold) --------------------------------------------------------
    t0 = time.perf_counter()
    s = dc.Store.open(STORE)
    n = s.count(PackageVersion)
    t_open = time.perf_counter() - t0
    print(f"\nreopened cold:         {n:>6,} nodes           {t_open:6.2f}s "
          "(open + index build)")

    # --- THE CYCLE PROOF (the deps.dev-specific result, #29) ------------------
    adj: dict[int, list[int]] = {}
    for pv in s.query(PackageVersion):
        oid = oid_of(pv)
        if oid is not None:
            adj[oid] = [d.oid for d in pv.dependencies]
    cycle = find_cycle(adj)
    assert cycle is not None, "the seeds guarantee a real dependency cycle"
    cyc = s.get_many(cycle[:-1])
    names = " → ".join(f"{p.name}@{p.version}" for p in cyc) + f" → {cyc[0].name}@{cyc[0].version}"
    print(f"\nreal dependency CYCLE survived persist + reopen ({len(cycle) - 1} nodes):")
    print(f"  {names}")
    # identity through the cycle: follow the edges around and land on the SAME instance
    cur = cyc[0]
    for nxt_oid in cycle[1:]:
        cur = next(d.get() for d in cur.dependencies if d.oid == nxt_oid)
    assert cur is cyc[0], "one-instance-per-OID must hold THROUGH a cycle"
    print("  followed the cycle back to the SAME live instance ✓ (identity through a cycle)")

    # --- LAZY CLOSURE WALK (#29 iterative + #30 lazy, on a cyclic graph) -------
    gc.collect()
    gulp = s.get(PackageVersion, coordinate="NPM/gulp/4.0.2")
    assert gulp is not None
    t0 = time.perf_counter()
    depth, touched = walk_closure(gulp)
    t_walk = (time.perf_counter() - t0) * 1000
    print(f"\nlazy closure walk from gulp@{gulp.version} (a cyclic graph):")
    print(f"  depth {depth}, {touched} nodes hydrated on demand   {t_walk:6.1f} ms "
          "— no RecursionError ✓")
    print(f"  live entities now:   {len(s._registry):>6,}  "  # pyright: ignore[reportPrivateUsage]
          f"(of {n:,} — lazy adjacency kept the rest off the RAM budget)")

    # --- incoming() = DEPENDENTS, proven == full scan (#20) -------------------
    in_degree: dict[int, int] = {}
    t0 = time.perf_counter()
    for oid, deps in adj.items():
        for d in deps:
            in_degree[d] = in_degree.get(d, 0) + 1
    t_scan = time.perf_counter() - t0
    hub_oid = max(in_degree, key=in_degree.__getitem__)
    oracle = {oid for oid, deps in adj.items() if hub_oid in deps}
    hub = s.get_many([hub_oid])[0]
    print(f"\nmost-depended-upon node {hub.name}@{hub.version}: {len(oracle):,} direct dependents")
    print(f"  full edge scan (no reverse index): {t_scan * 1000:6.1f} ms")
    t0 = time.perf_counter()
    dependents = s.incoming(hub)  # first call builds the global reverse index
    t_build = (time.perf_counter() - t0) * 1000
    assert {oid_of(x) for x in dependents} == oracle, "incoming() must match the full scan"
    other = s.get_many([next(iter(oracle))])[0]
    t0 = time.perf_counter()
    _ = s.incoming(other)
    t_reuse = (time.perf_counter() - t0) * 1000
    print(f"  via incoming() (#20): {len(dependents):,} dependents, built in {t_build:.1f} ms "
          "— matches the full scan exactly ✓")
    print(f"  a SECOND backlink reuses the built index in {t_reuse:.2f} ms (no re-scan)")

    # --- HYDRATE vs DECODE ----------------------------------------------------
    gc.collect()
    t0 = time.perf_counter()
    names_decode = s.pluck(PackageVersion, "name")
    t_pluck = time.perf_counter() - t0
    t0 = time.perf_counter()
    hydrated = s.query(PackageVersion)
    t_hydrate = time.perf_counter() - t0
    print(f"\nfull extent ({len(names_decode):,} nodes):")
    print(f"  pluck   (decode-level): {t_pluck * 1000:6.1f} ms")
    print(f"  query   (hydrated):     {t_hydrate * 1000:6.1f} ms   "
          f"({t_hydrate / t_pluck:.1f}x)")

    a = s.get(PackageVersion, coordinate="NPM/gulp/4.0.2")
    via_query = next(x for x in hydrated if x.name == "gulp")
    assert a is via_query, "identity broken: one OID must be one live instance"
    print("\ncorrectness: one instance per OID across paths ✓ · "
          "cyclic graph reopened + traversed without RecursionError ✓")
    s.close()


if __name__ == "__main__":
    main()
