"""``datacrystal[web]`` app wiring — FastAPI app over a store (#23 / #49 S; #92).

Where the REST (:mod:`._pydantic`) and GraphQL (:mod:`._strawberry`) boundaries
are mounted onto a single FastAPI application: store lifespan (open on startup /
close on shutdown), per-request snapshot dependencies, and the deployment
doctrine (single-writer owner thread, ADR-001). Lands in #92, after #98 + #100.

``fastapi`` is imported **only at this submodule's top** — never from core and
never from :mod:`._reflect` — so plain ``import datacrystal`` stays inside the
``{msgspec, pyroaring}`` budget (fitness gate ``test_import_isolation_*``).

Skeleton stub (#95): the app factory and request wiring land in #92. This module
exists now so the framework import has a home that the isolation gate already
covers.
"""

from __future__ import annotations

import fastapi  # noqa: F401  # pyright: ignore[reportUnusedImport]  # extra-only home for #92

__all__: list[str] = []
