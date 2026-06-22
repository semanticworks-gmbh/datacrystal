# Vision

> The "why" behind datacrystal — direction, not scope. Roadmap & boundaries live in
> [ROADMAP.md](ROADMAP.md). *(Ratified 2026-06-13.)*

## Your objects are already the model. Stop translating them.

You design your domain as Python objects — then you translate it into tables, SQL, an ORM, and a
stack of migrations, and spend the rest of the project keeping two models in sync. The database
becomes one more thing to run, serve, and translate to.

That's backwards.

**Your live Python objects *are* the database.** Declare your `@entity` classes and they persist —
typed, identity-preserving, fast. **The data follows your code:** change a class, and the data
follows. No mapping layer. No migration raindance.

And your data is safe. Every commit is one ACID transaction — all-or-nothing and durable, an exact
prefix after any crash, never a torn write. A real database, not a pickle file — you just never
have to leave your objects to get one.

**The only infrastructure is a blob store you already have** — S3 or a shared drive. Nothing to
operate. From a laptop to a fleet of instances, the same code, blazing fast: systems of record,
knowledge graphs, search, FastAPI services, live analytics over your data.

**And it's fractal.** Every node is the same datacrystal — the same objects, the same code; whether
it leads or follows is only config. Point edge nodes at the shared graph — indexers, annotators,
local LLMs — and each reads it locally at full speed and contributes back what it discovers. The
whole has the shape of the part, so it composes without limit: any follower can be promoted to
writer, and every discovery enriches the graph the next node reads. The cloud stays small while
intelligence accretes at the edges and flows home — one graph, many minds, self-similar all the
way up. That self-similarity is the point: the system grows more powerful by repeating one simple
shape, not by adding new machinery.

You came here to build, not to administer a database. Declare, deploy, ship — the data just follows.

---
> *Today:* single node, with Litestream / parquet-on-S3. The multi-instance + S3-native arc is the
> **direction** ([ROADMAP.md](ROADMAP.md) items 16 & 21), not yet shipped — honesty rule:
> [GUIDE.md](../GUIDE.md).
