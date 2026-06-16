# Documentation Audit & Improvement Guide

**For:** Claude Code, operating inside a project repository.
**Goal:** Assess the quality of a software library's documentation against community best practice, then improve it incrementally — without making it worse in the process.

Treat this file as a playbook. Read the whole thing once, then follow the **Workflow** at the bottom.

---

## 1. The mental model: Diátaxis

Almost every well-regarded documentation set in the industry maps onto one framework: **Diátaxis** (Daniele Procida). It is the de-facto standard, adopted by Python, Django, Canonical/Ubuntu, Gatsby, and many others. Internalize it before judging anything.

Diátaxis says every documentation page serves exactly **one** of four user needs. Mixing them on one page is the single most common cause of confusing docs.

| Mode | User is… | Answers | Voice | Test |
|------|----------|---------|-------|------|
| **Tutorial** | learning by doing | "Teach me, get me a first win" | "we will…" hand-held | Can a beginner follow it start-to-finish without making a single decision? |
| **How-to guide** | working toward a goal | "How do I achieve X?" | "to do X, do Y" task-focused | Does it solve one real, named problem for someone who already knows the basics? |
| **Reference** | needs to look something up | "What exactly is the signature/param/return?" | dry, complete, consistent | Is it accurate, exhaustive, and boring? (Boring is correct here.) |
| **Explanation** | wants to understand | "Why does it work this way?" | discursive, "why/trade-offs" | Could you read it away from the keyboard and still get value? |

**The cardinal rule:** a tutorial is not a how-to is not a reference is not an explanation. When a page tries to teach *and* be a complete reference *and* explain the design rationale at once, split it.

The classic engineer's mistake is leading with explanation ("How it works") because that's the author's mental model. Readers don't onboard that way — they want a **first success** first, understanding later.

---

## 2. What "good" looks like — patterns from the best

The exemplars the dev community repeatedly cites — **Stripe, Twilio, FastAPI, Django, Slack, Google Maps API** — share concrete, copyable patterns. Use these as the positive checklist.

1. **Time-to-first-success is brutally short.** A quickstart gets the reader to one working call / one running example in minutes, with copy-pasteable commands. No prerequisites essay before the first win.
2. **Every code example is copy-paste-ready and runnable.** Real, complete snippets — not pseudo-fragments with `...` where the hard part should be. Stripe/Twilio show request *and* example response side by side.
3. **Examples in the reader's language/context.** Multiple languages where relevant; at minimum, idiomatic for the library's own ecosystem.
4. **Goal-oriented navigation, not just an API dump.** Twilio organizes around tasks ("Send a message", "Verify a number"), not just an alphabetical endpoint list. The reader is *building something*, not browsing a catalog.
5. **Reference is generated from / kept in sync with the source.** Signatures, params, types, defaults, return values, and raised errors are complete and accurate. Stale reference is worse than none.
6. **Errors are documented.** What can go wrong, what the error means, how to recover. This is the most-skipped, highest-value section.
7. **Clean information architecture.** Predictable layout, persistent nav, search. Stripe's near-canonical 3-column layout (nav / prose / code) exists because it lets a reader locate and copy without losing place.
8. **Explanatory copy that respects the reader.** Twilio is praised for prose that actually explains concepts ("What's a REST API, anyway?") rather than terse one-liners — without being condescending.
9. **Versioning is explicit.** Which version does this doc describe? What changed? Is there a migration/upgrade guide and a changelog?

---

## 3. The README is the front door — audit it specifically

For a library, the `README` is usually the highest-traffic doc. A strong one converges on this anatomy (omit sections that don't apply; don't pad):

- **Title + one-line value statement** — what it is and the primary benefit, in one sentence, above the fold.
- **Badges** (build, version, coverage, license) — only if real and maintained.
- **The problem / why it exists** — one short paragraph. When it's non-obvious why someone would reach for this.
- **Install** — copy-paste, with the exact package-manager command(s) and prerequisites/version requirements.
- **Quickstart / minimal example** — the smallest complete program that does something useful and *actually runs*.
- **Key features** — scannable, benefit-led, not a wall of prose.
- **Links to fuller docs** — tutorials, how-tos, API reference, hosted site.
- **Configuration** — env vars, flags, defaults (or link to them).
- **How it works / architecture** — only when behavior is non-obvious; keep it short or link out.
- **Limitations / scope / non-goals** — sets expectations, prevents misuse. Underrated trust signal.
- **Contributing / dev setup** — how to build and test locally.
- **License + support/community** — where to ask, how to report issues.

Red flag: an install or quickstart block that can't actually be executed as written. Research shows a majority of install instructions in the wild fail when followed literally. **Verify them.**

---

## 4. Assessment rubric

Score each dimension **0–3** (0 = absent, 1 = present but poor, 2 = solid, 3 = exemplary). Record evidence (file + line) for each, not just the score.

| # | Dimension | What 3/3 looks like |
|---|-----------|---------------------|
| 1 | **First success** | Quickstart yields a working result in minutes; runnable as written. |
| 2 | **Diátaxis hygiene** | Tutorials, how-tos, reference, explanation exist and are not blended on one page. |
| 3 | **Reference completeness** | Every public API has documented params, types, defaults, returns, raised errors. |
| 4 | **Reference accuracy** | Docs match current source; no drift. Spot-check signatures against code. |
| 5 | **Examples** | Complete, runnable, idiomatic, with expected output. No mystery `...`. *Python: docstring examples are doctest-runnable.* |
| 6 | **Error/edge coverage** | Common failures and recovery are documented. *Python: docstring `Raises:` sections present.* |
| 7 | **Navigation & findability** | Predictable structure, working ToC/nav, search if hosted, no dead links. |
| 8 | **Onboarding prose** | Concepts explained for a newcomer without condescension; jargon defined on first use. |
| 9 | **Versioning** | Version stated; changelog present; migration guide for breaking changes. *Python: docs version == packaged version.* |
| 10 | **Maintainability** | Reference generated from source (autodoc/mkdocstrings), not hand-written; consistent docstring style; type hints on public signatures; CI checks docs (links/build/doctest). |
| 11 | **Agent-readability** | Machine-parseable structure, stable headings, complete copyable fenced code, text-based reference. `llms.txt` is a small bonus, not required. |

**Interpretation:** sum / 33. Below ~50% = structural problems, plan a real pass. 50–75% = solid, target the lowest-scoring dimensions. Above 75% = polish only.

---

## 5. The second audience: AI agents

A large and growing share of documentation consumption is no longer a human in a browser — it's a coding agent (Cursor, Windsurf, Claude Code, Copilot, Cline, Aider) pulling docs into context to answer a developer's question or write code *against your library*. The typical pattern: the agent identifies which dependency owns a feature, fetches that library's docs, and pulls the relevant pages before generating code.

**Separate the principle from the artifact — they have different maturity:**

- **The principle has fully landed in best practice.** Designing docs to be machine-parseable is now standard advice, not a frontier idea. And it requires almost nothing new: docs that read well for an LLM are the same as docs that read well for a human. The agent-friendly checklist *is* the quality checklist:
  - **Stable, descriptive headings** and a logical hierarchy (agents chunk by heading).
  - **Fenced code blocks with language tags** (` ```python `), complete and self-contained — imports included, no `...`.
  - **Explicit over implicit:** name the function/module the example uses; never let a screenshot carry meaning.
  - **No load-bearing images:** anything essential conveyed only in a diagram is invisible to an agent and to screen readers. Caption or restate in text.
  - **Text-based reference**, not just rendered HTML tables that lose structure when scraped.

- **The `llms.txt` artifact is adopted but not standardized — ship it, don't oversell it.** `llms.txt` / `llms-full.txt` are a community convention (proposed Sept 2024) that IDE/coding agents routinely fetch from a docs site's root, and tooling auto-generates them (Mintlify, GitBook, etc.). For a library, the concrete benefit is real: an agent integrating your lib fetches the file and reads only the canonical pages. **But** it is *not* a formal standard, no major model provider has committed to honouring it in production, and at least one large study found no measurable effect on AI citation rates. Treat it as: low cost, low risk, genuinely useful for IDE agents, **but not a substitute for well-structured docs**. The file only indexes quality that already exists.

> **Bottom line for the auditor:** score the underlying structure (headings, runnable fenced code, text-based reference) heavily. Treat presence of `llms.txt` as a small bonus, not a core requirement — and never recommend generating per-page Markdown mirrors that get indexed, as that creates duplicate-content problems.

---

## 5b. Python specifics (this project is a Python library)

The principles above are language-agnostic; here is how they cash out for a Python package. Audit these concretely.

### Docstrings are the foundation
- **Every public module, class, function, and method has a docstring.** The public surface is what's exported — check `__all__` and the package's `__init__.py`. Private/underscore-prefixed names can be lighter.
- **One docstring style, used consistently** across the whole codebase. Mixed styles are a primary audit finding — see *Choosing & enforcing a docstring style* below for which to pick and how to lint it automatically.
- **Docstrings document:** summary line → extended description (the "why") → `Args`/`Parameters` → `Returns` → `Raises` → `Examples`. The `Raises` section is the most-skipped and high value.
- **Lean on type hints instead of repeating types in prose.** With PEP 484 hints on signatures, the doc generator renders types automatically — don't duplicate `param (int):` when the signature already says `param: int`. Audit for missing hints on public signatures.

### Choosing & enforcing a docstring style

**Two separate things to check — don't conflate them:**
1. **Format consistency** — does every docstring follow the *same* convention and PEP 257 mechanics (triple quotes, imperative summary, section layout)?
2. **Signature ↔ docstring agreement** — do the documented `Args`/`Returns`/`Raises` actually match the real parameters, return, and exceptions? A perfectly-formatted docstring can still lie.

Note: **type checkers do not check either of these.** Pyright / mypy / ty verify types only and ignore docstrings entirely. This needs a dedicated docstring linter.

**Default style: Google.** Pick **NumPy** instead only for scientific / numeric / array-heavy libraries (it's the convention in numpy, scipy, pandas, scikit-learn, and its layout handles long parameter lists and array shapes well). The full set:

| Style | Looks like | Use it when |
|-------|-----------|-------------|
| **Google** *(default)* | `Args:` / `Returns:` / `Raises:` indented sections | Almost always — most readable, lowest ceremony, general-purpose. |
| **NumPy** | underlined `Parameters` / `Returns` headings | Scientific/data ecosystem or many-parameter, array-typed APIs. |
| **reStructuredText / Sphinx** | inline `:param x:` / `:returns:` fields | Only if you have a specific Sphinx-native reason; verbose and least human-readable. |
| **Epytext** | `@param` / `@return` (Javadoc-like) | Never for new code — effectively dead. |

> PEP 257 is **not** one of these — it's the baseline mechanics (triple quotes, imperative mood, one-line vs. multi-line). Google/NumPy/Sphinx are section layouts on top of it. All three are supported by every relevant tool (Sphinx napoleon, mkdocstrings/Griffe, Ruff, pydoclint), so this is a readability/ecosystem choice, not a tooling lock-in. **The thing that actually matters: pick one and enforce it in CI.**

**Enforcement tool — Ruff is the gold standard.** Use it; reach for the alternatives only in edge cases.

- **Ruff** (`astral-sh/ruff`) is the winner because it does *both* checks in one fast tool, configured from `pyproject.toml`:
  - `D` rules = a full reimplementation of **pydocstyle** (format/style). Set `convention = "google"` and it enforces exactly that style and auto-disables the rules that conflict with it.
  - `DOC` rules = **pydoclint** integrated (signature ↔ docstring match; currently behind `preview = true`).
  - It's effectively the consolidation point for this whole category — pydocstyle's own maintainers now tell users to switch to Ruff, and it replaces a stack of older single-purpose plugins with one binary that's already most projects' linter/formatter anyway.
  - **Gotcha to check:** the undocumented-parameter rule (`D417`) is only active under the **google** convention by default — another reason Google is the pragmatic pick.

- **Other tools (reference only — don't add them if Ruff is in place):**
  - **pydocstyle** — the original format/style checker; now deprecated and points users to Ruff. Only seen in older repos.
  - **pydoclint** — standalone signature-match linter (Google/NumPy/Sphinx), very fast, with a "baseline" feature for adopting it on legacy code gradually. Use standalone only if not on Ruff or you want that baseline workflow.
  - **darglint** — the old signature-match checker; abandoned and thousands of times slower than pydoclint. Do not use; migrate off it if found.
  - **interrogate** — measures docstring *coverage* (% of API with any docstring) and emits a badge. Complementary, not a style checker; optional if Ruff's `D1xx` missing-docstring rules already fail CI.
  - **docformatter** — auto-*fixes* PEP 257 formatting (wrapping, quotes). A formatter, not a linter; it won't convert between Google/NumPy section styles.

Minimal `pyproject.toml` to enforce all of the above:
```toml
[tool.ruff.lint]
preview = true                    # enables the DOC (pydoclint) signature-match rules
select = ["D", "DOC"]             # D = format/style, DOC = signature agreement
extend-ignore = ["D105", "D107"]  # optional: skip magic methods / __init__

[tool.ruff.lint.pydocstyle]
convention = "google"             # or "numpy" for scientific libs — the consistency enforcer

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["D"]                # don't require docstrings in tests
```

**Audit action:** confirm a convention is actually configured (not just docstrings present). If `[tool.ruff.lint.pydocstyle].convention` is unset and styles look mixed, flag it and propose the config above with the style matched to whatever the majority of existing docstrings already use, to minimize churn.


### Make examples executable — the Python superpower
- Examples in docstrings can be **run as tests** via `doctest`. This is the strongest possible guarantee that the snippets a human *or an agent* copies actually work.
- Check whether the project runs them: `python -m doctest -v src/...`, or `pytest --doctest-modules`, ideally wired into CI.
- **If examples aren't doctested, that's a top remediation target** — it directly enforces the "every example must run" rule and prevents drift for free.

### Reference generation (don't hand-write it)
Identify which toolchain the project uses (or recommend one) and that reference is generated from docstrings, not maintained by hand:
- **Sphinx** + `autodoc` + `napoleon` (for Google/NumPy docstrings) + `sphinx-autodoc-typehints` + `intersphinx` (cross-links to the stdlib and other libs' docs). The mature, API-first default; standard for scientific Python and CPython itself.
- **MkDocs** + **Material for MkDocs** + **`mkdocstrings[python]`** (which uses Griffe to extract docstrings). Markdown-native, modern, popular for newer projects. *Note: Material for MkDocs entered maintenance mode in early 2026, with a successor ("Zensical") that reads existing `mkdocs.yml`; flag this if recommending a fresh setup, but existing setups remain fine.*
- Reasonable default: **MkDocs + Material + mkdocstrings** if contributors prefer Markdown; **Sphinx** if the docs are API-first or need its richer cross-reference/extension ecosystem.

### Hosting & packaging hygiene
- **Read the Docs** is the standard host for open-source Python packages: builds on push, versioned URLs (`latest`/`stable`/per-tag). Check for `.readthedocs.yaml`.
- **`pyproject.toml` hygiene:** the PyPI page is rendered from the README (`readme`/`long_description`) — so README quality *is* PyPI quality. Check `project.urls` points to Docs / Source / Issues. Confirm the version in docs matches the packaged version.
- **Isolate the docs toolchain** from runtime deps — a PEP 735 `[dependency-groups]` `docs` group or a `docs` optional-dependency extra, never mixed into install requirements.
- **`CHANGELOG`** present and updated; for a library, an explicit **deprecation policy** and **migration notes** across major versions matter more than for an app.

### Python-flavored quick audit commands
```bash
# Public API surface vs. documented surface
grep -rn "__all__" src/ ; grep -rn '^\s*def \|^\s*class ' src/ | grep -v '    def _\|class _'
# Docstring style consistency + signature match (the consistency check)
ruff check --select D,DOC --preview src/ 2>&1 | tail -n 30
grep -n "convention" pyproject.toml   # is a docstring convention actually configured?
# Are docstring examples runnable?
pytest --doctest-modules -q 2>&1 | tail -n 20   # or: python -m doctest src/**/*.py
# Type-hint coverage on public signatures (if mypy configured)
mypy --strict src/ 2>&1 | tail -n 20
# Docs build cleanly? (whichever applies)
mkdocs build --strict   # or:  sphinx-build -W -b html docs/ docs/_build
```

---

## 6. Anti-patterns to flag

- **The blended page** — one page that teaches, references, and philosophizes simultaneously. Split it.
- **Explanation-first onboarding** — "Architecture" / "How it works" as the landing page instead of a quickstart.
- **Phantom examples** — snippets with `...`, undefined variables, or missing imports that can't run.
- **Stale reference** — params/signatures that no longer match the code.
- **Dead links / broken anchors / missing images.**
- **Undefined jargon** — domain terms used before they're introduced.
- **No errors documented** — happy path only.
- **No "why would I use this"** — feature list with no problem statement.
- **Version ambiguity** — no indication of which release the docs describe.
- **Wall-of-text README** with no scannable structure.
- **Python: hand-maintained API reference** that has drifted from the code instead of generated from docstrings.
- **Python: mixed docstring styles** across the codebase (Google here, NumPy there), or no `convention` configured in Ruff to enforce one.
- **Python: types repeated in prose** that contradict the actual type hints.
- **Python: examples that were never doctested** and silently broke.

---

## 7. Remediation rules (how to improve without breaking things)

Diátaxis itself warns against tearing everything down. Improve in small, safe increments.

1. **Audit before editing.** Produce the assessment report (Section 8) first. Do not start rewriting on contact.
2. **Fix the highest-leverage, lowest-risk things first**, in this rough order:
   a. Broken/non-runnable install & quickstart (highest leverage — it's the front door).
   b. Inaccurate reference (actively misleading).
   c. Dead links, broken examples, undefined jargon.
   d. Missing error documentation.
   e. Structural Diátaxis splits (higher effort — do deliberately, not in bulk).
3. **Verify every code sample and command you touch.** If the environment allows, actually run them. An example you haven't run is a guess.
4. **Preserve voice and existing conventions.** Match the project's tone, heading style, and terminology. You are improving their docs, not imposing yours.
5. **One concern per change.** Keep edits reviewable. Prefer several focused commits/PRs over one sprawling rewrite. Use descriptive commit messages.
6. **Don't invent.** If you can't determine a param's behavior or a default from the source, say so and flag it for a human — never fabricate reference detail.
7. **Generate reference from source where possible** rather than hand-maintaining it (docstrings + a doc generator), so it can't drift again.
8. **Add a guardrail.** Where feasible, propose a CI check (link checker, doc build, doctest/example runner) so quality doesn't regress.
9. **Leave the structure better-organized than you found it**, but only restructure where the payoff is clear. Random reorganization is churn.

---

## 8. Workflow & output

When asked to assess (and optionally improve) a project's docs, do this:

**Step 1 — Inventory.** Find all docs: `README*`, `docs/`, `CONTRIBUTING*`, `CHANGELOG*`, hosted-doc config (`mkdocs.yml`, `docusaurus.config.*`, Sphinx `conf.py`, etc.), in-source docstrings/comments, and any `llms.txt`. List what exists and classify each by Diátaxis mode.

**Step 2 — Cross-check against source.** Spot-check that the public API surface in the code is actually documented, and that documented signatures match real ones. Note gaps and drift.

**Step 3 — Verify the front door.** Attempt the install and quickstart exactly as written. Record whether they work.

**Step 4 — Score.** Fill in the Section 4 rubric, with file/line evidence per dimension.

**Step 5 — Report.** Emit a concise report:

```
## Documentation Audit: <project>

### Score: <sum>/33 (<percent>%)

### Dimension scores
| Dimension | Score | Evidence | Issue |
| ... one row per rubric line ... |

### Top findings (ranked by leverage × risk)
1. <finding> — <impact> — <file:line>
...

### Recommended actions (ordered)
- [ ] <smallest high-value fix>
- [ ] ...

### Quick wins vs. larger efforts
Quick wins: ...
Larger efforts (need human sign-off): ...
```

**Step 6 — Improve (only if asked).** Work the action list top-down following the Section 7 rules: small, verified, voice-preserving changes, one concern at a time. After each batch, restate what changed and what's left.

---

### One-line summary to keep in mind
**Get the reader to a first success fast, never blend the four doc types on one page, make every example actually run, and keep reference in sync with the code.**
