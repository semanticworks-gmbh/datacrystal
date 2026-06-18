# How-to: full-text search (datacrystal[fts])

Goal: add ranked, stemmed full-text search over prose fields. The `datacrystal[fts]` extra is a
commit-delta consumer (see [the commit-delta pipeline](../reference.md#the-commit-delta-pipeline)):
an SQLite FTS5 index in its own sidecar file, kept current by the pipeline, rebuildable from a
snapshot at any time. The `dc.FullText` marker is introduced in
[Define entities](../reference.md#define-entities); `FtsConfigError` is in
[the Errors reference](../reference.md#errors).

```python
pip install 'datacrystal[fts]'     # adds snowballstemmer
```

```python
from datacrystal.fts import FullTextIndex

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    notes: Annotated[str | None, dc.FullText(language="de")] = None

idx = FullTextIndex("cabinet.fts")     # config read from the dc.FullText markers
store.attach(idx)
... store.commit() ...

for hit in idx.search("Kristall"):     # stemming: finds "Kristalle", ranked by BM25
    print(hit.score, hit.typename, hit.snippet)   # snippet marks matches [like] this
minerals = store.get_many([hit.oid for hit in idx.search("Tsumeb", cls=Mineral)])
```

- **Stemming is per-field**: `dc.FullText(language="de")` gets index-time Snowball
  stemming (27 languages by ISO code or Snowball name); bare `dc.FullText` is fold-only
  exact matching (case + diacritics + Unicode-compat forms fold: `m²` matches `m2`,
  `Glänzend` matches `glanzend`). Exact matches outrank stem-only matches.
- Quoted phrases stay phrases; loose terms combine per `match=`: **`"any"` (the default)**
  ranks the OR-union of the terms (natural-language recall — a question doc needn't contain
  *every* word), `"all"` requires every term (precise faceting). User input is quoted into the
  FTS5 expression — it can never inject MATCH operators. `cls=` narrows to one entity type;
  `hit.snippets` maps each matched field to its highlighted excerpt, and `hit.snippet` is the
  first non-empty one.
- Attaching to a lived-in store: `FullTextIndex.bootstrap(path, snapshot)` (deltas are
  not retained — the [snapshot-bootstrap recipe](snapshots-and-delta-log.md)). Reopening with a
  different field/language configuration raises `FtsConfigError`: rebuild, a half-matching index is
  stale.
- Honest limits: unsegmented CJK runs are single tokens under unicode61 (`水晶です` is
  findable only as that whole run) `[planned — segmenting tokenizer, demand-driven]`;
  abugida-script languages (hi/ne/ta) are refused loudly rather than silently broken.
  Like the store, an index is used from the thread that opened it.
- For semantic / RAG retrieval, combine this BM25 index with embeddings you compute — see
  [Vector and hybrid search](vector-search.md), which fuses `idx.search(...)` and dense vectors
  with Reciprocal Rank Fusion over the same store.
