"""``datacrystal[web]`` — reflect your ``@entity`` graph into REST + GraphQL (ROADMAP item 12, #23).

One reflection engine, two targets: the ``@entity`` surface reflected into a
**REST** boundary (Pydantic DTOs over the request edge) *and* a **GraphQL**
boundary (Strawberry types resolved over ``store.snapshot()`` with per-request
DataLoaders). The shared field analysis lives in :mod:`._reflect`; the framework
deps (``pydantic``/``fastapi``/``strawberry``) are imported **only** inside their
submodules and ship **only** with the ``web`` extra — core stays
``{msgspec, pyroaring}`` and ``datacrystal.web`` is deliberately *not* re-exported
from :mod:`datacrystal` (importing it requires the extra; the dep-isolation
fitness gate keeps all three frameworks out of a bare ``import datacrystal``).

The design seam was ratified by the #49 spike (build plan #23); each web surface
cites those in its submodule docstring. This barrel keeps exports **append-only**
and **grouped per submodule** — a later story appends its names into its own group
below, never reshuffling the existing ones.
"""

from __future__ import annotations

# Importing this package pulls the framework-backed submodules, so
# ``import datacrystal.web`` works only with the ``web`` extra installed —
# while a bare ``import datacrystal`` (which never touches this package) stays
# inside the {msgspec, pyroaring} budget (dep-isolation fitness gate).

# --- _reflect: shared TypeInfo → field-descriptor analysis (both targets) -----
from datacrystal.web._reflect import FieldDescriptor, reflect

# --- _pydantic: REST boundary (#96-#98) ---------------------------------------
from datacrystal.web._pydantic import entity_model, to_pydantic

# --- _strawberry: GraphQL boundary (#99-#101) ---------------------------------
from datacrystal.web import _strawberry  # noqa: F401  # pyright: ignore[reportUnusedImport]  # extra home #99-101

# --- _app: FastAPI app wiring (#92) -------------------------------------------
from datacrystal.web import _app  # noqa: F401  # pyright: ignore[reportUnusedImport]  # extra home #92

__all__ = [
    # _reflect
    "FieldDescriptor",
    "reflect",
    # _pydantic (#96-#98): append from_pydantic below
    "entity_model",
    "to_pydantic",
    # _strawberry (#99-#101): append the Strawberry reflection surface
    # _app (#92): append the app factory
]
