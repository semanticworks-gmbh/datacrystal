"""Strategies and replay for the M4 query oracle: a random cabinet history and a random condition.

Small domains, so collisions, misses and None all actually happen.
"""

from __future__ import annotations

import operator
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

from hypothesis import strategies as st

import datacrystal as dc
from datacrystal._conditions import Condition


@dc.entity
class OracleSpecimen:
    """A specimen with two indexed fields and two unindexed ones."""

    sid: Annotated[str, dc.Unique]
    quality: Annotated[str | None, dc.Index] = None
    system: Annotated[str | None, dc.Index] = None
    mass: float | None = None
    note: str = ""


S = dc.fields(OracleSpecimen)

SIDS = [f"S{i}" for i in range(12)]
QUALITY = st.sampled_from([None, "A", "B", "C"])
SYSTEM = st.sampled_from([None, "cubic", "trigonal", "monoclinic"])
MASS = st.sampled_from([None, 0.0, 1.5, 7.0, 10.0])
NOTE = st.sampled_from(["", "twin", "twinned cluster", "phantom"])
DOMAIN = {"quality": QUALITY, "system": SYSTEM, "mass": MASS, "note": NOTE}

_ORDERING: dict[str, Callable[[Any, Any], Any]] = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@st.composite
def _rows(draw: st.DrawFn, sid: str) -> dict[str, Any]:
    return {
        "sid": sid,
        "quality": draw(QUALITY),
        "system": draw(SYSTEM),
        "mass": draw(MASS),
        "note": draw(NOTE),
    }


@st.composite
def scripts(draw: st.DrawFn) -> list[tuple[Any, ...]]:
    """Draw a cabinet history: upserts, field updates, deletions and interleaved commits."""
    ops: list[tuple[Any, ...]] = []
    for _ in range(draw(st.integers(4, 25))):
        kind = draw(st.sampled_from(["upsert", "upsert", "upsert", "update", "delete", "commit"]))
        sid = draw(st.sampled_from(SIDS))
        if kind == "upsert":
            ops.append(("upsert", draw(_rows(sid))))
        elif kind == "update":
            field = draw(st.sampled_from(["quality", "system", "mass", "note"]))
            ops.append(("update", sid, field, draw(DOMAIN[field])))
        elif kind == "delete":
            ops.append(("delete", sid))
        else:
            ops.append(("commit",))
    return ops


@st.composite
def _predicates(draw: st.DrawFn) -> Condition:
    field = draw(st.sampled_from(["sid", "quality", "system", "mass", "note"]))
    expr = getattr(S, field)
    if field == "mass":
        op = draw(st.sampled_from(["==", "!=", "<", "<=", ">", ">=", "in"]))
        if op == "in":
            return expr.in_(draw(st.lists(MASS, min_size=0, max_size=3)))
        return _ORDERING[op](expr, draw(st.sampled_from([None, 0.0, 1.5, 3.3, 7.0, 10.0])))
    op = draw(st.sampled_from(["==", "!=", "in", "contains", "startswith"]))
    if op == "contains":
        return expr.contains(draw(st.sampled_from(["", "A", "cu", "tri", "win", "S1", "x", "twin"])))
    if op == "startswith":
        return expr.startswith(
            draw(st.sampled_from(["", "A", "cu", "tri", "twin", "S", "S1", "x"]))
        )
    values = st.sampled_from(SIDS) if field == "sid" else DOMAIN[field]
    if op == "in":
        return expr.in_(draw(st.lists(values, min_size=0, max_size=3)))
    return _ORDERING[op](expr, draw(values))


def _both(pair: tuple[Condition, Condition]) -> Condition:
    return pair[0] & pair[1]


def _either(pair: tuple[Condition, Condition]) -> Condition:
    return pair[0] | pair[1]


def _negated(condition: Condition) -> Condition:
    return ~condition


def _combined(kids: st.SearchStrategy[Condition]) -> st.SearchStrategy[Condition]:
    pairs = st.tuples(kids, kids)
    return st.one_of(pairs.map(_both), pairs.map(_either), kids.map(_negated))


conditions = st.recursive(_predicates(), _combined, max_leaves=5)


@dataclass
class CabinetHistory:
    """A committed store and the plain rows a brute-force sweep expects in it."""

    store: dc.Store
    rows: dict[str, dict[str, Any]]


@contextmanager
def replayed(ops: list[tuple[Any, ...]]) -> Generator[CabinetHistory]:
    """Replay a script on a scratch store, commit, yield it with its oracle rows."""
    with tempfile.TemporaryDirectory() as directory:
        yield from _replayed_in(Path(directory), ops)


def _replayed_in(directory: Path, ops: list[tuple[Any, ...]]) -> Generator[CabinetHistory]:
    """Replay the script on a store opened in ``directory``; close it after the yield."""
    store = dc.Store.open(directory, durability="never")
    live: dict[str, Any] = {}  # sid -> canonical live entity
    rows: dict[str, dict[str, Any]] = {}  # sid -> expected field values (the oracle)
    for op in ops:
        if op[0] == "upsert":
            row = op[1]
            live[row["sid"]] = store.upsert(OracleSpecimen(**row))
            rows[row["sid"]] = dict(row)
        elif op[0] == "update":
            _, sid, field, value = op
            if sid in live:
                setattr(live[sid], field, value)
                rows[sid][field] = value
        elif op[0] == "delete":
            if op[1] in live:
                store.delete(live.pop(op[1]))
                del rows[op[1]]
        else:
            store.commit()
    store.commit()  # normalize: committed state == oracle state
    try:
        yield CabinetHistory(store, rows)
    finally:
        store.close()


def matching_sids(rows: dict[str, dict[str, Any]], condition: Condition) -> set[str]:
    """Return the sids whose plain row satisfies the condition, by brute-force sweep."""
    return {sid for sid, row in rows.items() if condition.evaluate(SimpleNamespace(**row))}
