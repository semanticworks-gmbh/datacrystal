"""The M4 exit gate: query vs a brute-force oracle (KICKOFF M4).

Hypothesis builds a random cabinet history — upserts, field updates,
deletions, interleaved commits — then a random Condition tree over indexed
and non-indexed fields (every operator, including the M4 contains/
startswith), and demands that the planner's answer equal a brute-force
``cond.evaluate`` sweep over plain rows. One oracle checks four surfaces
at once: ``query()``, ``count()``, ``pluck()`` and the snapshot's bitmap
``query()``/``count()`` at the same watermark.

Backend: memory. The planner is backend-independent (indexes are built
from a type scan either way); memory-vs-sqlite equivalence is pinned by
the parametrized engine tests.
"""

from __future__ import annotations

import warnings
from types import SimpleNamespace
from typing import Annotated

from hypothesis import given, settings
from hypothesis import strategies as st

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend


@dc.entity
class OracleSpecimen:
    sid: Annotated[str, dc.Unique]
    quality: Annotated[str | None, dc.Index] = None
    system: Annotated[str | None, dc.Index] = None
    mass: float | None = None
    note: str = ""


S = dc.fields(OracleSpecimen)

# Small domains so collisions, misses and None all actually happen.
SIDS = [f"S{i}" for i in range(12)]
QUALITY = st.sampled_from([None, "A", "B", "C"])
SYSTEM = st.sampled_from([None, "cubic", "trigonal", "monoclinic"])
MASS = st.sampled_from([None, 0.0, 1.5, 7.0, 10.0])
NOTE = st.sampled_from(["", "twin", "twinned cluster", "phantom"])
DOMAIN = {"quality": QUALITY, "system": SYSTEM, "mass": MASS, "note": NOTE}

_ORDERING = {
    "==": lambda e, v: e == v,
    "!=": lambda e, v: e != v,
    "<": lambda e, v: e < v,
    "<=": lambda e, v: e <= v,
    ">": lambda e, v: e > v,
    ">=": lambda e, v: e >= v,
}


@st.composite
def _rows(draw, sid: str) -> dict:
    return {
        "sid": sid,
        "quality": draw(QUALITY),
        "system": draw(SYSTEM),
        "mass": draw(MASS),
        "note": draw(NOTE),
    }


@st.composite
def _scripts(draw) -> list[tuple]:
    ops: list[tuple] = []
    for _ in range(draw(st.integers(4, 25))):
        kind = draw(st.sampled_from(
            ["upsert", "upsert", "upsert", "update", "delete", "commit"]))
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
def _predicates(draw):
    field = draw(st.sampled_from(["sid", "quality", "system", "mass", "note"]))
    expr = getattr(S, field)
    if field == "mass":
        op = draw(st.sampled_from(["==", "!=", "<", "<=", ">", ">=", "in"]))
        if op == "in":
            return expr.in_(draw(st.lists(MASS, min_size=0, max_size=3)))
        return _ORDERING[op](expr, draw(st.sampled_from(
            [None, 0.0, 1.5, 3.3, 7.0, 10.0])))
    op = draw(st.sampled_from(["==", "!=", "in", "contains", "startswith"]))
    if op == "contains":
        return expr.contains(draw(st.sampled_from(
            ["", "A", "cu", "tri", "win", "S1", "x", "twin"])))
    if op == "startswith":
        return expr.startswith(draw(st.sampled_from(
            ["", "A", "cu", "tri", "twin", "S", "S1", "x"])))
    values = st.sampled_from(SIDS) if field == "sid" else DOMAIN[field]
    if op == "in":
        return expr.in_(draw(st.lists(values, min_size=0, max_size=3)))
    return _ORDERING[op](expr, draw(values))


_conditions = st.recursive(
    _predicates(),
    lambda kids: st.one_of(
        st.tuples(kids, kids).map(lambda t: t[0] & t[1]),
        st.tuples(kids, kids).map(lambda t: t[0] | t[1]),
        kids.map(lambda c: ~c),
    ),
    max_leaves=5,
)


@settings(deadline=None)
@given(ops=_scripts(), cond=_conditions)
def test_query_count_pluck_and_snapshot_match_the_oracle(ops, cond):
    store = dc.Store._from_backend(MemoryBackend())
    live: dict[str, object] = {}   # sid → canonical live entity
    rows: dict[str, dict] = {}     # sid → expected field values (the oracle)
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

    expected = {sid for sid, row in rows.items()
                if cond.evaluate(SimpleNamespace(**row))}

    with warnings.catch_warnings():
        # a script may delete every row — the loud-empty warning is correct
        # there and pinned by its own unit tests; the oracle checks results
        warnings.simplefilter("ignore", dc.UnseenTypeWarning)
        assert {e.sid for e in store.query(cond)} == expected
        assert store.count(cond) == len(expected)
        assert sorted(store.pluck(cond, "sid")) == sorted(expected)
        with store.snapshot() as snap:
            assert {v.sid for v in snap.query(cond)} == expected
            assert snap.count(cond) == len(expected)
    store.close()
