"""Proving ground #4 — MaStR (German Marktstammdatenregister, vision SCALE).

The vision-scale absolute-throughput run we kept flagging as UNMEASURED. Streams
the real German Marktstammdatenregister "Gesamtdatenexport" — public solar
generation units in Germany (millions of <EinheitSolar> records across 63 UTF-16
XML files, ~22 GB) — through datacrystal with iterparse + BATCHED commits, and
reports the headline: absolute ingest records/s on millions of real records, with
**peak RSS bounded by MASTR_BATCH, not the corpus** (proving the streaming /
batched-commit path holds at scale). Exercises the SOR/metadata persona (indexed
Bundesland/PLZ/Betriebsstatus queries) and the #13 multi-valued list index on the
genuinely comma-separated VerknuepfteEinheitenMaStRNummern of EEG-Biomasse plants.

On-demand eval, NOT a unit test (it ingests tens of GB). The data is LOCAL — point
MASTR_DIR at the unpacked Gesamtdatenexport directory. Tune with the env knobs:

    # full corpus (the multi-million headline; tens of GB, minutes):
    uv run python evals/proving_grounds/mastr.py
    # quick smoke run (a few hundred k):
    MASTR_MAX=500000 uv run python evals/proving_grounds/mastr.py
    # sweep the RAM-vs-batch lever (peak RSS must track the batch, not MAX):
    MASTR_BATCH=10000 MASTR_MAX=500000 uv run python evals/proving_grounds/mastr.py

Env: MASTR_DIR (export dir), MASTR_MAX (0 = full corpus), MASTR_BATCH (records per
commit, default 50000).

Source: Marktstammdatenregister (Bundesnetzagentur), Gesamtdatenexport. Licensed
under Datenlizenz Deutschland – Namensnennung – Version 2.0 (dl-de/by-2-0,
https://www.govdata.de/dl-de/by-2-0). Source attribution: "Bundesnetzagentur –
Marktstammdatenregister (MaStR)". Not endorsed by or affiliated with it.
"""

from __future__ import annotations

import gc
import os
import re
import resource
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import field
from pathlib import Path
from typing import Annotated, Iterator

import datacrystal as dc

MASTR_DIR = Path(os.environ.get(
    "MASTR_DIR", str(Path.home() / "Downloads" / "Gesamtdatenexport_20260612_26")))
DATA = Path(__file__).resolve().parent.parent / "data"
STORE = DATA / "mastr.store"
BIO_STORE = DATA / "mastr_biomass.store"
BATCH = int(os.environ.get("MASTR_BATCH", "50000"))
MAX = int(os.environ.get("MASTR_MAX", "0"))  # 0 = full corpus

# Bundesland catalog codes used by EinheitSolar (1400–1416; a DIFFERENT enum than
# the Marktakteure set). Just the few we print.
BUNDESLAND = {1403: "Bayern", 1409: "Nordrhein-Westfalen", 1408: "Niedersachsen"}
BETRIEB = {31: "In Planung", 35: "In Betrieb", 37: "Vorüb. stillgelegt", 38: "Endg. stillgelegt"}


@dc.entity
class SolarUnit:
    mastr_nr: Annotated[str, dc.Unique]                      # EinheitMastrNummer (SEE…)
    name: str = ""                                           # NameStromerzeugungseinheit
    bundesland: Annotated[int | None, dc.Index] = None       # Bundesland (1400–1416)
    betriebsstatus: Annotated[int | None, dc.Index] = None   # EinheitBetriebsstatus
    plz: Annotated[str | None, dc.Index] = None              # Postleitzahl
    ort: str = ""
    landkreis: str = ""
    bruttoleistung: float = 0.0                              # kW
    nettonennleistung: float = 0.0
    anzahl_module: int = 0
    inbetriebnahme: str | None = None                        # ISO date string
    eeg_nr: str | None = None                                # cross-ref (EEG…)
    betreiber_nr: str | None = None                          # cross-ref (ABR…)
    lokation_nr: str | None = None                           # cross-ref (SEL…)


@dc.entity
class EegBiomassePlant:
    eeg_nr: Annotated[str, dc.Unique]                        # EegMaStRNummer
    units: Annotated[list[str], dc.Index] = field(default_factory=list)  # multi-valued (#13)
    leistung: float = 0.0
    status: Annotated[int | None, dc.Index] = None


def peak_rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024 / 1024 if sys.platform == "darwin" else r / 1024  # bytes vs KB


def _int(v: str | None) -> int | None:
    return int(v) if v not in (None, "") else None


def _float(v: str | None) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _natural_key(p: Path) -> list[object]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


def stream_records(paths: list[Path], record_tag: str) -> Iterator[dict[str, str]]:
    """Yield one ``{field: text}`` dict per record element across the files,
    RAM-flat. ``iterparse`` honors each file's UTF-16 BOM; match by LOCAL name;
    ``elem.clear()`` releases the record and ``root.clear()`` drops processed
    siblings so the tree never grows past one record."""
    for path in paths:
        context = ET.iterparse(path, events=("end",))
        _, root = next(context)  # the container root, to prune its children
        for _event, elem in context:
            if elem.tag.rsplit("}", 1)[-1] == record_tag:
                yield {c.tag.rsplit("}", 1)[-1]: (c.text or "") for c in elem}
                elem.clear()
                root.clear()  # the load-bearing flat-RAM line


def to_unit(rec: dict[str, str]) -> SolarUnit:
    g = rec.get
    return SolarUnit(
        mastr_nr=g("EinheitMastrNummer") or "",
        name=g("NameStromerzeugungseinheit") or "",
        bundesland=_int(g("Bundesland")),
        betriebsstatus=_int(g("EinheitBetriebsstatus")),
        plz=g("Postleitzahl") or None,
        ort=g("Ort") or "",
        landkreis=g("Landkreis") or "",
        bruttoleistung=_float(g("Bruttoleistung")),
        nettonennleistung=_float(g("Nettonennleistung")),
        anzahl_module=_int(g("AnzahlModule")) or 0,
        inbetriebnahme=g("Inbetriebnahmedatum") or None,
        eeg_nr=g("EegMaStRNummer") or None,
        betreiber_nr=g("AnlagenbetreiberMastrNummer") or None,
        lokation_nr=g("LokationMaStRNummer") or None,
    )


def ingest_solar() -> tuple[int, float, float, float]:
    """Stream + batched-commit the solar units. Returns (n, seconds, MB on disk,
    peak RSS MB)."""
    files = sorted(MASTR_DIR.glob("EinheitenSolar_*.xml"), key=_natural_key)
    if not files:
        sys.exit(f"no EinheitenSolar_*.xml under {MASTR_DIR} — set MASTR_DIR")
    shutil.rmtree(STORE, ignore_errors=True)
    store = dc.Store.open(STORE)
    n = 0
    batch: list[SolarUnit] = []
    t0 = time.perf_counter()
    for rec in stream_records(files, "EinheitSolar"):
        if not rec.get("EinheitMastrNummer"):
            continue
        batch.append(to_unit(rec))
        if len(batch) >= BATCH:
            for u in batch:
                store.store(u)  # free-floating: not root-pinned (invariant 6)
            store.commit()
            n += len(batch)
            batch = []          # drop strong refs → the batch is now collectable
            gc.collect()        # force it, so live RAM stays ≈ one batch
            if MAX and n >= MAX:
                break
    if batch and not (MAX and n >= MAX):
        for u in batch:
            store.store(u)
        store.commit()
        n += len(batch)
    secs = time.perf_counter() - t0
    on_disk = sum(f.stat().st_size for f in STORE.rglob("*") if f.is_file()) / 1e6
    store.close()
    return n, secs, on_disk, peak_rss_mb()


def main() -> None:
    if not MASTR_DIR.exists():
        sys.exit(f"missing {MASTR_DIR} — download the MaStR Gesamtdatenexport "
                 "(see module docstring) and/or set MASTR_DIR")
    print(f"datacrystal proving ground: MaStR solar units  ({sys.platform})")
    print(f"  batch={BATCH:,}  max={MAX or 'full corpus':}\n")

    # --- INGEST (streaming + batched commits) --------------------------------
    n, secs, on_disk, rss = ingest_solar()
    print(f"ingested:              {n:>10,} solar units   {secs:7.2f}s  "
          f"({n / secs:,.0f} units/s)")
    print(f"  on disk:             {on_disk:>10.1f} MB   ({on_disk * 1e6 / n:.0f} B/unit)")
    print(f"  peak RSS:            {rss:>10.0f} MB   (the batch={BATCH:,} entity working set "
          "is bounded; the in-RAM")
    print("                                     unique/index maps are NOT — they grow with "
          "the corpus, invariant 11)")
    print(f"  → vision-scale absolute ingest number: {n / secs:,.0f} units/s "
          f"on {n:,} real records")
    gc.collect()

    # --- REOPEN (cold) -------------------------------------------------------
    t0 = time.perf_counter()
    s = dc.Store.open(STORE)
    total = s.count(SolarUnit)  # forces the one-time index build (scan)
    t_open = time.perf_counter() - t0
    print(f"\nreopened cold:         {total:>10,} units   {t_open:7.2f}s (open + index build)")

    # --- QUERY COST-LADDER (the SOR/metadata persona's whole surface) --------
    F = dc.fields(SolarUnit)

    def timed(fn):  # returns (result, seconds)
        t0 = time.perf_counter()
        r = fn()
        return r, time.perf_counter() - t0

    keys = s.pluck(SolarUnit, "mastr_nr")            # whole extent, decode-level
    _, t_get = timed(lambda: s.get(SolarUnit, mastr_nr=keys[0]))
    bayern, t_c1 = timed(lambda: s.count(F.bundesland == 1403))
    nrw, t_c2 = timed(lambda: s.count((F.bundesland == 1409) & (F.betriebsstatus == 35)))
    names, t_pl = timed(lambda: s.pluck(F.bundesland == 1403, "name"))
    _, t_hy = timed(lambda: s.query(F.bundesland == 1403, limit=100_000))
    big, t_rg = timed(lambda: s.count(F.bruttoleistung >= 1000.0))   # residual full scan

    print(f"\nquery cost-ladder on {total:,} units  (indexed: mastr_nr·bundesland·"
          "betriebsstatus·plz):")
    print(f"  get(mastr_nr)              unique lookup    {t_get * 1000:8.2f} ms   O(1)")
    print(f"  count(Bundesland==1403)    {bayern:>9,} hits  {t_c1 * 1000:8.2f} ms   bitmap")
    print(f"  count(NRW & In-Betrieb)    {nrw:>9,} hits  {t_c2 * 1000:8.2f} ms   bitmap AND")
    print(f"  pluck(==1403, name)        {len(names):>9,} hits  {t_pl * 1000:8.0f} ms   "
          "decode-level, O(hits)")
    print(f"  query(==1403, limit=100k)    100,000 live  {t_hy * 1000:8.0f} ms   "
          "hydrate, O(hits)")
    print("  ── GAP (#18): a range / non-indexed predicate has NO index → full scan ──")
    print(f"  count(Bruttoleistung>=1MW) {big:>9,} hits  {t_rg:8.2f} s    "
          f"scans all {total:,} (~{t_rg / max(t_c1, 1e-9):,.0f}x the indexed count)")
    print("  → range queries (leistung > X, registered between dates) are O(extent) "
          "today; see #18")

    # --- CORRECTNESS ---------------------------------------------------------
    one = s.get(SolarUnit, mastr_nr=keys[0])
    assert one is not None and one.mastr_nr == keys[0]
    via_query = s.query(F.bundesland == one.bundesland, limit=1) if one.bundesland else []
    if via_query:
        assert s.get(SolarUnit, mastr_nr=via_query[0].mastr_nr) is via_query[0], \
            "one instance per OID across paths"
    print("\ncorrectness: unique-key get ✓ · one-instance-per-OID across paths ✓")
    s.close()

    multivalued_biomass()


def multivalued_biomass() -> None:
    """#13 — the genuinely multi-valued VerknuepfteEinheitenMaStRNummern of
    EEG-Biomasse plants: a real comma-separated list[str], queried by element
    membership against a full-scan oracle."""
    bio = MASTR_DIR / "AnlagenEegBiomasse.xml"
    if not bio.exists():
        print("\n[#13] AnlagenEegBiomasse.xml not found — skipped")
        return
    shutil.rmtree(BIO_STORE, ignore_errors=True)
    store = dc.Store.open(BIO_STORE)
    plants: list[EegBiomassePlant] = []
    multi = 0
    for rec in stream_records([bio], "AnlageEegBiomasse"):
        eeg = rec.get("EegMaStRNummer")
        if not eeg:
            continue
        raw = rec.get("VerknuepfteEinheitenMaStRNummern", "")
        units = [u for u in raw.replace(" ", "").split(",") if u]
        if len(units) > 1:
            multi += 1
        plants.append(EegBiomassePlant(
            eeg_nr=eeg, units=units, leistung=_float(rec.get("InstallierteLeistung")),
            status=_int(rec.get("AnlageBetriebsstatus"))))
    for p in plants:
        store.store(p)
    store.commit()
    print(f"\n[#13] EEG-Biomasse plants: {len(plants):,} "
          f"({multi:,} with a MULTI-unit VerknuepfteEinheitenMaStRNummern list)")
    # pick a unit that appears in some plant's list, query by membership
    target = next((u for p in plants if p.units for u in p.units), None)
    if target is None:
        store.close()
        return
    F = dc.fields(EegBiomassePlant)
    t0 = time.perf_counter()
    hits = store.query(F.units.contains(target))  # multi-valued element membership (bitmap)
    t_q = (time.perf_counter() - t0) * 1000
    oracle = {p.eeg_nr for p in store.query(EegBiomassePlant) if target in p.units}
    assert {p.eeg_nr for p in hits} == oracle, "list-index membership must match the scan"
    print(f"  query(units contains {target}): {len(hits)} plant(s)  {t_q:.2f} ms "
          "— matches the full-scan oracle exactly ✓ (#13)")
    store.close()


if __name__ == "__main__":
    main()
