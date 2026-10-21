"""Principals, actors, and the permission ladder (epic #168, W1).

A :class:`Principal` is the in-memory subject a session acts under, holding a level per group.
Core only needs every commit stamped with one; datacrystal is never the identity provider.
:class:`Actor` is the shipped registry entity, so grants ride the same delta stream as the data.
Ladder constants and their rationale: ADR-008 R3.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated

from datacrystal._entity import Index, Unique, entity

# Defined in the _permissions leaf module because _entity needs them and importing
# _actors from there would cycle; re-exported to keep the public dc.* surface (ADR-008 R3).
from datacrystal._permissions import (  # re-exports, all listed in __all__
    ADMIN,
    AGENT,
    AUTOMATION,
    CURATOR,
    EXECUTIVE,
    NO_STANDING,
    STAFF,
    VIEWER,
    WORLD,
)

__all__ = [
    "ADMIN",
    "AGENT",
    "AUTOMATION",
    "CURATOR",
    "EXECUTIVE",
    "NO_STANDING",
    "STAFF",
    "VIEWER",
    "WORLD",
    "Actor",
    "Principal",
    "root_principal",
]


@dataclass(frozen=True)
class Principal:
    """The acting subject: a uid holding a level per group.

    ``memberships`` maps group id to the level held there; there is no global rank.
    ``uid=0`` is the anonymous principal that a store opened without an identity stamps
    (COMMIT-DELTA-v2 §1).
    Frozen pure data; resolution from the registry happens in ``store.acting_as()``.
    """

    uid: int
    memberships: Mapping[int, int] = field(default_factory=dict[int, int])


def root_principal(uid: int, memberships: Mapping[int, int] | None = None) -> Principal:
    """Construct the store root principal, the audited break-glass (ADR-008 R9).

    Returns an ordinary :class:`Principal` holding ``{WORLD: EXECUTIVE}``; it adds no mode, flag or
    bypass path.
    Prefer it to the bare literal because it names the intent.
    Extra ``memberships`` merge in, and the ``WORLD: EXECUTIVE`` sentinel always wins for ``WORLD``.
    """
    m: dict[int, int] = dict(memberships or {})
    m[WORLD] = EXECUTIVE
    return Principal(uid=uid, memberships=m)


@entity(protected=True)
class Actor:
    """The shipped actor registry, one record per human or technical user.

    ``uid`` is the compact integer in commit stamps; ``subject`` is the external key (OIDC ``sub``,
    SCIM id).
    ``sponsor`` is a human's uid, required for every non-human actor (enforced by ``acting_as()``).
    Born protected (ADR-008 W2): W1-era rows decode under the R7 legacy fill.
    Registering new actors needs a non-anonymous session.
    """

    uid: Annotated[int, Unique]
    subject: Annotated[str, Index] = ""
    display: str = ""
    human: bool = False
    sponsor: int | None = None
    memberships: dict[int, int] = field(default_factory=dict[int, int])
