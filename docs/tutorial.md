# Tutorial: your first mineral cabinet

This is a hand-held first session. By the end you will have a small datacrystal store on disk
that survives a restart — you will define a model, add data, commit it, reopen the store and
**see your data still there**, run a query, and follow a lazy reference. Every step is
explained; there are no choices to make along the way. (Once you have the feel of it, the
[how-to guides](reference.md#see-also) cover specific goals and the
[reference](reference.md) is the complete API.)

We model a tiny **mineral cabinet** — a few minerals, and the locality one of them came from.

## Step 0 — install

datacrystal is not on PyPI yet (the name is reserved); install it straight from GitHub. The
[README Quick start](../README.md#quick-start) has the exact commands; the short version with
`uv`:

```bash
uv add "datacrystal @ git+https://github.com/themerius/datacrystal@v0.6.0"
```

You need Python 3.14. Nothing else — the core has just two dependencies and no database server
to run; a datacrystal store is a directory on disk.

## Step 1 — the whole program

Here is the complete first session as one runnable file. Save it as `cabinet.py`. We will walk
through every line below, but run it first to see it work:

```python
from typing import Annotated
import datacrystal as dc

# 1. Define a tiny model — two entity classes.
@dc.entity
class Locality:
    qid: Annotated[str, dc.Unique]     # a unique key we can look up by
    name: str

@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    crystal_system: Annotated[str | None, dc.Index] = None   # indexed → queryable
    type_locality: dc.Lazy[Locality] | None = None           # a lazy reference

# 2. Open a store (a directory; created on first run).
store = dc.Store.open("cabinet.store")

# 3. On the very first run the store is empty — root is None. Add data once.
if store.root is None:
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine")
    store.root = {"minerals": [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"),
        Mineral(qid="Q193563", name="azurite", crystal_system="monoclinic",
                type_locality=dc.Lazy.of(tsumeb)),
    ]}
    store.commit()                      # 4. persist everything we just built
    print("first run: added 2 minerals")
else:
    print("reopened: the data is already here")

# 5. Run a query — find every monoclinic mineral.
hits = store.query(Mineral.crystal_system == "monoclinic")
print("monoclinic:", sorted(m.name for m in hits))

# 6. Follow a lazy reference — load azurite, then its type locality on demand.
azurite = store.get(Mineral, qid="Q193563")
print("azurite was first described at:", azurite.type_locality.get().name)

store.close()
```

Run it:

```bash
python cabinet.py
```

You should see:

```
first run: added 2 minerals
monoclinic: ['azurite']
azurite was first described at: Tsumeb Mine
```

Now **run the exact same command a second time**:

```bash
python cabinet.py
```

This time:

```
reopened: the data is already here
monoclinic: ['azurite']
azurite was first described at: Tsumeb Mine
```

The second run did **not** add anything — it reopened the store you wrote on the first run and
found your two minerals waiting. That is the whole point: your live objects *are* the database.

## What each step did

**1. Define a tiny model.** `@dc.entity` turns a plain typed class into something datacrystal can
persist — it is still just a dataclass you construct with `Mineral(qid=..., name=...)`. Two field
markers, written inside `typing.Annotated`, told the engine what to do with two of the fields:

- `Annotated[str, dc.Unique]` makes `qid` a **unique key** — one record per value, and you can
  look an entity up by it with `store.get(...)`.
- `Annotated[str | None, dc.Index]` makes `crystal_system` **indexed** — queries that test it
  (`== "monoclinic"`) answer from a bitmap index instead of scanning every record.

The `type_locality` field is a `dc.Lazy[Locality]` — a **reference** to another entity that loads
only when you ask for it. More on that in step 6.

**2. Open a store.** `dc.Store.open("cabinet.store")` opens (or, the first time, creates) a
directory called `cabinet.store`. That directory *is* your database — an SQLite file inside it
holds your records.

**3. The first-run check.** `store.root` is the single entry point into your object graph. It is
`None` until you assign it, so `if store.root is None:` is exactly "is this the first run?". On the
first run we build our data and assign it to `store.root`; on later runs that block is skipped
because the root is already there. We used a plain dict (`{"minerals": [...]}`) as the root — a dict
is a perfectly good root.

**4. Commit.** `store.commit()` writes everything we built atomically. There is no session to open,
no `save()` on each object, no dirty flag to set — you mutate your Python objects and call
`commit()`, and datacrystal works out what changed. After a commit the data is durable: it survives
the process ending.

**5. Query.** `store.query(Mineral.crystal_system == "monoclinic")` returns the live `Mineral`
objects whose `crystal_system` is `"monoclinic"`. Because that field is indexed, the query answers
from the bitmap index — it does not read every mineral. The result is real `Mineral` instances; we
just read `.name` off each.

**6. Follow a lazy reference.** `store.get(Mineral, qid="Q193563")` looks azurite up by its unique
key. Its `type_locality` is a `dc.Lazy[Locality]` — at this point the `Locality` it points at is
**not** loaded yet. Calling `.get()` on the lazy reference loads it now and returns the `Locality`
object, whose `.name` is `"Tsumeb Mine"`. (We wrote the reference with `dc.Lazy.of(tsumeb)` back in
step 3.) Lazy references are how you keep large graphs off your memory budget — you load only the
nodes you actually walk to.

## Where to go next

You now have the core loop: **define, open, mutate, commit, reopen, query, follow.** From here:

- **Goals** — the [how-to guides](reference.md#see-also): paging and backlinks, keeping memory
  bounded on big imports, evolving your schema, binary blobs, full-text search, analytics with
  DuckDB, deploying behind FastAPI/GraphQL.
- **The full API** — the [reference](reference.md): every method, every option, every guarantee.
- **The "why"** — the [explanation](explanation.md): how identity and memory work, the query
  semantics, the design philosophy.
