"""``datacrystal[web]`` REST boundary — reflect ``@entity`` into Pydantic (#23 / #49 S2-S4).

The REST half of the web tier: the shared reflection in :mod:`._reflect` is
turned into a Pydantic model here (``entity_model`` / ``to_pydantic`` /
``from_pydantic``, landing in #96-#98), giving FastAPI a typed boundary DTO
without leaking the live engine object across the request edge.

``pydantic`` is imported **only at this submodule's top** — never from core and
never from :mod:`._reflect` — so plain ``import datacrystal`` stays inside the
``{msgspec, pyroaring}`` budget (fitness gate ``test_import_isolation_*``).

Skeleton stub (#95): the reflection-to-Pydantic surface lands in later Sprint 9
stories (#96 ``entity_model`` · #97 ``to_pydantic`` · #98 ``from_pydantic`` +
FastAPI e2e). This module exists now so the framework import has a home that the
isolation gate already covers.
"""

from __future__ import annotations

import pydantic  # noqa: F401  # pyright: ignore[reportUnusedImport]  # extra-only home for #96-#98

__all__: list[str] = []
