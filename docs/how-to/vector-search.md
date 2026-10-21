# How-to: search by vector and fuse it with full-text ranking

After this page you can store embeddings you computed, rank them by exact cosine similarity under a structured prefilter, and fuse that ranking with BM25.

datacrystal never owns the embedding model; you compute vectors in your application and datacrystal stores and ranks them.
This example uses 3-dimensional toy vectors; replace `embed` with a call to your model, for example `sentence-transformers` (a dependency of your application, not of datacrystal).

```python
import math
import struct
from typing import Annotated

import datacrystal as dc
from datacrystal.fts import FullTextIndex

DIMENSIONS = 3

def pack(vector: list[float]) -> bytes:
    norm = math.sqrt(sum(x * x for x in vector))
    return struct.pack(f"{DIMENSIONS}f", *(x / norm for x in vector))

def unpack(blob: bytes) -> tuple[float, ...]:
    return struct.unpack(f"{DIMENSIONS}f", blob)

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str, dc.Index]
    notes: Annotated[str | None, dc.FullText(language="en")] = None
    embedding: bytes | None = None

store = dc.Store.open("cabinet.store")
index = FullTextIndex("cabinet.fts")
store.attach(index)
if store.root is None:
    store.root = {"seeded": True}
    for qid, name, system, notes, vector in [
        ("Q193563", "azurite", "monoclinic", "deep blue copper carbonate", [0.9, 0.1, 0.2]),
        ("Q190063", "malachite", "monoclinic", "bright green copper carbonate", [0.8, 0.3, 0.1]),
        ("Q43010", "quartz", "trigonal", "clear silicon dioxide", [0.1, 0.9, 0.3]),
    ]:
        store.store(Mineral(qid=qid, name=name, crystal_system=system, notes=notes, embedding=pack(vector)))
    store.commit()

def nearest(query: list[float], k: int, where=None) -> list[int]:
    query_vector = unpack(pack(query))
    with store.snapshot() as snap:
        views = snap.query(where) if where is not None else snap.all(Mineral)
        scored = [(sum(a * b for a, b in zip(query_vector, unpack(view.embedding))), view.oid)
                  for view in views if view.embedding is not None]
    return [oid for _, oid in sorted(scored, reverse=True)[:k]]

def rrf(*ranked_oid_lists: list[int], k: int = 60) -> list[int]:
    score: dict[int, float] = {}
    for ranked in ranked_oid_lists:
        for rank, oid in enumerate(ranked):
            score[oid] = score.get(oid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=score.__getitem__, reverse=True)

F = dc.fields(Mineral)
dense = nearest([0.85, 0.2, 0.15], k=3, where=F.crystal_system == "monoclinic")
lexical = [hit.oid for hit in index.search("copper carbonate", cls=Mineral, limit=3)]
fused = store.get_many(rrf(dense, lexical)[:2])
print([m.name for m in store.get_many(dense)], [m.name for m in fused])
store.close()
```

- Store the vector as an inline `bytes` field: you own the dtype and shape, and datacrystal stores opaque bytes.
  Keep the dimension fixed per field so a reader can reshape the bytes without metadata.
  A 768-dimension `float32` vector is 3072 bytes, which is the right size for inline storage.
- Use `Annotated[bytes, dc.Blob]` only when the vector would bloat scans of the owning type, for example a per-token matrix of tens of KB ([blobs](blobs.md)).
  Avoid `list[float]`, which is the largest on disk and decodes element by element.
- The structured prefilter runs through the bitmap planner first, so the vector scan only ranks the survivors; this is the candidate-set then residual shape of the [Reading API](../reference.md#reading-api).
  With unit-norm vectors cosine equals the dot product, and with `numpy` the rerank over a prefiltered set is one matrix multiplication.
- Read through a [`snapshot()`](../reference.md#snapshots) so the ranking is exact and callable from any thread.
- Reciprocal Rank Fusion combines the two lists on rank rather than score, so BM25 and cosine need no normalization; `k=60` is the community default.
  A library helper `fuse_rrf` is `[planned — theme:search]`, and `rrf` above is the whole of it until then.
- Exact brute force is the baseline of comparable embedded tools (LanceDB, sqlite-vec, `llm`) and holds up to about 100K vectors or 100 ms per query; a tight prefilter reaches much larger corpora.
- Beyond that there are two paths, and neither is in core.
  An in-process approximate index, the `datacrystal[vector]` extra (a usearch sidecar with a `dc.Vector` marker, GitHub #22), is `[planned — item 11]`.
  The analytics tier mirrors entities to parquet with [`datacrystal[arrow]`](analytics.md) and searches there with DuckDB's VSS extension, because the core planner never grows a cost model ([why](../explanation.md#query-semantics-the-planner-the-residual-and-the-candidate-set)).
- Late interaction (ColBERT) stores a matrix of per-token vectors as `Annotated[bytes, dc.Blob]` and computes MaxSim in Python; treat it as a separate, advanced path.

Related: [full-text search](search.md).
