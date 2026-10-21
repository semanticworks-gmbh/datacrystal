"""The record-label side of native permissions (epic #168, W2; ADR-008).

A ``Principal`` (:mod:`datacrystal._actors`) is who acts; :class:`Permissions` is what a protected
record carries.
A leaf module at import time: ``_entity`` needs the ladder constants and ``_actors`` imports
``_entity``, so the constants live here.
The label verbs import the engine lazily inside their bodies (the ``_state`` precedent).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

    from datacrystal._actors import Principal

WORLD = 0
"""The world group id: every principal implicitly holds ``{WORLD: VIEWER}``."""

# The ladder (ADR-008 R3), spaced by 100 so levels can be inserted without renumbering.
# AGENT sits below AUTOMATION on purpose: an LLM agent is the least predictable writer, and
# competence is not clearance.
# NO_STANDING is the absence of a shared group, not a grantable level, so "read_floor=VIEWER" still
# means "any member".
NO_STANDING = -1
VIEWER = 0
AGENT = 100
AUTOMATION = 200
STAFF = 300
CURATOR = 400
ADMIN = 500
EXECUTIVE = 600

# The four lib-managed columns @entity(protected=True) injects, in schema order (ADR-008 Context).
# The `_dc_` prefix is reserved on every entity class; PersistentList/PersistentDict reuse
# `_dc_owner` for an unrelated backref.
PERM_FIELDS = ("_dc_owner", "_dc_groups", "_dc_read_floor", "_dc_write_floor")


# The R7 legacy fill (ADR-008): the labels of a record persisted before its class turned protected.
# Deliberately unlike the R6 birth defaults; one constant so hydration, snapshot decode, index build
# and the gate agree.
PERM_LEGACY_FILLS: dict[str, Callable[[], Any]] = {
    "_dc_owner": lambda: 0,          # nobody — and uid 0 never matches (R7a)
    "_dc_groups": lambda: [WORLD],
    "_dc_read_floor": lambda: VIEWER,
    "_dc_write_floor": lambda: ADMIN,
}


@dataclass(frozen=True, slots=True)
class Permissions:
    """A protected record's security label, as one frozen view (read it via
    ``record.dc_permissions``).

    ``owner`` is the registering uid (0 means nobody, the R7a sentinel), ``groups`` the compartments
    it is shared into, and the floors the minimum authority to read or write.
    ``groups`` is a point-in-time tuple copy, never the record's live list.
    """

    owner: int
    groups: tuple[int, ...]
    read_floor: int
    write_floor: int


class LabelColumns(Protocol):
    """The four lib-managed label columns, as a ``protected=True`` entity carries them."""

    _dc_owner: int
    _dc_groups: list[int]
    _dc_read_floor: int
    _dc_write_floor: int


# --- the label verbs (W2-4) ---------------------------------------------------


def _staged(rec: object) -> LabelColumns:
    """Validate that ``rec`` is a live, mutable, protected entity and return its label columns.

    This is the shared preamble of the label verbs.
    Raises before any mutation, so a refused verb stages nothing.
    The verbs perform no authority check: the commit gate checks the committing principal
    (maker-checker flow).
    """
    from datacrystal._entity import is_entity, type_info
    from datacrystal._errors import FrozenEntityError

    if not is_entity(rec):
        from datacrystal._snapshot import EntityView

        if isinstance(rec, EntityView):
            raise AttributeError(
                "snapshot views are read-only — labels change on the live "
                "record (get it via store.get()/query()), never on a view"
            )
        type_info(rec)  # raises NotAnEntityError with the standard message
    info = type_info(rec)
    if not info.protected:
        raise TypeError(
            f"{type(rec).__name__} is not protected — share()/unshare()/"
            "protect() need @entity(protected=True) (ADR-008); unprotected "
            "classes carry no permission columns"
        )
    if info.frozen:
        raise FrozenEntityError(
            f"{type(rec).__name__} is an @entity(frozen=True) record — its "
            "labels were fixed at registration (container inheritance or "
            "birth defaults) and can never be relabeled"
        )
    # no static type says "an @entity(protected=True) class": decoration injects the columns
    return cast("LabelColumns", rec)


def _check_level(name: str, level: object) -> None:
    """Raise if ``level`` is not a grantable ladder level."""
    if type(level) is not int:
        raise TypeError(f"{name} must be an int ladder level, got {level!r}")
    if level < VIEWER:
        raise ValueError(
            f"{name}={level} is not a grantable level — NO_STANDING is the "
            "absence of standing, not a floor (the ladder starts at VIEWER)"
        )


def _check_group(group: object) -> None:
    """Raise if ``group`` is not an int group id."""
    if type(group) is not int:
        raise TypeError(f"group must be an int group id, got {group!r}")


def share(rec: object, group: int, *, read: int, write: int) -> None:
    """Share ``rec`` into ``group`` and set the record-wide floors; both levels are required (R6).

    Staging only: the commit gate decides whether you may set these floors (ADR-008 R8).

    Raises:
        TypeError: not a protected entity, or ``group`` or a level is not an int.
        ValueError: a level below ``VIEWER``.
        FrozenEntityError: ``rec`` is ``frozen=True`` (``DeletedEntityError`` if deleted).
    """
    labelled = _staged(rec)
    _check_group(group)
    _check_level("read", read)
    _check_level("write", write)
    labelled._dc_read_floor = read
    labelled._dc_write_floor = write
    groups = labelled._dc_groups
    if group not in groups:
        groups.append(group)  # in place: the owner-bound list dirty-marks rec


def unshare(rec: object, group: int) -> None:
    """Remove ``group`` from ``rec``'s groups; an absent group is a no-op that buffers no write.

    Floors are untouched; use :func:`protect`.
    """
    labelled = _staged(rec)
    _check_group(group)
    groups = labelled._dc_groups
    if group in groups:
        groups.remove(group)


def protect(rec: object, *, read: int | None = None, write: int | None = None) -> None:
    """Set ``rec``'s read and/or write floor, never its groups; at least one keyword is required
    (R6).
    """
    labelled = _staged(rec)
    if read is None and write is None:
        raise TypeError(
            "protect() needs read= and/or write= — a bare protect() would "
            "be a silent no-op grant (ADR-008 R6: explicit levels only)"
        )
    if read is not None:
        _check_level("read", read)
    if write is not None:
        _check_level("write", write)
    if read is not None:
        labelled._dc_read_floor = read
    if write is not None:
        labelled._dc_write_floor = write


# --- the normative predicates (ADR-008 Context, transcribed verbatim) -----------


def level(p: Principal, g: int) -> int:
    """Return the level ``p`` holds in group ``g``; every principal holds at least ``VIEWER`` in
    ``WORLD`` (ADR-008).
    """
    return p.memberships.get(g, VIEWER if g == WORLD else NO_STANDING)


def is_owner(p: Principal, owner: int) -> bool:
    """Return whether ``p`` owns a record with ``owner``; uid 0 is anonymous, so ``_dc_owner == 0``
    means nobody (R7a).
    """
    return p.uid != 0 and owner == p.uid


def authority_towards(p: Principal, owner: int, groups: Iterable[int]) -> int:
    """Return the highest level ``p`` holds in any group the record is shared with.

    The owner of an unshared record acts at their personal-best level (ADR-008, F3 amendment
    2026-07-16).
    The boost tests ``not levels``, not ``not groups``, so a one-shot iterable is consumed once.
    Pure over plain values: shared by the commit gate, the ceiling check and the readable-set
    compiler.
    """
    levels = [level(p, g) for g in groups]
    if is_owner(p, owner) and not levels:
        levels.append(max(p.memberships.values(), default=VIEWER))
    return max(levels, default=NO_STANDING)


def is_root(p: Principal) -> bool:
    """Return whether ``p`` is the store root: EXECUTIVE held in WORLD, the audited break-glass
    (R9).

    ``uid != 0`` keeps the anonymous principal from ever being root (R7a).
    """
    return p.uid != 0 and p.memberships.get(WORLD, VIEWER) >= EXECUTIVE


def can_read_row(p: Principal, owner: int, groups: Iterable[int], read_floor: int) -> bool:
    """Return the ADR-008 ``can_read`` predicate over decoded label values (W3-1).

    Pure, builds no entity and touches no registry; root (R9) is the one bypass.
    :func:`datacrystal._indexes.readable_bitmap` is its bitmap-compiler twin.
    The owner clause comes first, so ownership alone grants a read.
    """
    if is_root(p):
        return True
    return is_owner(p, owner) or authority_towards(p, owner, groups) >= read_floor
