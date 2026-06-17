# How-to: evolve your schema

Goal: change your entity classes between runs without losing or rewriting old data. datacrystal
adapts old records **on load**; this guide is the task-by-task playbook. The error you may hit
(`SchemaMismatchError`) is in [the Errors reference](../reference.md#errors); the load-time markers
(`dc.RenamedFrom`, `dc.Glue`) are introduced in [Define entities](../reference.md#define-entities).

## What change is safe?

You can evolve entity classes between runs; old records adapt **on load**:

| Change | What happens |
|---|---|
| add a field **with a default** | old records get the default when loaded ✔ |
| remove a field | old values are ignored ✔ |
| reorder fields | values map by name ✔ |
| add a field **without a default** | `SchemaMismatchError` naming the field — add a default |
| add a `dc.Unique` field | must default to `None`, else `SchemaMismatchError` (a shared non-None default would collide) |
| rename a field | mark the new field `Annotated[T, dc.RenamedFrom("old")]` — the old values follow ✔ (see below) |
| split / merge / derive a field | mark the new field `Annotated[T, dc.Glue(fn)]` — `fn(old_record)` computes it from the old record ✔ (see below) |
| change a field's type | not checked (annotations are not validated on load) — avoid, or use `dc.Glue` to convert on load |

## Rename a field without losing data

To rename a field without losing data, mark the new field with its old persisted name:
`mohs: Annotated[float | None, dc.RenamedFrom("hardness")]`. On load, a record that lacks
`mohs` but still has `hardness` binds the old column, so the rename follows your code —
additively, never rewriting the record (and the new name wins once data is written under it).

## Reshape a field: split, merge, derive (dc.Glue)

When a change needs the data *reshaped* — split one field into two, merge two into one, or
convert a type — mark the new field with `dc.Glue(fn)`. On load, a record that lacks the field
calls `fn(old)` with the old record as a read-only `{name: value}` mapping and uses the result:

```python
@dc.entity
class Locality:
    # old records persisted coords="48.1,11.5"; lat/lon now follow your code
    lat: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[0]))] = 0.0
    lon: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[1]))] = 0.0
```

Glue fires **only when the field is absent** from a record's persisted shape — so once data is
written in the new shape it is a no-op, and old records are never rewritten in place (additive,
like `RenamedFrom`).

v0.2 scopes both `RenamedFrom` and `Glue` to **non-indexed** fields read through live hydration
and decode (`get`/`query`/`pluck`); honoring them in the index, snapshot, and arrow decode
paths, and renaming an indexed field, are `[planned — v0.2]`.

## Materialize the new shape on disk: migrate and verify

`RenamedFrom` and `Glue` adapt old records *on read*. When you want the new shape **materialized
on disk** — so a derived field becomes a real persisted column you can then index — run the
offline `store.migrate()`:

```python
moved = store.migrate()   # re-encode every stale-shape record to the newest shape
```

`migrate()` hydrates each record persisted under an older lineage shape (through renames, glue and
defaults) and re-commits it under the current shape — additive (a new lineage row, never a blob
rewrite), owner-confined, lease-held, and crash-safe (it rides the normal commit; a partial run
just resumes). It is **idempotent** (a second run rewrites nothing) and commits in `batch`-sized
chunks (`store.migrate(batch=10_000)`, the default) so peak memory tracks the batch, not the store.

`store.verify()` is the read-only pre-flight: it decodes every record against the current code
*without* mutating anything and returns the `(typename, oid)` pairs that **don't** decode — a field
removed-then-re-added with no default or `Glue`, a type the running code no longer defines, or a
corrupt record. An empty list means the whole store reads cleanly. Run `verify()` before
`migrate()`.

## Recipe: deriving an *indexed* field (Glue + migrate)

`Glue` and `RenamedFrom` are read-time markers and live only on **non-indexed** fields — putting
one on a `dc.Index`/`dc.Unique` field raises at `@entity`. The reason is correctness, not
arbitrariness: an index is built from the *persisted* value, not the glued one, so a glued index
would silently index the wrong data. To end up with a *derived* field that is **also indexed**,
split it into two steps and let `migrate()` bridge them:

```python
# Step 1 — derive on read (NON-indexed), so old records adapt immediately on load
@dc.entity
class Locality:
    name: str
    lat: Annotated[float, dc.Glue(lambda old: float(old["coords"].split(",")[0]))] = 0.0

store = dc.Store.open("cabinet.store")
store.migrate()        # Step 2 — materialize `lat` into a real persisted column on disk

# Step 3 — `lat` is now a plain column; (re)declare it indexed and reopen
@dc.entity
class Locality:
    name: str
    lat: Annotated[float, dc.Index] = 0.0   # no Glue — a real, indexable column
```

After `migrate()`, every record physically carries `lat`, so adding `dc.Index` builds a **correct**
index over real data. The ordering matters: keep the field non-indexed while the value is glued
(the glue derives it on every read), and only add the index once `migrate()` has written the column
to disk. The same recipe applies to a renamed field you want indexed (`RenamedFrom` → `migrate()` →
`Index`). `migrate()` keeps existing indexes consistent automatically — it rewrites through the
normal commit path, so committed records fold into any built index and a reopen rebuilds indexes
from the newest records.

How it works (one paragraph, so the behavior is predictable): the store keeps a **type
lineage** — every field shape a class ever had gets its own row in the type dictionary, and
each record decodes through the shape it was written with, by field name. Old records are never
rewritten in place; they migrate to the newest shape the next time you modify and commit them.
A store that used schema evolution is still openable by this and any newer library version.
