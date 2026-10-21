"""``datacrystal[web]`` — reflect your ``@entity`` graph into REST + GraphQL (ROADMAP item 12, #23).

One reflection engine (:mod:`._reflect`), two targets: REST (Pydantic DTOs) and GraphQL
(Strawberry over ``store.snapshot()``).
The framework deps are imported only inside their submodules and ship only with the ``web`` extra,
so ``datacrystal.web`` is deliberately not re-exported from :mod:`datacrystal`.
Exports are append-only and grouped per submodule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# --- _pydantic: REST boundary (#96-#98) — needs only pydantic ------------------
from datacrystal.web._pydantic import entity_model, from_pydantic, to_pydantic

# ``_reflect`` and ``_pydantic`` need at most pydantic (the ``datacrystal[follower]`` install),
# so they import eagerly; the strawberry/fastapi submodules import lazily via ``__getattr__``
# (#153: a follower must not need them; the follower-profile dep-isolation gate guards it).
# --- _reflect: shared TypeInfo → field-descriptor analysis (both targets) -----
from datacrystal.web._reflect import FieldDescriptor, reflect

if TYPE_CHECKING:
    # Static visibility for the lazily-imported names (PEP 562 below) so pyright
    # and ``from datacrystal.web import X`` type-check without an eager import.
    from datacrystal.web._app import (
        SNAPSHOT_CONTEXT_KEY as SNAPSHOT_CONTEXT_KEY,
    )
    from datacrystal.web._app import (
        create_app as create_app,
    )
    from datacrystal.web._app import (
        get_principal as get_principal,
    )
    from datacrystal.web._app import (
        get_store as get_store,
    )
    from datacrystal.web._app import (
        graphql_context_getter as graphql_context_getter,
    )
    from datacrystal.web._app import (
        read_snapshot as read_snapshot,
    )
    from datacrystal.web._app import (
        store_lifespan as store_lifespan,
    )
    from datacrystal.web._app import (
        submit_write as submit_write,
    )
    from datacrystal.web._federation import federation_router as federation_router
    from datacrystal.web._strawberry import (
        LOADER_CONTEXT_KEY as LOADER_CONTEXT_KEY,
    )
    from datacrystal.web._strawberry import (
        SnapshotLoader as SnapshotLoader,
    )
    from datacrystal.web._strawberry import (
        StrawberryReflector as StrawberryReflector,
    )
    from datacrystal.web._strawberry import (
        reflect_strawberry_type as reflect_strawberry_type,
    )
    from datacrystal.web._strawberry import (
        snapshot_context as snapshot_context,
    )

# name → owning submodule, resolved on first access by __getattr__ below.
_LAZY: dict[str, str] = {
    # _strawberry: GraphQL boundary (#99-#101) — needs strawberry
    "LOADER_CONTEXT_KEY": "_strawberry",
    "SnapshotLoader": "_strawberry",
    "StrawberryReflector": "_strawberry",
    "reflect_strawberry_type": "_strawberry",
    "snapshot_context": "_strawberry",
    # _app: FastAPI app wiring (#92) — needs fastapi
    "SNAPSHOT_CONTEXT_KEY": "_app",
    "create_app": "_app",
    "get_principal": "_app",
    "get_store": "_app",
    "graphql_context_getter": "_app",
    "read_snapshot": "_app",
    "store_lifespan": "_app",
    "submit_write": "_app",
    # _federation: replication surface (#149, ROADMAP item 21) — needs fastapi
    "federation_router": "_federation",
}


def __getattr__(name: str) -> Any:
    """Import the submodule owning a strawberry- or fastapi-backed name on first access (PEP 562).

    A follower install (no strawberry/fastapi) can therefore import this package.
    """
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value  # cache: subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    """List the public names, including the lazy ones."""
    return sorted(__all__)


__all__ = [  # noqa: RUF022 - grouped by source module, one comment per group
    # _reflect
    "FieldDescriptor",
    "reflect",
    # _pydantic (#96-#98)
    "entity_model",
    "from_pydantic",
    "to_pydantic",
    # _strawberry (#99-#101)
    "StrawberryReflector",
    "reflect_strawberry_type",
    # _strawberry (#100): per-request DataLoader over a snapshot
    "LOADER_CONTEXT_KEY",
    "SnapshotLoader",
    "snapshot_context",
    # _app (#92): FastAPI lifespan + per-request read/write/context deps
    "SNAPSHOT_CONTEXT_KEY",
    "create_app",
    "get_principal",
    "get_store",
    "graphql_context_getter",
    "read_snapshot",
    "store_lifespan",
    "submit_write",
    # _federation (#149): coordinator replication read endpoints (item 21)
    "federation_router",
]
