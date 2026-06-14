# SPIKE — Pydantic interop for datacrystal (FastAPI elegance)

**Status:** research complete · feeds #23 / ROADMAP item 12 (`datacrystal[web]`, *backlog*) · **no owner ruling needed on the approach**
**Date:** 2026-06-14
**Method:** 3 research agents (ecosystem patterns · datacrystal seam · synthesis), all facts verified against the codebase.

> This is the readable companion to issue #49. It leads with **how it should feel to use** (the
> code), then the verdict and the why. The sized story set lives on #49.

---

## How it feels to use (the end state)

Your entity is **unchanged** — still a slots dataclass with identity, lazy refs, and owner
confinement. Pydantic never touches it:

```python
@dc.entity
class Mineral:
    qid: Annotated[str, dc.Unique]
    name: str
    mohs: float | None = None
    type_locality: dc.Lazy[Locality] | None = None     # a lazy cross-ref
```

`datacrystal[web]` (an optional extra — `pydantic` stays out of the `{msgspec, pyroaring}` core)
generates a **paired Pydantic model by reflection**, and converts at the request/response edge:

```python
from datacrystal.web import entity_model, to_pydantic, from_pydantic

MineralOut = entity_model(Mineral)   # a pydantic.BaseModel, generated from TypeInfo/FieldSpec, cached
# MineralOut mirrors Mineral's shape, with one rule for graph edges:
#   type_locality: int | None   ← a Lazy/ref becomes the target OID (a scalar the wire can carry)
```

FastAPI then works with zero hand-written schemas:

```python
from fastapi import FastAPI
app = FastAPI()

@app.get("/minerals/{qid}", response_model=MineralOut)
def read(qid: str):
    m = store.get(Mineral, qid=qid)      # a live entity, on the owner thread
    return to_pydantic(m)                # → a detached DTO: refs/Lazy → OID, containers → list/dict

@app.post("/minerals", response_model=MineralOut)
def create(body: MineralOut):            # FastAPI validates the request body into MineralOut
    m = from_pydantic(body, Mineral)     # reconstruct THROUGH the public constructor: Mineral(**fields)
    store.store(m); store.commit()       #   → OID stamped, dirty-tracked, owner-confined by the engine
    return to_pydantic(m)
```

Because the generated model carries `model_config = ConfigDict(from_attributes=True)`, you can also
hand FastAPI the entity directly for read-only paths — it pulls fields by name:

```python
@app.get("/minerals/{qid}", response_model=MineralOut)
def read(qid: str):
    return store.get(Mineral, qid=qid)   # MineralOut.model_validate(entity) under the hood
```

That is the whole developer experience: **declare your objects once, get FastAPI request/response
models, OpenAPI docs, and validation for free** — the VISION's "declare, deploy, scale" promise,
without your persistence objects ever becoming Pydantic models.

---

## Verdict

**Convert at the boundary; never make `@entity` *be* a Pydantic model.** Generate a paired
Pydantic model from the entity's already-materialized `TypeInfo`/`FieldSpec`, and convert with
`to_pydantic`/`from_pydantic` at the edge — the DTO a one-way detached snapshot exactly like the
existing `EntityView`. It all lives in a new `datacrystal[web]` extra (`src/datacrystal/web.py`,
sibling of `arrow.py`/`fts.py`), importing `pydantic` only at its own module top, never re-exported
from `__init__.py`. This is the **Tortoise-style derive-by-reflection** pattern, the cleanest of the
surveyed options, and it sidesteps SQLModel's silent `table=True`-skips-validation trap by
construction (the validated model is a separate class).

## Why not make `@entity` a Pydantic model

A `BaseModel` is the structural opposite of an `@entity`, and unifying them breaks four load-bearing
invariants:

| `@entity` (load-bearing)                                              | Pydantic `BaseModel`                          |
|----------------------------------------------------------------------|-----------------------------------------------|
| **one live instance per OID** (WeakValueDictionary registry, inv. 6) | `model_validate` **copies** — mints new objects |
| tracked one-shot `__setattr__` (ADR-001 thread check *before* mutate)| its own validating `__setattr__` — would collide |
| `slots=True` + `weakref_slot` (speed + collectability, fitness #15)  | not slots by default; carries validator machinery |
| core deps **exactly** `{msgspec, pyroaring}` (invariant 2)           | `pydantic` is heavyweight → must be an **extra** |

So the persistence object and the API model are **different objects** — the same lesson `msgspec`,
Tortoise, and even SQLModel-in-practice all converge on. datacrystal already has the reflection
(`TypeInfo`/`FieldSpec`) and the detach precedent (`EntityView`, `_view_value`) to bridge them
cheaply.

## How values cross the boundary

| In the entity                         | In the Pydantic DTO            |
|---------------------------------------|--------------------------------|
| scalar (`str`/`int`/`float`/`bool`)   | itself                         |
| entity ref / `dc.Lazy[T]`             | the target **OID** (`int`) — `peek()`-if-loaded-else-`oid`, the snapshot path's exact rule |
| `PersistentList` / `PersistentDict`   | `list` / `dict`                |
| `None`                                | `None`                         |

`from_pydantic` reconstructs through the **public** `cls(**fields)` constructor, so OID stamping and
dirty-tracking go through the engine — never by poking `__dc_*` slots.

## Sized story set (build plan for #23 — full ACs on #49)

| # | Story | Concerns | Deps |
|---|-------|----------|------|
| **S1** | Stand up the `web` extra skeleton (`pyproject` `web = ["pydantic>=2"]`, `web.py`, dep-isolation fitness stays green) | 2 | — |
| **S2** | `entity_model(cls)` — reflect `TypeInfo`/`FieldSpec` into a cached paired Pydantic model | 4 | S1 |
| **S3** | `to_pydantic(entity_or_view)` — detached DTO with the snapshot value mapping | 4 | S2 |
| **S4** | `from_pydantic(model, cls)` — reconstruct through the public constructor (engine-stamped) | 3 | S2 |
| S5 | *(deferred spike)* validation hooks / custom scalar types at the edge | ? | S4 |
| S6 | *(sibling)* strawberry GraphQL types from the **same** reflection | — | S2 |

## Future: one reflection engine, many web targets

The reflection that builds the Pydantic model is the same engine that builds **strawberry GraphQL**
types (#23's other half) — so `datacrystal[web]` grows one schema-generator with two front-ends
(REST/OpenAPI via Pydantic, GraphQL via strawberry). The same pattern generalizes to any future
"describe my entity to an external framework" need (msgspec Structs for a non-Pydantic API,
JSON-Schema export, etc.): reflect `TypeInfo`, never subclass the entity.

---

## Appendix — ecosystem survey (the reasoning)

- **SQLModel** makes the persisted model a Pydantic model by inheritance — but `table=True`
  **skips validation** silently (issues #406/#453), so its own tutorial recommends separate
  `HeroCreate`/`HeroPublic` boundary models. Even the "unified" library converges on boundary models.
- **Beanie** subclasses `Document` from `BaseModel` (tightest coupling) — not an option here (it
  would drag Pydantic into the persistence object).
- **ormar** composes a Pydantic model + a SQLAlchemy table side-by-side.
- **Tortoise** *generates* a separate Pydantic schema by reflection (`pydantic_model_creator`) — the
  cleanest separation, and the model we adopt.
- **msgspec.Struct vs pydantic** — the closest analog to our own situation: a fast slots-struct does
  **not** pretend to be a Pydantic model; it interops at a boundary. That is exactly `@entity`.

FastAPI's requirement is narrow: any request/response type needs a pydantic-core schema (for
validation, `response_model` serialization, and OpenAPI). A generated `BaseModel` with
`from_attributes=True` satisfies all three; an opaque `@entity` does not (`arbitrary_types_allowed`
is insufficient for a `response_model`). Hence: generate + convert.
