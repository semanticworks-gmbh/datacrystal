# How-to: vector and hybrid search (bring your own embeddings)

Goal: semantic / RAG retrieval over your live objects — store the embeddings you compute, rank by
exact cosine similarity, and (optionally) fuse that ranking with `datacrystal[fts]` BM25 for hybrid
search. The differentiator is that the text index, the vectors, and the structured fields are the
**same store**: one `snapshot()`, one query, no separate vector database to keep in sync.

datacrystal never owns the embedding **model**. `sentence-transformers` (and its ~5 GB of
torch) lives in *your* application, not the library's dependency tree — the core stays
`{msgspec, pyroaring}`. You pass vectors in; datacrystal stores them and ranks them.

```python
pip install sentence-transformers      # YOUR dependency, not datacrystal's
```

## Store the vector as bytes

A 768-dim `float32` embedding (the size `BAAI/bge-base-en-v1.5` produces) is 3072 bytes — store it
as a plain inline `bytes` field. You own the dtype and shape; datacrystal stores opaque bytes.

```python
import numpy as np
from typing import Annotated
import datacrystal as dc
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("BAAI/bge-base-en-v1.5")     # 768-dim, ~3 KB per vector

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str, dc.Index]             # a structured field to prefilter on
    notes: str | None = None
    embedding: bytes | None = None                       # 3072 bytes, stored inline

def embed(text: str) -> bytes:
    v = model.encode(text, normalize_embeddings=True)    # unit-norm ⇒ cosine == dot product
    return v.astype(np.float32).tobytes()

for m in store.query(Mineral):                           # backfill; or set it on first write
    if m.notes:
        m.embedding = embed(m.notes)
store.commit()
```

- **Inline `bytes` is the right default at this size.** It is a native msgpack scalar — no
  indirection. Reach for `Annotated[bytes, dc.Blob]` only once the vector would bloat *scans* of the
  owning type (a ColBERT per-token matrix at tens of KB on a frequently-scanned type), where the
  out-of-line, descriptor-only scan-skip pays off — see [Storing binary
  blobs](../reference.md#storing-binary-blobs). Avoid `list[float]`: it is the largest on disk and
  decodes element by element.
- datacrystal stores no dtype/shape metadata — you carry it. Keep the dimension fixed per field (768
  here) so `np.frombuffer(...).reshape(n, 768)` is unambiguous on read.

## Search: bitmap prefilter, then exact rerank

Narrow to a candidate set with a normal bitmap query, then compute cosine top-k over just those
rows. This is the same **candidate-set → Python residual** shape the [reading
API](../reference.md#reading-api) already runs — the planner does the indexed prefilter; the vector
math is the residual. Reading happens over a pinned [`snapshot()`](../reference.md#snapshots), so it
is exact and callable from any thread.

```python
def knn(store, query_text, k=10, *, where=None):
    q = model.encode(query_text, normalize_embeddings=True).astype(np.float32)
    with store.snapshot() as snap:                       # frozen view; close promptly
        views = snap.query(where) if where is not None else snap.all(Mineral)
        rows = [v for v in views if v.embedding is not None]
        if not rows:
            return []
        mat = np.frombuffer(b"".join(v.embedding for v in rows),
                            dtype=np.float32).reshape(len(rows), 768)
        scores = mat @ q                                 # one matmul; unit-norm ⇒ cosine
        top = np.argsort(-scores)[:k]
        return [(rows[i].oid, float(scores[i])) for i in top]

S = dc.fields(Mineral)
hits = knn(store, "deep-blue copper carbonate", k=5,
           where=(S.crystal_system == "monoclinic"))     # roaring prefilter → exact rerank
results = store.get_many([oid for oid, _ in hits])        # one storage round-trip
```

The tighter the structured prefilter, the smaller the matmul — the metadata filter does the heavy
lifting and the vector scan only reranks what survives it.

## Hybrid: fuse BM25 and dense with RRF

You already have lexical search via [`datacrystal[fts]`](search.md). Reciprocal Rank Fusion (the
standard pattern) combines two ranked lists on **rank, not score** — so the BM25-vs-cosine scale
mismatch never arises, with no normalization. `k=60` is the community default.

```python
def rrf(*ranked_oid_lists, k=60):
    score = {}
    for ranked in ranked_oid_lists:
        for rank, oid in enumerate(ranked):
            score[oid] = score.get(oid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=score.get, reverse=True)

dense   = [oid for oid, _ in knn(store, q, k=50)]                 # vector rank list
lexical = [h.oid for h in idx.search(q, cls=Mineral, limit=50)]   # BM25 via datacrystal[fts]
fused   = store.get_many(rrf(dense, lexical)[:10])                # one ranking over one store
```

`idx` is the `FullTextIndex` from the [full-text how-to](search.md). A small
`fuse_rrf(*ranked_lists, k=60)` library helper is `[planned — theme:search]`; until it lands, the
function above is the whole thing.

## Honest scope: when brute force is enough, and when it isn't

Exact brute-force top-k is the shipped baseline in comparable embedded tools (LanceDB, sqlite-vec,
Simon Willison's `llm`), and the industry-consensus crossover is around **~100K vectors / ~100ms**
per query — comfortably inside datacrystal's embedded, single-writer sweet spot. With a structured
prefilter narrowing the candidate set first, you reach much larger corpora before the matmul is the
bottleneck.

Beyond that ceiling there are two on-creed paths, neither baked into core:

- **Approximate (ANN) at scale, in-process:** the `datacrystal[vector]` extra — a usearch sidecar
  consumer with a `dc.Vector` field marker, one index file per field — is `[planned — item 11]`
  (GitHub #22), demand-gated. When it lands it targets single-vector ANN; it rides the same
  commit-delta pipeline as `[fts]`/`[arrow]`.
- **Analytics-tier ANN:** mirror entities to parquet with [`datacrystal[arrow]`](analytics.md) and
  search there — DuckDB's VSS/HNSW extension owns the cost-based, billion-scale tier (the
  "[DuckDB owns clever](../explanation.md#query-semantics-the-planner-the-residual-and-the-candidate-set)"
  doctrine). The core query
  planner stays rule-based and never grows a vector cost model.

Late-interaction (ColBERT, e.g. `lightonai/GTE-ModernColBERT-v1` via pylate) is a heavier shape: a
variable-length matrix of per-token 128-dim vectors per document, scored with MaxSim rather than a
single dot product. Store the matrix as `Annotated[bytes, dc.Blob]` and compute MaxSim in Python —
but treat it as an advanced, separate path, not the first thing to reach for. Like the store, a
snapshot is read from the thread that opened it.
