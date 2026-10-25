"""W3-1 oracle support: a labeled Rock corpus and the ADR-008 access predicate, transcribed.

The corpus mixes freshly protected rows with explicit (owner, groups, read_floor) labels
(committed under a root principal, which bypasses the W2-5 write gate entirely, so arbitrary
label combinations are reachable) and LEGACY rows (committed before the class turned
``protected=True``, decoding under the ADR-008 R7 fill). The principals span every ADR-008
category: anonymous, uid-only, single/multi-membership, owner-of-a-row, root.

``oracle_can_read`` is a LITERAL transcription of the ADR-008 Context predicate block, written
independently of ``_permissions.py`` so a shared bug in the library code cannot cancel out
against the oracle.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from hypothesis import strategies as st

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend

ROOT = dc.Principal(uid=1, memberships={dc.WORLD: dc.EXECUTIVE})
ANONYMOUS = dc.Principal(uid=0, memberships={})
OUTSIDE_ROOT = dc.Principal(uid=999, memberships={dc.WORLD: dc.EXECUTIVE})
REQUIRED = object()

UIDS = [2, 3, 4, 5]
GROUPS = [dc.WORLD, 7, 8, 9]
LADDER = [dc.VIEWER, dc.AGENT, dc.AUTOMATION, dc.STAFF, dc.CURATOR, dc.ADMIN, dc.EXECUTIVE]

Labels = tuple[int, list[int], int]  # (owner, groups, read_floor)
LEGACY_LABELS: Labels = (0, [dc.WORLD], dc.VIEWER)  # ADR-008 R7, hardcoded independently


def rock(*, protected: bool, **fields: tuple[Any, Any]) -> type[Any]:
    """(Re)define the 'Rock' entity with these fields under the SAME typename.

    The schema-evolution retrofit pattern (see tests/unit/test_schema_evolution.py /
    test_protected_stamping.py), used here to fabricate a store whose Rock lineage mixes
    pre-protection (legacy) rows with freshly protected ones.
    """
    annotations: dict[str, Any] = {}
    namespace: dict[str, Any] = {
        "__module__": __name__,
        "__qualname__": "Rock",
        "__annotations__": annotations,
    }
    for name, (annotation, default) in fields.items():
        annotations[name] = annotation
        if default is not REQUIRED:
            namespace[name] = default
    # a class built at runtime from a field table is untypeable by design
    return dc.entity(type("Rock", (), namespace), protected=protected)  # pyrefly: ignore[no-matching-overload]


def protected_rock() -> type[Any]:
    """Return the protected Rock entity with a unique name."""
    return rock(protected=True, name=(Annotated[str, dc.Unique], REQUIRED))


# --- the literal, independent oracle (ADR-008 Context, transcribed by hand) ----


def _oracle_level(p: dc.Principal, g: int) -> int:
    return p.memberships.get(g, dc.VIEWER if g == dc.WORLD else dc.NO_STANDING)


def _oracle_is_owner(p: dc.Principal, owner: int) -> bool:
    return p.uid != 0 and owner == p.uid


def _oracle_authority_towards(p: dc.Principal, owner: int, groups: list[int]) -> int:
    levels = [_oracle_level(p, g) for g in groups]
    if _oracle_is_owner(p, owner):
        levels.append(max(p.memberships.values(), default=dc.VIEWER))
    return max(levels, default=dc.NO_STANDING)


def _oracle_is_root(p: dc.Principal) -> bool:
    return p.uid != 0 and p.memberships.get(dc.WORLD, dc.VIEWER) >= dc.EXECUTIVE


def oracle_can_read(p: dc.Principal, owner: int, groups: list[int], read_floor: int) -> bool:
    """Say whether the principal may read a row with these labels, per ADR-008."""
    if _oracle_is_root(p):
        return True
    return _oracle_is_owner(p, owner) or _oracle_authority_towards(p, owner, groups) >= read_floor


# --- hypothesis strategies -----------------------------------------------------


@st.composite
def labeled_rows(draw: st.DrawFn) -> dict[str, Any]:
    """Draw the explicit labels of a freshly protected row."""
    owner = draw(st.sampled_from([0, *UIDS]))
    groups = draw(st.lists(st.sampled_from(GROUPS), min_size=0, max_size=3, unique=True))
    read_floor = draw(st.sampled_from(LADDER))
    return {"owner": owner, "groups": groups, "read_floor": read_floor}


@st.composite
def owned_rows(draw: st.DrawFn) -> dict[str, Any]:
    """Draw the labels of a row that has an owner."""
    row = draw(labeled_rows())
    row["owner"] = draw(st.sampled_from(UIDS))
    return row


@st.composite
def _drawn_principals(draw: st.DrawFn) -> dc.Principal:
    kind = draw(st.sampled_from(["anonymous", "uid_only", "single", "multi", "root"]))
    if kind == "anonymous":
        return ANONYMOUS
    uid = draw(st.sampled_from(UIDS))
    if kind == "root":
        return dc.Principal(uid=uid, memberships={dc.WORLD: dc.EXECUTIVE})
    if kind == "uid_only":
        return dc.Principal(uid=uid, memberships={})
    if kind == "single":
        g = draw(st.sampled_from(GROUPS))
        lvl = draw(st.sampled_from(LADDER))
        return dc.Principal(uid=uid, memberships={g: lvl})
    gs = draw(st.lists(st.sampled_from(GROUPS), min_size=2, max_size=3, unique=True))
    mem = {g: draw(st.sampled_from(LADDER)) for g in gs}
    return dc.Principal(uid=uid, memberships=mem)


principals = st.one_of(_drawn_principals(), st.just(ANONYMOUS), st.just(OUTSIDE_ROOT))


# --- the corpus -------------------------------------------------------------------


@dataclass
class Corpus:
    """A memory-backed store of Rock rows and the labels each was INTENDED to carry.

    ``oracle`` maps a row's name to its labels; it is the independent ground truth, never read
    back from the store.
    ``oids`` maps the same names to the rows' OIDs, in the order the rows were created.
    """

    store: dc.Store
    rock: type[Any]
    oracle: dict[str, Labels]
    oids: dict[str, int]


class Relabelable(Protocol):
    """A protected record whose labels ``dc_permissions`` reads and assigns."""

    dc_permissions: dc.Permissions


def seed_labels(rec: Relabelable, owner: int, groups: list[int], read_floor: int) -> None:
    """Relabel a stored Rock through ``dc_permissions``, keeping its write floor."""
    rec.dc_permissions = dc.Permissions(
        owner=owner,
        groups=tuple(groups),
        read_floor=read_floor,
        write_floor=rec.dc_permissions.write_floor,
    )


@contextmanager
def labeled_corpus(n_legacy: int, labeled: list[dict[str, Any]]) -> Generator[Corpus]:
    """Yield a store with ``n_legacy`` legacy rows and one protected row per ``labeled`` dict."""
    backend = MemoryBackend()
    if n_legacy:
        unprotected = rock(protected=False, name=(Annotated[str, dc.Unique], REQUIRED))
        legacy_store = dc.Store._from_backend(backend)
        for i in range(n_legacy):
            legacy_store.store(unprotected(name=f"legacy-{i}"))
        legacy_store.commit()
        legacy_store.close()

    rock_class = protected_rock()
    store = dc.Store._from_backend(backend)
    oracle: dict[str, Labels] = {}
    oids: dict[str, int] = {}
    for i in range(n_legacy):
        name = f"legacy-{i}"
        oids[name] = store.store(store.get(rock_class, name=name))
        oracle[name] = LEGACY_LABELS

    with store.acting_as(ROOT):
        for i, row in enumerate(labeled):
            name = f"post-{i}"
            rec = rock_class(name=name)
            oids[name] = store.store(rec)
            seed_labels(rec, row["owner"], row["groups"], row["read_floor"])
            oracle[name] = (row["owner"], list(row["groups"]), row["read_floor"])
        store.commit()

    try:
        yield Corpus(store, rock_class, oracle, oids)
    finally:
        store.close()


def readable_names_by_oracle(corpus: Corpus, principal: dc.Principal) -> set[str]:
    """Return the names of the corpus rows the oracle lets the principal read."""
    return {name for name, row in corpus.oracle.items() if oracle_can_read(principal, *row)}


def names_of(rows: list[Any]) -> set[str]:
    """Return the names of the rock rows a query returned."""
    return {row.name for row in rows}


def names_dereferenced(corpus: Corpus, slots: list[Any]) -> set[str]:
    """Return the names whose OID, dereferenced in the corpus's order, came back as the real row."""
    return {
        name for name, slot in zip(corpus.oids, slots, strict=True) if not isinstance(slot, dc.Redacted)
    }
