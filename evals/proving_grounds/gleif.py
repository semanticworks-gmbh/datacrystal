"""Proving ground #2 — GLEIF (real legal-entity ownership graph).

Runs the *real* GLEIF Level-2 relationship golden copy (the "who owns whom" of
~3.1M legal entities worldwide) through datacrystal as a navigational object
graph, and reports honest absolute numbers for the **system-of-record /
organizational-digital-twin persona** — ingest throughput, reopen cost,
on-demand lazy ownership-chain traversal, and the headline: ``incoming()`` on a
real **Zipf-hub** distribution ("every entity consolidated under this parent"),
proven correct against a full scan. A handful of ultimate parents (global banks,
fund managers) consolidate thousands of subsidiaries — the single best
real-world validator for the reverse-reference index (#20).

The relationship layer is small (~650k edges, ~23 MB zipped), so this is a
self-contained run from one download. Level-1 attribute enrichment (legal names,
jurisdictions, the ~3M-entity multi-GB *scale* ingest) is optional — drop the
``lei2`` golden copy next to the ``rr`` one and it is streamed in too.

On-demand eval, NOT a unit test (it downloads + ingests tens of MB). Run it
during an evaluation phase:

    curl -sL -o evals/data/gleif-rr.csv.zip \\
      "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/rr/latest.csv"
    # optional Level-1 enrichment + the vision-scale ingest (~466 MB zip):
    # curl -sL -o evals/data/gleif-lei2.csv.zip \\
    #   "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/lei2/latest.csv"
    uv run python evals/proving_grounds/gleif.py

Contains LEI data from the GLEIF Global LEI Index (https://www.gleif.org), made
available under CC0 1.0. Not endorsed by or affiliated with GLEIF.
"""

from __future__ import annotations

import csv
import gc
import io
import os
import resource
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Annotated, Iterator

import datacrystal as dc
from datacrystal._entity import oid_of

DATA = Path(__file__).resolve().parent.parent / "data"
RR = DATA / "gleif-rr.csv.zip"      # Level 2 — relationship records (who owns whom)
LEI2 = DATA / "gleif-lei2.csv.zip"  # Level 1 — entity attributes (optional enrichment)
STORE = DATA / "gleif.store"

DIRECT = "IS_DIRECTLY_CONSOLIDATED_BY"
ULTIMATE = "IS_ULTIMATELY_CONSOLIDATED_BY"
# Cap the Level-1 enrichment stream (0 = unlimited); the ownership graph itself
# is always ingested in full from the small RR file.
LEI2_CAP = int(os.environ.get("GLEIF_LEI2_CAP", "0"))


@dc.entity
class LegalEntity:
    lei: Annotated[str, dc.Unique]                       # 20-char ISO 17442 key
    legal_name: str = ""                                 # from Level 1 (optional)
    jurisdiction: Annotated[str | None, dc.Index] = None  # ISO code (optional)
    direct_parent: dc.Lazy["LegalEntity"] | None = None    # closest consolidating parent
    ultimate_parent: dc.Lazy["LegalEntity"] | None = None  # top-of-tree parent


def peak_rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024 / 1024 if sys.platform == "darwin" else r / 1024  # bytes vs KB


def _csv_rows(zip_path: Path) -> Iterator[list[str]]:
    """Stream the single CSV member of a GLEIF golden-copy .zip row-by-row,
    never extracting the (multi-GB, for Level 1) file to disk."""
    with zipfile.ZipFile(zip_path) as zf:
        member = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(member) as raw:
            yield from csv.reader(io.TextIOWrapper(raw, encoding="utf-8"))


def stream_edges(zip_path: Path) -> Iterator[tuple[str, str, str]]:
    """Yield ``(child_lei, parent_lei, rel_type)`` for every ACTIVE direct/ultimate
    consolidation edge. GLEIF semantics: the edge is directed CHILD → PARENT —
    StartNode is the child, EndNode the parent."""
    rows = _csv_rows(zip_path)
    header = next(rows)
    col = {name: i for i, name in enumerate(header)}
    ci = col["Relationship.StartNode.NodeID"]
    pi = col["Relationship.EndNode.NodeID"]
    ti = col["Relationship.RelationshipType"]
    si = col["Relationship.RelationshipStatus"]
    for row in rows:
        if row[si] != "ACTIVE":
            continue
        rtype = row[ti]
        if rtype == DIRECT or rtype == ULTIMATE:
            yield row[ci], row[pi], rtype


def stream_level1() -> Iterator[tuple[str, str, str | None]]:
    """Yield ``(lei, legal_name, jurisdiction)`` from the Level-1 golden copy, if
    present. Columns resolved by header name (the wide CSV's width drifts)."""
    rows = _csv_rows(LEI2)
    header = next(rows)
    col = {name: i for i, name in enumerate(header)}
    li, ni = col["LEI"], col["Entity.LegalName"]
    ji = col.get("Entity.LegalJurisdiction")
    n = 0
    for row in rows:
        yield row[li], row[ni], (row[ji] if ji is not None else None)
        n += 1
        if LEI2_CAP and n >= LEI2_CAP:
            return


def chain_len(lei: str, direct: dict[str, str]) -> int:
    """Length of the direct-parent ownership ladder above ``lei`` (visited-guarded
    against cycles — ownership should be a DAG, but be safe)."""
    seen: set[str] = set()
    d = 0
    cur: str | None = lei
    while cur is not None and cur in direct and cur not in seen:
        seen.add(cur)
        cur = direct[cur]
        d += 1
    return d


def deepest_chain(direct: dict[str, str]) -> str:
    """The child at the foot of the longest direct-parent ownership ladder."""
    return max(direct, key=lambda lei: chain_len(lei, direct))


def walk_to_ultimate(entity: LegalEntity) -> tuple[int, int]:
    """Follow the lazy ``direct_parent`` edges up the ownership ladder, on demand.
    Returns (chain depth, distinct entities touched)."""
    seen: set[int] = set()
    depth = 0
    cur: LegalEntity | None = entity
    while cur is not None:
        oid = oid_of(cur)
        if oid is None or oid in seen:
            break
        seen.add(oid)
        nxt = cur.direct_parent          # a dc.Lazy[LegalEntity] | None
        cur = nxt.get() if nxt is not None else None  # .get() hydrates one entity
        if cur is not None:
            depth += 1
    return depth, len(seen)


def main() -> None:
    if not RR.exists():
        sys.exit(f"missing {RR} — download the GLEIF rr golden copy first "
                 "(see module docstring)")
    shutil.rmtree(STORE, ignore_errors=True)
    print(f"datacrystal proving ground: GLEIF legal-entity ownership  ({sys.platform})\n")

    # --- PARSE EDGES ----------------------------------------------------------
    t0 = time.perf_counter()
    direct: dict[str, str] = {}
    ultimate: dict[str, str] = {}
    leis: set[str] = set()
    for child, parent, rtype in stream_edges(RR):
        leis.add(child)
        leis.add(parent)
        (direct if rtype == DIRECT else ultimate)[child] = parent
    t_parse = time.perf_counter() - t0
    n_edges = len(direct) + len(ultimate)
    print(f"parsed RR edges:       {n_edges:>9,} active edges     {t_parse:6.2f}s")
    print(f"  distinct entities:   {len(leis):>9,}  "
          f"({len(direct):,} direct, {len(ultimate):,} ultimate consolidations)")

    deep_lei = deepest_chain(direct)

    # --- INGEST ---------------------------------------------------------------
    t0 = time.perf_counter()
    store = dc.Store.open(STORE)
    ent = {lei: LegalEntity(lei=lei) for lei in leis}
    enriched = 0
    if LEI2.exists():
        for lei, name, juris in stream_level1():
            e = ent.get(lei)
            if e is not None:
                e.legal_name = name
                e.jurisdiction = juris
                enriched += 1
    for child, parent in direct.items():
        ent[child].direct_parent = dc.Lazy.of(ent[parent])
    for child, parent in ultimate.items():
        ent[child].ultimate_parent = dc.Lazy.of(ent[parent])
    for e in ent.values():
        store.store(e)  # free-floating: not root-pinned, so reopen RAM is bounded
    store.root = {"source": "GLEIF RR golden copy", "entities": len(ent)}
    store.commit()
    t_ingest = time.perf_counter() - t0
    on_disk = sum(f.stat().st_size for f in STORE.rglob("*") if f.is_file()) / 1e6
    rss = peak_rss_mb()
    print(f"\ningested:              {len(ent):>9,} entities, {n_edges:,} edges  "
          f"{t_ingest:6.2f}s  ({len(ent) / t_ingest:,.0f} entities/s)")
    print(f"  on disk:             {on_disk:>9.1f} MB   peak RSS {rss:.0f} MB   "
          f"({on_disk * 1e6 / len(ent):.0f} B/entity on disk)"
          + (f"   [{enriched:,} enriched from Level 1]" if enriched else ""))
    print(f"  → first vision-scale absolute ingest number: "
          f"{len(ent) / t_ingest:,.0f} entities/s on a real {len(ent):,}-node graph")
    store.close()
    del ent
    gc.collect()

    # --- REOPEN (cold) --------------------------------------------------------
    t0 = time.perf_counter()
    s = dc.Store.open(STORE)
    n = s.count(LegalEntity)  # forces the one-time index build (a backend scan)
    t_open = time.perf_counter() - t0
    print(f"\nreopened cold:         {n:>9,} entities         {t_open:6.2f}s "
          "(open + index build)")

    # --- LAZY OWNERSHIP-CHAIN WALK (#29 + #30 on real data) -------------------
    gc.collect()
    deep = s.get(LegalEntity, lei=deep_lei)
    assert deep is not None
    assert deep.direct_parent is None or isinstance(deep.direct_parent, dc.Lazy)
    t0 = time.perf_counter()
    depth, touched = walk_to_ultimate(deep)
    t_walk = (time.perf_counter() - t0) * 1000
    print(f"\nownership-ladder walk of the deepest chain ({deep_lei}):")
    print(f"  depth {depth}, {touched} entities hydrated on demand   {t_walk:6.1f} ms")
    print(f"  live entities now:   {len(s._registry):>9,}  "  # pyright: ignore[reportPrivateUsage]
          f"(of {n:,} — lazy parents kept the rest off the RAM budget)")

    # --- THE HUB BACKLINK: incoming() on a real Zipf distribution (#20) -------
    # Who consolidates the most subsidiaries? (a global bank / fund manager.)
    ref_counts: dict[int, int] = {}
    t0 = time.perf_counter()
    for e in s.query(LegalEntity):  # full-extent scan — the cost WITHOUT a reverse index
        for parent in (e.direct_parent, e.ultimate_parent):
            if parent is not None:
                ref_counts[parent.oid] = ref_counts.get(parent.oid, 0) + 1
    t_scan = time.perf_counter() - t0
    hub_oid = max(ref_counts, key=ref_counts.__getitem__)
    hub_edges = ref_counts[hub_oid]  # direct + ultimate edges (counts multiplicity)
    hub = s.get_many([hub_oid])[0]
    # full-scan oracle: every DISTINCT entity whose direct OR ultimate parent is the
    # hub (an entity consolidated both directly AND ultimately is ONE referrer).
    oracle: set[int] = set()
    for e in s.query(LegalEntity):
        eo = oid_of(e)
        for parent in (e.direct_parent, e.ultimate_parent):
            if parent is not None and parent.oid == hub_oid and eo is not None:
                oracle.add(eo)
    print(f"\nbiggest consolidator (hub) {hub.lei}: {len(oracle):,} distinct subsidiaries "
          f"({hub_edges:,} consolidation edges — many are both directly & ultimately "
          "consolidated by it)")
    print(f"  full edge scan (no reverse index): {t_scan:6.2f}s   "
          "→ exactly what #20 incoming() turns into an index lookup")
    del ref_counts
    gc.collect()

    # --- SAME BACKLINK VIA THE REVERSE INDEX (#20 incoming()) -----------------
    t0 = time.perf_counter()
    subs = s.incoming(hub)  # first call builds the global reverse index (one scan)
    t_build = time.perf_counter() - t0
    via_index = {oid_of(x) for x in subs}
    assert via_index == oracle, "incoming() must answer the SAME set as the full scan"
    # a SECOND, unrelated backlink reuses the built index (no re-scan)
    other = s.get_many([next(iter(oracle))])[0]  # some subsidiary
    t0 = time.perf_counter()
    _ = s.incoming(other)
    t_reuse = (time.perf_counter() - t0) * 1000
    print("\nsame backlink via incoming() (#20 reverse index):")
    print(f"  build (first call, one scan): {t_build:5.2f}s   {len(subs):,} subsidiaries "
          "— matches the full scan exactly ✓")
    print(f"  a SECOND, unrelated backlink reuses the built index in {t_reuse:.2f} ms "
          "(no re-scan)")

    # --- HYDRATE vs DECODE ----------------------------------------------------
    gc.collect()
    t0 = time.perf_counter()
    leis_decode = s.pluck(LegalEntity, "lei")  # decode-level, no entities
    t_pluck = time.perf_counter() - t0
    t0 = time.perf_counter()
    hydrated = s.query(LegalEntity)  # build every live entity
    t_hydrate = time.perf_counter() - t0
    print(f"\nfull extent ({len(leis_decode):,} entities):")
    print(f"  pluck   (decode-level): {t_pluck:6.2f}s")
    print(f"  query   (hydrated):     {t_hydrate:6.2f}s   "
          f"({t_hydrate / t_pluck:.1f}x — use pluck/[arrow] for analytics)")

    # --- CORRECTNESS: identity across two paths -------------------------------
    a = s.get(LegalEntity, lei=deep_lei)
    assert a is not None
    via_query = next(x for x in hydrated if x.lei == deep_lei)
    assert a is via_query, "identity broken: one OID must be one live instance"
    print("\ncorrectness: one instance per OID across paths ✓ · "
          "ownership graph reopened + traversed without RecursionError ✓")
    s.close()


if __name__ == "__main__":
    main()
