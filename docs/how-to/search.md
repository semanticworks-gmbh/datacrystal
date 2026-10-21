# How-to: search prose fields with full-text search

After this page you can search prose fields with stemming and BM25 ranking, and keep a protected class's hits behind its fence.

```bash
pip install 'datacrystal[fts]'
```

```python
from typing import Annotated

import datacrystal as dc
from datacrystal.fts import FullTextIndex

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    notes: Annotated[str | None, dc.FullText(language="de")] = None

store = dc.Store.open("cabinet.store")
index = FullTextIndex("cabinet.fts")
store.attach(index)
if store.root is None:
    store.root = {"seeded": True}
    store.store(Mineral(qid="Q193563", name="azurite", notes="Tiefblaue Kristalle aus Tsumeb"))
    store.store(Mineral(qid="Q43010", name="quartz", notes="Farbloser Kristall"))
    store.commit()

for hit in index.search("Kristall"):
    print(hit.typename, hit.snippet)
oids = [hit.oid for hit in index.search("Tsumeb", cls=Mineral)]
print([m.name for m in store.get_many(oids)])
store.close()
```

- `FullTextIndex` is a commit-delta consumer: an SQLite FTS5 file beside the store, kept current by the pipeline and rebuildable from a snapshot ([pipeline](../reference.md#the-commit-delta-pipeline)).
- `dc.FullText(language="de")` stems at index time with Snowball (27 languages by ISO code or Snowball name).
  Bare `dc.FullText` is fold-only exact matching: case, diacritics and Unicode compatibility forms fold, so `m²` matches `m2` and `Glänzend` matches `glanzend`.
  Exact matches outrank stem-only matches.
- Quoted phrases stay phrases.
  Loose terms combine per `match=`: `"any"` (default) ranks the OR-union, `"all"` requires every term.
  Input is quoted into the FTS5 expression, so it cannot inject MATCH operators.
- `cls=` narrows a search to one entity type; `hit.snippets` maps each matched field to its highlighted excerpt and `hit.snippet` is the first non-empty one.
- To attach to a store that already has data, build the index with `FullTextIndex.bootstrap(path, snapshot)`; deltas are not retained ([snapshots](snapshots-and-delta-log.md)).
  Reopening with a different field or language configuration raises `FtsConfigError`, so rebuild.
- An index that covers a `protected=True` class refuses `search()` without a snapshot (`ReadDeniedError`).
  Pass a principal-bound `snapshot=`: `with store.snapshot(principal=user) as snap: index.search("Kristall", snapshot=snap)`.
  Hits that principal cannot read are dropped, and snippets render from the snapshot's own view text.
  A root-bound snapshot (`dc.root_principal(...)`) filters nothing, and `bootstrap()` over a protected class requires one.
  Over-fetch is `4 × limit`, doubling, under a hard `scan_cap` (default 2000).
- The sidecar holds the plaintext of every indexed field, so guard the `.fts` file like the store itself ([ADR-008](../design/ADR-008-permissions-contract.md) R13).
- Unsegmented CJK runs are single tokens (`水晶です` is findable only as that whole run), `[planned — segmenting tokenizer, demand-driven]`.
  Abugida-script languages (hi, ne, ta) are refused loudly.
- An index is used from the thread that opened it.
- For semantic retrieval, fuse this BM25 ranking with embeddings in [vector and hybrid search](vector-search.md).

Reference: [Define entities](../reference.md#define-entities), [Errors](../reference.md#errors).
