# The name: datacrystal

Decided 2026-06-10. Proposed by Sven; vetted against PyPI/GitHub/web the same day.

## Origin

The founding image: we **capture our live objects and data inside a crystal** and preserve them —
perfectly, transparently, indefinitely. Like an inclusion in amber or a crystal, the object is
not transformed, mapped, or flattened on the way in; it is kept exactly as it lived, visible
through the material, recoverable at any time.

The previous working title, *pyrsistance*, was abandoned because it is near-homophonous with
`pyrsistent` (Tobias Gustafsson's immutable-collections library, ~41.7M downloads/month) —
the stress test rated that collision "cheapest fatal-if-ignored item on the list"
([STRESS-TEST.md](STRESS-TEST.md)).

## Why the metaphor earns its keep

It is not just imagery — each association maps onto a real feature of the engine:

| Crystal property | datacrystal feature |
|---|---|
| **Transparent** — you see the object inside, unchanged | *Transparent persistence* (the actual technical term); zero-copy Arrow/DuckDB views of live data |
| **Crystallize** — the fluid, living thing solidifies into a permanent ordered structure | `commit()`: the mutable object graph becomes a durable record, structure intact (no ORM impedance mismatch) |
| **Lattice** — an ordered, repeating structure of connected nodes | the object graph itself: typed entities connected by references |
| **Facets** — polished surfaces you view the crystal through | indexes and views; *faceted search* is an established IR term → natural API vocabulary (`store.facet(...)`) |
| **Growth by accretion** — layer upon layer, never rewriting what's below | append-only storage, the EclipseStore-inherited write model |
| **Inclusions** — objects trapped and preserved perfectly inside | stored entities, kept byte-faithful with identity preserved |
| **Frozen** | immutable watermark snapshots (`store.snapshot()`, ADR-001) |
| **Geode** — plain rock outside, crystals inside | the store directory: unassuming files containing the whole preserved graph |

Free cultural resonance: "data crystal" is an established sci-fi trope for *perfect, eternal
knowledge storage* (Babylon 5 data crystals, holocrons, Superman's memory crystals) — exactly the
promise of the product, pre-installed in the audience's imagination.

## Availability audit (2026-06-10)

| Check | Result |
|---|---|
| PyPI `datacrystal` | **free** — reserve before any public artifact |
| PyPI `data-crystal` | **free** (distinct normalized name — reserve both to prevent confusion-squatting) |
| GitHub org/user `datacrystal` | taken; `data-crystal` org is free |
| Existing products | "Data Crystal" ROM-hacking wiki (datacrystal.tcrf.net) — niche, unrelated domain, no library/PyPI presence; low risk |

Known caveats, judged acceptable: **Crystal is a programming language** (crystal-lang), so always
use the full compound — never shorten to "crystal"; some search noise from crystallography
databases and the materials-science Python ecosystem (pymatgen et al.); a faint healing-crystals
connotation, negligible among developers.

## Runners-up (all PyPI-free on 2026-06-10, kept as fallbacks)

`heartwood` (durable core of a tree), `objarium`/`objectarium` ("-arium" = a place where things
are kept alive), `speicher` (German: RAM *and* storehouse), `rootstore`, `crystalline`,
`vitrify` (vitrification = real-world preservation of living tissue in glass — deepest metaphor,
but obscure), `clathrate`, `krystallos`.

## Action items

- [ ] Reserve `datacrystal` and `data-crystal` on PyPI (minimal placeholder release) before
      anything public.
- [ ] Decide GitHub home: `data-crystal` org vs `datacrystal` repo under the personal account.
- Naming conventions going forward: package/import `datacrystal`; prose "DataCrystal" or
  "datacrystal"; extension packages `datacrystal-rdf`, extras `datacrystal[fts]` etc.;
  never abbreviate to "crystal".
