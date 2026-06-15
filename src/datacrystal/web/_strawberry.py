"""``datacrystal[web]`` GraphQL boundary — reflect ``@entity`` into Strawberry (#23 / #49 S6).

The GraphQL half of the web tier: the shared reflection in :mod:`._reflect` is
turned into a Strawberry type here, resolving edges over a pinned
``store.snapshot()`` with a per-request DataLoader so a deep graph query stays
O(depth) batched reads, not O(nodes) (the no-N+1 gate, #101). Targets land in
#99 (reflect → Strawberry type) · #100 (per-request DataLoader) · #101 (op-count
gate).

``strawberry`` is imported **only at this submodule's top** — never from core and
never from :mod:`._reflect` — so plain ``import datacrystal`` stays inside the
``{msgspec, pyroaring}`` budget (fitness gate ``test_import_isolation_*``).

Skeleton stub (#95): the reflection-to-Strawberry surface lands in the later
Sprint 9 GraphQL stories. This module exists now so the framework import has a
home that the isolation gate already covers.
"""

from __future__ import annotations

import strawberry  # noqa: F401  # pyright: ignore[reportUnusedImport]  # extra-only home for #99-#101

__all__: list[str] = []
