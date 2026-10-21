# Standards

Rules that bind every change to datacrystal, each short enough to check a diff against.
Copied from tetrahedron's `architecture/standards/STANDARDS.md` on 2026-10-20 (#186) and owned here from now on: a change goes through a PR on this file, not through tetrahedron.
Cite a rule by its slug.

Gates that switch on in a later wave say so by issue: ANN401, C901, PLR2004 in `src/` in #192.
#191 switched on D401, the docstring form (D102, D105, D107, D403, D415), T20 and ruff's own default rule set in `src/`, with E4, E7, E9 and F still named in `ruff.toml` and ruff pinned in CI; only the five-body-line docstring limit stays with review, since ruff has no rule for it.

### self-documenting-code
- **Rule:** Code states its intent through names, explaining variables, named predicates and constants; a docstring or comment adds only what the code cannot say — a one-line summary in the imperative, at most five body lines, the why — never a restated name, type or next line.
- **Applies to:** all code, tests included.
- **Enforced by:** ci — `ruff.toml` selects the google docstring rules and W505 (docs and comments within 100 columns) that are at 0 today; review for the rest — the PR reviewer reads every new name, condition, docstring and comment and checks the five-line docstring limit (ruff has no rule for it); D401, D102, D105, D107, D403 and D415 gate in `src/` and `scripts/` since #191 (tests ignore D103, D107 and D415); cannot see: stale or wrong text, name quality, a docstring over five body lines.
- **Example:** `scripts/testrules.py::check_file`
- **Source:** tetrahedron decision 542, copied 2026-10-20 (#186).

### typed-python
- **Rule:** Every function and method is fully annotated (ruff `ANN`) and the project passes Pyrefly at zero errors; CI blocks on both. A suppression is a line-level `# pyrefly: ignore[<code>]` with its reason, never file-wide in `src/`. A ruff `# noqa: <code> - <reason>` in `src/` is line-level too and allowed only where the rule contradicts a documented contract (BLE001, TRY004, UP007 today).
- **Applies to:** `src/`, `examples/`, `benchmarks/`; `tests/`.
- **Enforced by:** ci — `uv run pyrefly check` at 0 on `src`, `examples`, `benchmarks`, `tests`; `uvx ruff check .` selects ANN001–ANN003 and ANN2xx for `src`, `examples` and `scripts`, ANN401 (`Any`) from #192 and ANN for `tests`, `benchmarks` and `evals`; cannot see: an `Any` that hides a real type, a suppression without a reason.
- **Example:** `src/datacrystal/web/_pydantic.py` (the one `bad-return` suppression, with its reason)
- **Source:** tetrahedron decision 552, copied 2026-10-20 (#186); pyright leaves `src/` CI here.

### readable-tests
- **Rule:** A test names its behaviour and outcome, reads Given / When / Then, is straight-line code — no `if`, `for`, `while`, `try` or lambda in a test, no comprehension outside an `assert` — calls the system and asserts in its own body with the values stated, one fact per assert, through the public API, any fixture function-scoped — except a session-scoped process handle that only a function-scoped fixture requests — no `sleep()`; a helper builds data only; `parametrize` rows hold plain values only.
- **Applies to:** every test; `benchmarks/test_pr_gates.py` is covered too. The domain is the mineral cabinet only. An engine test runs on both backends through `store_factory` (`tests/conftest.py`), and memory and sqlite behave identically.
- **Enforced by:** ci — the `test-rules` job in `.github/workflows/ci.yml` runs `scripts/testrules.py` on every `tests/**/test_*.py` a PR changes, and on `benchmarks/test_*.py`, and fails on a finding; review for what it cannot see — a name that lies, a Then the asserts skip, a helper that acts without asserting, a mock that hides the behaviour; ruff `PT` and `FBT001` select the test rules for `tests/`; #193 adds a mutation check per PR.
- **Example:** `tests/unit/test_query.py`
- **Source:** tetrahedron decision 568, copied 2026-10-20 (#186) — the rules after *Software Engineering at Google* ch. 11–13 and Testing on the Toilet, Beck's Test Desiderata, Meszaros' test smells.
- **Exceptions:** a Hypothesis `RuleBasedStateMachine` asserts in its rules and invariants, because Hypothesis reports a failure only by exception; it lives in a helper module (`tests/property/_dirty_commit_machine.py`) and a test only runs it; a test reads private state only where no public seam exists, each read listed in #199, until #199 decides.

### structured-logging
- **Rule:** The library logs through the standard library only: `logging.getLogger(__name__)` under `datacrystal.*`, a `NullHandler` on `datacrystal` and nothing else — no handler, level, formatter or `basicConfig`, since the app chooses its logging framework; the message is a fixed snake_case event in the past tense, the data in `extra=` fields, never interpolated; no entity field value, secret or identifier in a record, only oids, type names, counts and reason codes; a warning (`warnings.warn`) is for a caller's mistake, a log record for an operational event, never both; no `print()` in `src/`, CLI output lives in `scripts/`.
- **Applies to:** `src/`; a script for its diagnostics, not its output.
- **Enforced by:** review — the PR reviewer checks new log calls and warnings, while ruff `T20` gates `print()` in `src/` since #191; cannot see: user data inside a field. Nothing in `src/` logs today; #195 adds the first events.
- **Source:** tetrahedron decision 553, copied 2026-10-20 (#186), in library form: core dependencies stay `{msgspec, pyroaring}` (invariant 2), so no structlog; an app on structlog renders the `extra` fields through its stdlib bridge (`ProcessorFormatter` with `ExtraAdder`).

### english-only
- **Rule:** Code, comments, docstrings and technical documentation are written in English; a term from another language is translated or, where no English term is established, kept.
- **Applies to:** `src/`, `tests/`, `docs/`, `README.md`, `AGENTS.md`, `CHANGELOG.md`.
- **Enforced by:** review — the PR reviewer checks new identifiers, comments and docs; cannot see: German already in the code.
- **Source:** tetrahedron standard english-only (founder directive, #502), copied 2026-10-20 (#186).

### teaching-docs
- **Rule:** A docs page teaches one thing: it opens with one sentence on what the reader can do after it, then one runnable mineral-cabinet example, then only what the example cannot show; no filler or marketing words and no history, which goes to `CHANGELOG.md` or an ADR; one sentence per line. The README keeps a fixed shape — name and one sentence, status, quickstart in at most five commands, usage, extras, development gates, links — under 150 lines.
- **Applies to:** `README.md`, `docs/tutorial.md`, `docs/how-to/`, `docs/explanation.md`, `docs/GUIDE.md`, `llms.txt`; `docs/reference.md` stays complete and dry (the drift guard) and only takes one sentence per line; `docs/design/` holds records and is out of scope.
- **Enforced by:** ci — the fitness gates run the README quickstart twice and check internal doc links, and the `docs-wrap` job in `.github/workflows/ci.yml` runs `scripts/check_wrap.py --since` on the Markdown lines a PR adds; review for the rest — the PR reviewer reads every changed page against the rule and runs each Python example it adds; cannot see: whether a page teaches, whether an example still runs.
- **Source:** datacrystal's own (owner decision 2026-10-20, #196), after tetrahedron decision 554 (project-readme) and its one-sentence-per-line convention.

### python-layout
- **Rule:** The code lives in `src/datacrystal/`, the tests in `tests/`, helper scripts as self-contained PEP 723 scripts in `scripts/`; `examples/`, `benchmarks/` and `evals/` hold runnable demos, perf gates and proving grounds.
- **Applies to:** the repo.
- **Enforced by:** review — the PR reviewer checks where a new file lands; cannot see: a script that is not self-contained, a package imported by path.
- **Source:** tetrahedron decision 551, copied 2026-10-20 (#186).

## Not adopted

- **spec-by-example** — no `tests/spec/` modules: datacrystal is a library whose rules are already pinned by the fitness gates and the contract vectors, with no founder-approved domain examples to freeze.
- **settings-module** — the library reads no environment; configuration is passed to `Store.open`.
- **health-endpoints** — datacrystal is not a server app; `datacrystal[web]` ships routers, not a service.
