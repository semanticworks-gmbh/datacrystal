# Proving grounds — real-dataset evals

datacrystal's **proving grounds** run real external datasets through the library and report
honest absolute numbers — throughput, latency, peak RSS, and correctness against *known-true*
answers. They are the frontier sensor of the eval loop; see
[`docs/design/EVAL-STRATEGY.md`](../docs/design/EVAL-STRATEGY.md) for the strategy and a run-log
of what each has proven.

**They are NOT unit tests.** They download and ingest tens of MB to multi-GB, so they live
*outside* the fast `pytest` suite and run **on demand**, never in CI. (A real dataset's *shape* —
fan-out, depth, cycles — is distilled into `benchmarks/_gen.py` so the unit/fitness tests stay
mineral-cabinet-fast and toy-free.)

## What is in version control

Only the **scripts** (`proving_grounds/*.py`) and this README. The datasets and the stores they
build are **git-ignored** — see [`.gitignore`](.gitignore): `data/` and `*.store/`. The repo never
carries a big dataset. Fetch the data into `evals/data/` with the commands below; re-running a
proving ground rebuilds its `.store/` from scratch.

## Reproduce

Run from the repo root. Each command downloads one dataset into `evals/data/` (`--create-dirs`
makes the folder on a fresh clone), then runs its proving ground.

### #1 — Gene Ontology · knowledge-graph polyhierarchy · CC-BY 4.0 · ~31 MB

```bash
curl -sL --create-dirs -o evals/data/go-basic.obo \
  https://current.geneontology.org/ontology/go-basic.obo
uv run python evals/proving_grounds/gene_ontology.py
```

### #2 — GLEIF · legal-entity ownership (SOR / org-digital-twin persona) · CC0 1.0 · ~23 MB

```bash
curl -sL --create-dirs -o evals/data/gleif-rr.csv.zip \
  "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/rr/latest.csv"
uv run python evals/proving_grounds/gleif.py
```

Optional Level-1 enrichment + the (multi-GB) vision-scale ingest — drop `gleif-lei2.csv.zip`
next to the RR file and it is streamed in too:

```bash
curl -sL --create-dirs -o evals/data/gleif-lei2.csv.zip \
  "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/lei2/latest.csv"   # ~466 MB
```

### #3 — deps.dev · CYCLIC software-dependency graph (the #29 reproducer) · CC-BY 4.0

Unlike #1/#2 this one **fetches its own data** from the keyless deps.dev REST API (a BFS over a
small npm seed set) and caches every response into `evals/data/` — no manual download, and
re-runs touch no network:

```bash
uv run python evals/proving_grounds/deps_dev.py
```

### #4 — MaStR (German Marktstammdatenregister) · SOR/metadata at vision SCALE · dl-de/by-2.0

The only **local** dataset — the Gesamtdatenexport is portal-download only (no URL). Point
`MASTR_DIR` at the unpacked export directory; tune the run with `MASTR_MAX` (0 = full corpus,
~22 GB / millions) and `MASTR_BATCH` (records/commit, the RAM-vs-batch lever):

```bash
MASTR_DIR=/path/to/Gesamtdatenexport_* MASTR_MAX=500000 \
  uv run python evals/proving_grounds/mastr.py   # quick (a few hundred k)
MASTR_DIR=/path/to/Gesamtdatenexport_* \
  uv run python evals/proving_grounds/mastr.py   # full corpus (tens of GB, minutes)
```

### #5 — BEIR / MIRACL · full-text search with a RELEVANCE oracle · CC-BY-SA / Apache-2.0

The first ground with real relevance judgments (qrels), so it measures **ranking quality**
(nDCG@10 / precision@k / nDCG against human judgments), not just throughput. Needs the `fts`
extra. Default = BEIR NFCorpus (tiny, English, densely judged):

```bash
curl -sL --create-dirs -o evals/data/nfcorpus.zip \
  https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip
(cd evals/data && unzip -o nfcorpus.zip)
uv run --extra fts python evals/proving_grounds/search.py
# German Snowball stemming + scale (BEIR-formatted MIRACL-de dir):
# SEARCH_DIR=miracl-de SEARCH_LANG=german QRELS=dev uv run --extra fts python evals/proving_grounds/search.py
```

### #6 — Blob store · real PDFs/documents (enterprise-search + SOR-archive persona) · local

Like MaStR, this one is **local-first** — point `BLOB_DIR` at a directory of documents you have
(the persona is literally "your invoice/scan archive"). It proves the two blob claims (ADR-007):
the object table stays flat no matter how many GB of PDF you store, and streamed write/read keep
peak RSS far below the bytes. Correctness oracle: every blob's sha256 round-trips.

```bash
BLOB_DIR=/path/to/your/pdfs uv run python evals/proving_grounds/blob_store.py
# knobs: BLOB_GLOB='**/*.pdf' (default) · BLOB_MAX=0 (all) · BLOB_CHUNK=1048576
```

No corpus handy? Any folder of files works (PDFs are ideal — multi-MB, real shape). A quick
public-domain set, e.g. a few NASA technical reports (US-gov, public domain):

```bash
mkdir -p evals/data/pdfs && cd evals/data/pdfs
for id in 19950020935 19930091059 20040031234; do
  curl -sL -o "$id.pdf" "https://ntrs.nasa.gov/api/citations/$id/downloads/$id.pdf"
done
cd - && BLOB_DIR=evals/data/pdfs uv run python evals/proving_grounds/blob_store.py
```

## Attribution / licenses

- Gene Ontology — CC-BY 4.0, http://geneontology.org
- GLEIF LEI data — CC0 1.0, https://www.gleif.org. Not endorsed by or affiliated with GLEIF.
- deps.dev (Open Source Insights), Google LLC — CC-BY 4.0, https://deps.dev
- MaStR (Marktstammdatenregister, Bundesnetzagentur) — dl-de/by-2.0, https://www.govdata.de/dl-de/by-2-0. Not endorsed by or affiliated with the Bundesnetzagentur.
- BEIR (NFCorpus etc.) — CC-BY-SA-4.0, https://github.com/beir-cellar/beir. MIRACL — Apache-2.0, https://github.com/project-miracl/miracl.

Both datasets are free to redistribute, but we do **not** commit them — keep them in the
git-ignored `evals/data/`.
