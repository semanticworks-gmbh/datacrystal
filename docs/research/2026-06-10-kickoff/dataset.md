# Canonical example domain + validation dataset

> Workflow runs wf_41ceea42-869 (resumed), 2026-06-10/11. Summary: Recommends the Wikidata mineral graph (CC0, verified) plus a deterministic synthetic collector layer ("the mineral cabinet") as datacrystal's canonical example domain: 6,285 mineral species, 1,337 type localities, and ~150 countries vendored as a <4 MB msgpack snapshot, with synthetic Specimen and frozen CatalogEvent entities supplying dense numerics, prose, and the append-only mode. Live SPARQL verification exposed sparse Wikidata physical numerics (Mohs only 276/6,285), which the synthetic layer fixes by design. MovieLens was rejected on its verified share-alike/non-commercial redistribution clause, Gutenberg/Open Library on unverifiable licensing; fallback is the same-vocabulary pure-synthetic generator (Chinook, verified MIT, as real-data alternate but FTS-disqualified), and the generator scales the identical domain to the 1M-object benchmark preset.

# Canonical example domain & validation dataset — datacrystal first runnable version

## Requirements recap

One domain must serve the README quickstart, the demo app, and the fixture every integration test loads. It must be object-graph-shaped — ≥3 entity types with real cross-references, ≥1 `Lazy[T]` single ref and ≥1 list-of-refs one-to-many — because identity, lazy hydration, and traversal are the engine's whole point. It must vendor into the repo at ≤~5 MB under a license verified to permit redistribution; carry text fields (the late-v0.x FTS5 watermark harness needs prose), numeric plus low-cardinality categorical fields (pyroaring bitmap facets + Condition AST), and a plausible future embedding use; and a natural unique secondary key (ROADMAP item 4) plus an append-only entity for `@entity(frozen=True)`.

## Candidate table

| Domain | Source | License / redistribution / verification | Size | Graph shape | Text / facets / vector | Unique key | Frozen entity |
|---|---|---|---|---|---|---|---|
| **1. Wikidata minerals** (recommended) | WDQS SPARQL snapshot | **CC0 1.0** ("All structured data … under the Creative Commons CC0 License"). **VERIFIED** ([Wikidata:Licensing](https://www.wikidata.org/wiki/Wikidata:Licensing)); counts live-verified via WDQS 2026-06-11 | est. 1.5–2.5 MB msgpack (+optional 1–1.5 MB CC BY-SA extracts) | Mineral→Locality→Country + reverse list-of-refs; 4,219 minerals fan into 1,337 localities (avg 3.2, heavy-tailed) | Thin core descriptions + 1,733 enwiki extracts; crystal system (80%, 8 values), IMA status (~100%); WD numerics sparse (Mohs 4%) → fixed by Specimen layer; name/notes embeddings | Wikidata QID (`"Q43010"`) | synthetic `CatalogEvent` |
| 2. MovieLens ml-latest-small | grouplens.org | "may redistribute … under these same license conditions"; **commercial use requires permission**. **VERIFIED** ([README](https://files.grouplens.org/datasets/movielens/ml-latest-small-README.html)). Vendorable but license-incompatible inside a permissive repo | ~1 MB | Weak: Movie hub + Rating/Tag spokes via bare userId — no deep traversal | Titles/tags thin; genres facet good; rating numerics | movieId/imdbId | Rating |
| 3. Chinook | lerocha/chinook-database | **MIT** ("… distribute, sublicense, and/or sell"). **VERIFIED** ([LICENSE.md](https://github.com/lerocha/chinook-database)) | ~1 MB | Good: Artist→Album→Track→Genre; Customer→Invoice→Line | **Zero prose — cannot validate FTS**; genre/country facets fine; no plausible vectors | Customer.email | Invoice/Line |
| 4. Gutenberg / Open Library books | gutenberg.org feeds; OL dumps | PG catalog "freely available" but no explicit license statement located; OL dumps page shows no license, only archive.org ToS link. **UNVERIFIED — disqualifying** | dumps 0.5–12 GB; heavy subsetting | Good (Author→Work→Subject) | Good text; dates/subjects facets | gutenberg id / OL key | Edition |
| 5. Pure synthetic | generator only | No license. N/A | 0 (code only) | Whatever we design | Template text only; no real-data edge cases (Unicode, gaps); zero README credibility | designed | designed |

## Recommendation: Wikidata mineral graph + deterministic synthetic collection layer ("the mineral cabinet")

Thematically self-documenting for **datacrystal** (crystal systems inside a database named datacrystal), the only candidate with explicit public-domain redistribution, and a real graph. Live-verified coverage (WDQS, 2026-06-11): **6,285** mineral species (`P31=Q12089225`); formula P274 90%; crystal system P556 80%; type locality P2695 67% → **1,337** distinct localities, 1,294 with country P17; named-after P138 78%; IMA status P579 ~100%. Weaknesses found by verification: physical numerics are sparse (Mohs 276, streak 639, density 89, color 92) and WD descriptions are one-liners — answered by the Specimen layer (dense numerics, generated prose) and an optional CC BY-SA extracts file (1,733 minerals have enwiki articles).

### Acquisition recipe (one-time, scripted, re-runnable)

1. Three WDQS queries (page with `LIMIT 2000 OFFSET n`; label service for en labels): **minerals** — `?m wdt:P31 wd:Q12089225` with OPTIONAL P274 formula, P556 crystal system, P579 IMA status, P1088 Mohs, P2695 type locality, en description + `skos:altLabel` aliases; **localities** — ids from query 1: label, P17 country, P625 coords; **countries** — label, P297 ISO-2.
2. Normalize → msgspec-msgpack snapshot vendored at `/Users/sh/pyrsistance/data/minerals/`: `minerals.msgpack`, `localities.msgpack`, `countries.msgpack`, the three `.sparql` files, `regenerate.py`, `LICENSE` (CC0 notice + snapshot date).
3. Optional, separate file so the core snapshot stays pure CC0: Wikipedia REST `/page/summary` intros for the 1,733 sitelinked minerals → `extracts.msgpack` + `ATTRIBUTION.md` (CC BY-SA 4.0).
4. Specimens/events are **never vendored** — generated deterministically (`seed=42`) by the same generator the benchmarks use.

### Entity model sketch

`@dc.entity` applies `@dataclass(slots=True, weakref_slot=True)` per DESIGN.md; `dc.Unique` = the ROADMAP-item-4 unique secondary-key marker (name TBD).

```python
import datacrystal as dc
from datacrystal import Lazy
from typing import Annotated
from datetime import date, datetime

@dc.entity
class Country:
    qid: Annotated[str, dc.Unique]            # "Q183"
    name: str
    iso2: str | None = None

@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]
    name: str
    country: Lazy[Country] | None             # single lazy ref
    type_minerals: list[Lazy["Mineral"]]      # one-to-many (avg 3.2; hot spots: Tsumeb)

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]            # natural key: upsert-by-QID
    name: Annotated[str, dc.Index]
    formula: str | None                       # IMA formula, P274 (90%)
    crystal_system: Annotated[str | None, dc.Index]   # 8 values -> bitmap facet
    ima_status: Annotated[str | None, dc.Index]       # ~6 values
    mohs: Annotated[float | None, dc.Index]   # sparse (276/6285): Optional showcase
    description: Annotated[str, dc.FullText]  # WD description+aliases (+opt. extract)
    type_locality: Lazy[Locality] | None = None       # single lazy ref

@dc.entity
class Specimen:                               # synthetic collection layer
    catalog_no: Annotated[str, dc.Unique]     # "DC-000042"
    mineral: Lazy[Mineral]
    mass_g: Annotated[float, dc.Index]        # dense numeric range queries
    quality: Annotated[str, dc.Index]         # {A,B,C,D}
    acquired: date
    notes: Annotated[str, dc.FullText]        # template prose; later ≥2 @Vector fields

@dc.entity(frozen=True)
class CatalogEvent:                           # append-only provenance log
    specimen: Lazy[Specimen]
    kind: Annotated[str, dc.Index]            # acquired/loaned/sold/relabeled
    at: datetime
    note: str
```

Requirement mapping: lazy single refs `Specimen.mineral`, `Mineral.type_locality`; list-of-refs `Locality.type_minerals`; unique index `qid`/`catalog_no`; bitmap facets at ideal cardinalities (8/6/4/~150 country); dense `mass_g` + sparse `mohs`; FTS on `notes`+`description`; future `notes_vec`+`photo_vec` on Specimen satisfies the ≥2-@Vector-fields SDA requirement; frozen `CatalogEvent`.

**Counts/size**: vendored 6,285 + 1,337 + ~150 entities ≈ 1.5–2.5 MB msgpack (+ optional extracts ≈ 1–1.5 MB; total < 4 MB; estimates). Demo/test default generates 2,000 Specimen + ~2,400 CatalogEvent → ~12k objects: bitmap indexes visibly beat scans, setup stays sub-second.

## Fallback

Candidate 5 folded into the recommendation: if WDQS snapshotting stalls, ship the **generator only**, seeded with a ~200-mineral embedded vocabulary (mineral names and crystal systems are facts, CC0 anyway) — same entity model, same tests, zero license risk; only README "real data" credibility is lost. Chinook (MIT, verified) is the named real-data alternate, but its prose-free fields cannot validate the FTS watermark harness — the deciding defect.

## Scaling into the perf generator

The vendored real data **is** the generator's vocabulary, so README demo, integration tests, FTS harness, and benchmarks share one language. The real entities are the fixed low-cardinality backbone (already at natural maximum: ~6.3k minerals, 1.3k localities, ~150 countries); scale comes from the collection layer: specimens sampled Zipf-over-minerals with hot localities (realistic bitmap AND/OR skew), events = 1 `acquired` + Poisson(0.2) extras per specimen. **1M-object preset ≈ 7.8k real backbone + 450k Specimen + ~540k CatalogEvent.** Knobs: `--specimens`, `--events-mean`, `--seed`; presets `tiny` (~12k, demo/tests), `bench` (1M), `stress` (5M — the positioning envelope's edge). The same Condition AST queries run unchanged at every scale (`(Specimen.quality == 'A') & (Mineral.crystal_system == 'cubic')`, `mass_g` ranges); frozen events exercise append-only commit throughput; `notes` templates feed FTS and, later, embedding benchmarks.
