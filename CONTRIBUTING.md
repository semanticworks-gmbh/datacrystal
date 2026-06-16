# Contributing to datacrystal

datacrystal is maintained solo by **Sven Hodapp**. Issues, bug reports, and small focused pull
requests are welcome.

## Before you open something

- **Scope lives in [docs/design/ROADMAP.md](docs/design/ROADMAP.md)** — the scope authority,
  including the explicit **Punted** and **Never** lists (no Rust core, no CRDT core, no
  multi-writer, no homegrown SPARQL/Cypher, …). Check both lists before proposing a feature; a
  PR that crosses the Never list will be declined no matter how good the code is.
- The product **"why"** is [docs/design/VISION.md](docs/design/VISION.md); the engineering
  standards (fitness functions, perf gates) are [docs/design/KICKOFF.md](docs/design/KICKOFF.md).
- Editing the docs? The maintainer playbook is
  [docs/design/DOCS_AUDIT_GUIDE.md](docs/design/DOCS_AUDIT_GUIDE.md) (Diátaxis, the
  improve-without-regressing workflow).
- The public API **froze at the v0.1.0 tag** — everything since is purely additive. A change that
  touches the frozen surface needs a design decision first (open an issue).

## Dev setup

datacrystal targets **Python 3.14** (pinned via `.python-version`) and uses
[uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras          # environment, incl. the [fts]/[arrow]/[web] extras for their tests
uv run pytest -q              # full suite, incl. the fitness gates + the SIGKILL crash test
uv run ruff check .           # lint (line length 100)
uvx pyright src tests examples benchmarks   # type check, standard mode — 0 errors
uvx pyright -p pyrightconfig.strict.json    # strict mode, library src/ only — 0 errors
```

The README quickstart and `examples/minerals/demo.py` must each run **twice** from a clean
directory — the second run must find the first run's data.

## Pull requests

- **Small, logical commits**, one concern each.
- Keep the gates green: `ruff`, `pytest`, and both `pyright` modes must pass.
- Engine behavior is tested against **both backends** (memory and SQLite) via the
  `store_factory` fixture — they must behave identically.
- **Documentation honesty rule:** document only what exists. A feature that isn't built yet is
  marked `[planned — milestone]`, never described as if it were real.
- The test/demo domain is always the **mineral cabinet** — don't invent a second one.

## License

By contributing you agree your contributions are licensed under the project's
[MIT License](LICENSE).
