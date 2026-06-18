# datacrystal — hero image

The project hero, shown at the top of the README (and in `docs/design/NAME.md`) via a `<picture>`
element that serves the small AVIF with a PNG fallback.

## Files

- **`hero.avif`** — web-delivery version (~143 KB, AVIF q65). Served first via `<picture>`/`<source>`
  so READMEs and docs load fast and cheap. ~18× smaller than the PNG, visually indistinguishable.
- **`hero.png`** — the lossless master and the `<img>` fallback (for the rare browser without AVIF).
  This is the file to **edit** for the brand hex pass below; re-export the AVIF afterwards with:
  `avifenc -q 65 -s 4 --jobs all hero.png hero.avif`

**The idea** is documented beside the name, in
[`docs/design/NAME.md` → The hero image](../docs/design/NAME.md#the-hero-image): the picture
visualizes datacrystal's founding metaphor — unstructured data on the left **crystallizes** into
an ordered, faceted data crystal, the facets carrying SemanticWorks' Information Tetrahedron
(navy / blue / teal).

## Concept

"Crystallization" — concept #2 of the hero workshop (2026-06-18). Chosen over the iconic
single-tetrahedron "Seed Cell" and the "Bedrock foundation" variants because it best tells the
story a newcomer needs in one glance: *your messy, living data becomes a durable, ordered
structure — without being flattened or translated.*

## Reproduction

- **Tool:** the SemanticWorks brand illustration pipeline — Flux 2 Pro via
  `generate.py` in the corporate-identity repo (Black Forest Labs API).
- **Format:** `hero` preset, 1920 × 1088.
- **Seed:** `201` (a variant roll of the original seed `22`).
- **Prompt:**

  > Ultra-wide architectural blueprint on a clean off-white background (#F8F9FA) with a faint
  > drafting grid and a strong left-to-right narrative flow. LEFT EDGE: scattered fragmented
  > documents, invoices and unstructured data streams in warm graphite and sepia pencil — sketchy,
  > incomplete, chaotic raw data. The streams sweep rightward and SNAP into order across a
  > transition zone marked by fine dashed arcs. CENTER-RIGHT: from a single seed point an ordered
  > crystalline lattice grows — many small flat low-poly tetrahedra with flat solid navy (#000080),
  > medium blue (#256dbd) and vivid teal (#3ab0a2) facets, crisp edges, flat 2D not 3D, interlocking
  > into a faceted data crystal that becomes more structured and confident toward the right. One
  > emerging facet in flat copper-amber (#B8600F). Fine construction lines and concentric arcs tie
  > the raw side to the crystallized side. Visual weight in the upper two-thirds, composition fades
  > to off-white at the bottom with a minimal baseline. A tiny human silhouette for scale. Absolutely
  > no text, no lettering, no labels, no watermarks. No photorealistic 3D rendering, no shadows, no
  > gradients on facets, no neon. High resolution.

## Post-processing (before any polished or public use)

Per the SemanticWorks guideline (*"Nie unbearbeitet veröffentlichen"* — never publish unedited),
pull the AI output to exact brand hex in an editor before treating it as final:

- [ ] Navy facets → `#000080` (the raw output leans slightly royal-blue)
- [ ] Confirm a distinct middle-blue face → `#256dbd`
- [ ] Teal facets → `#3ab0a2`
- [ ] Copper-amber accent → `#B8600F`
- [ ] Background → off-white `#F8F9FA`
- [ ] Remove any AI artifacts / stray marks

The committed `hero.png` is the unedited generation — a deliberate placeholder ("for now"),
pending the hex pass.

## Alternatives kept

Other strong candidates from the same workshop live in the corporate-identity illustrations repo
(`illustrations/datacrystal/`): the iconic **Seed Cell** unit-cell-in-a-lattice mark
(`hero-1-seedcell-v2-103`) and the **Bedrock** image of datacrystal as the load-bearing
foundation beneath the semantic layer (`hero-4-bedrock-303`).
