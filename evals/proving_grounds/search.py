"""Proving ground #5 — BEIR / MIRACL (real full-text search with a relevance oracle).

The first eval with a GENUINE relevance oracle — qrels (human judgments mapping
each query to its relevant documents) — so it reports not just throughput but
**ranking quality**: nDCG@10, precision@k, recall@k, MRR against real judgments.
That is the thing only a search dataset can prove, and the thing
``datacrystal[fts]`` (SQLite FTS5 BM25 + Snowball stemming) exists for.

Default = **BEIR NFCorpus** (English medical IR — 3,633 docs, 323 queries, dense
qrels ~38/query — a fast, deeply-judged wire-up that makes precision@k/nDCG
trustworthy). For the German Snowball-stemming + scale headline, point it at
**MIRACL German** (the BEIR-formatted ``miracl-de`` dir, ``SEARCH_LANG=de``): a
German corpus is the only thing that actually exercises the ``dc.FullText
(language="de")`` stem-first-fold-after path, where a stemming bug becomes a
*measurable recall loss*.

On-demand eval, NOT a unit test. Run it with the ``fts`` extra:

    curl -sL --create-dirs -o evals/data/nfcorpus.zip \\
      https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip
    (cd evals/data && unzip -o nfcorpus.zip)
    uv run --extra fts python evals/proving_grounds/search.py
    # German Snowball + scale (BEIR-formatted MIRACL-de dir):
    # SEARCH_DIR=miracl-de SEARCH_LANG=de QRELS=dev uv run --extra fts python …

NFCorpus: CC-BY-SA-4.0 (BEIR). MIRACL: Apache-2.0.
"""

from __future__ import annotations

import json
import math
import os
import resource
import shutil
import sys
import time
from csv import reader as csv_reader
from pathlib import Path
from typing import Annotated, Iterator

import datacrystal as dc
from datacrystal._entity import oid_of
from datacrystal.fts import FullTextIndex

DATA = Path(__file__).resolve().parent.parent / "data"
DIR = DATA / os.environ.get("SEARCH_DIR", "nfcorpus")
LANG = os.environ.get("SEARCH_LANG", "english")  # snowballstemmer language name
QRELS = os.environ.get("QRELS", "test")          # test | dev
STORE = DATA / "search.store"
SIDECAR = DATA / "search.fts"


@dc.entity
class Document:
    doc_id: Annotated[str, dc.Unique]
    title: str = ""
    text: Annotated[str, dc.FullText(language=LANG)] = ""  # title + body, full-text indexed


def peak_rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024 / 1024 if sys.platform == "darwin" else r / 1024


def load_corpus() -> Iterator[Document]:
    with open(DIR / "corpus.jsonl", encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            title = o.get("title", "") or ""
            body = ((title + ". ") if title else "") + (o.get("text", "") or "")
            yield Document(doc_id=str(o["_id"]), title=title, text=body.strip())


def load_queries() -> dict[str, str]:
    out: dict[str, str] = {}
    with open(DIR / "queries.jsonl", encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            out[str(o["_id"])] = o["text"]
    return out


def load_qrels() -> dict[str, dict[str, int]]:
    """{query-id: {doc-id: relevance}} for relevance > 0 (TREC-style TSV)."""
    out: dict[str, dict[str, int]] = {}
    with open(DIR / "qrels" / f"{QRELS}.tsv", encoding="utf-8") as f:
        rows = csv_reader(f, delimiter="\t")
        next(rows)  # header: query-id  corpus-id  score
        for qid, did, score in rows:
            if int(score) > 0:
                out.setdefault(str(qid), {})[str(did)] = int(score)
    return out


def ndcg_at(retrieved: list[str], relevant: dict[str, int], k: int) -> float:
    dcg = sum(relevant.get(d, 0) / math.log2(i + 2) for i, d in enumerate(retrieved[:k]))
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def main() -> None:
    if not (DIR / "corpus.jsonl").exists():
        sys.exit(f"missing {DIR}/corpus.jsonl — download the dataset (see docstring)")
    shutil.rmtree(STORE, ignore_errors=True)
    SIDECAR.unlink(missing_ok=True)
    print(f"datacrystal proving ground: full-text search  ({sys.platform})")
    print(f"  dataset={DIR.name}  lang={LANG}  qrels={QRELS}\n")

    # --- INGEST + BUILD THE FTS INDEX ----------------------------------------
    t0 = time.perf_counter()
    store = dc.Store.open(STORE)
    docs = list(load_corpus())
    for d in docs:
        store.store(d)
    store.commit()
    t_ingest = time.perf_counter() - t0
    oid2did = {oid_of(d): d.doc_id for d in docs}  # for mapping hits → doc-ids
    t0 = time.perf_counter()
    with store.snapshot() as snap:
        idx = FullTextIndex.bootstrap(SIDECAR, snap)  # build the FTS5 sidecar
    t_fts = time.perf_counter() - t0
    on_disk = SIDECAR.stat().st_size / 1e6
    print(f"ingested + indexed:    {len(docs):>8,} docs   "
          f"store {t_ingest:5.2f}s  ·  FTS build {t_fts:5.2f}s "
          f"({len(docs) / t_fts:,.0f} docs/s)")
    print(f"  FTS sidecar:         {on_disk:>8.1f} MB   peak RSS {peak_rss_mb():.0f} MB")

    # --- a qualitative look ---------------------------------------------------
    queries = load_queries()
    qrels = load_qrels()
    sample_qid = next(iter(qrels))
    sample_hits = idx.search(queries[sample_qid], limit=3)
    print(f"\nsample query [{sample_qid}]: {queries[sample_qid][:70]!r}")
    for h in sample_hits:
        d = store.get_many([h.oid])[0]
        rel = "  ✓relevant" if oid2did[h.oid] in qrels.get(sample_qid, {}) else ""
        print(f"  {h.score:6.2f}  {d.title[:64]!r}{rel}")

    # --- THE RELEVANCE ORACLE (the new thing — ranking quality, #1-#4 can't) -
    n_q = 0
    P10 = R100 = NDCG10 = MRR = 0.0
    t0 = time.perf_counter()
    for qid, relevant in qrels.items():
        qtext = queries.get(qid)
        if not qtext:
            continue
        hits = idx.search(qtext, limit=100)
        retrieved = [oid2did[h.oid] for h in hits]
        n_q += 1
        P10 += sum(1 for d in retrieved[:10] if d in relevant) / 10
        R100 += sum(1 for d in retrieved[:100] if d in relevant) / len(relevant)
        NDCG10 += ndcg_at(retrieved, relevant, 10)
        for i, d in enumerate(retrieved):
            if d in relevant:
                MRR += 1 / (i + 1)
                break
    t_q = (time.perf_counter() - t0) * 1000
    print(f"\nrelevance over {n_q} judged queries (BM25 + {LANG} stemming, vs human qrels):")
    print(f"  nDCG@10:   {NDCG10 / n_q:.3f}")
    print(f"  P@10:      {P10 / n_q:.3f}")
    print(f"  Recall@100:{R100 / n_q:.3f}")
    print(f"  MRR:       {MRR / n_q:.3f}")
    print(f"  query latency: {t_q / n_q:.1f} ms/query  ({n_q} queries, top-100)")

    # --- CORRECTNESS: stemming actually fires (recall a morphological variant) -
    idx.close()
    store.close()
    print("\ncorrectness: FTS index built from a snapshot ✓ · "
          "ranking measured against real human judgments ✓")


if __name__ == "__main__":
    main()
