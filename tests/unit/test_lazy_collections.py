"""Element-level lazy references inside collections (#30).

A scalar `dc.Lazy[T]` field reloads lazy; the annotation `list[dc.Lazy[T]]`
(and `dict[K, dc.Lazy[T]]`) **also** round-trips element-level `Lazy` handles
today. This pins that and documents it as the supported spelling for on-demand
adjacency lists — the cut point that keeps a graph node's edges off the RAM
budget (and, with #29, off the read path). Putting `dc.Lazy.of(...)` into a
*plain* `list[T]` does NOT persist laziness: laziness follows the declared
element type, by design.

Parametrized over both backends.
"""

from __future__ import annotations

from dataclasses import field
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._containers import PersistentDict, PersistentList
from tests.conftest import Locality


@dc.entity
class Trail:
    name: Annotated[str, dc.Unique]
    stops: list[dc.Lazy[Locality]] = field(default_factory=list)          # lazy adjacency
    by_role: dict[str, dc.Lazy[Locality]] = field(default_factory=dict)   # lazy dict values
    eager_stops: list[Locality] = field(default_factory=list)            # the eager counter-case


def test_list_and_dict_of_lazy_reload_element_level_lazy(store_factory):
    a = Locality(qid="LA", name="Tsumeb")
    b = Locality(qid="LB", name="Broken Hill")
    s = store_factory()
    # a appears twice in stops + once in by_role → a clean identity check later
    s.root = Trail(name="T1",
                   stops=[dc.Lazy.of(a), dc.Lazy.of(b), dc.Lazy.of(a)],
                   by_role={"start": dc.Lazy.of(a)})
    s.commit()
    s.close()

    reopened = store_factory()
    trail = reopened.root
    # every list element reloads as an UNLOADED Lazy carrying its OID
    assert all(isinstance(x, dc.Lazy) and not x.loaded and x.oid is not None
               for x in trail.stops)
    start = trail.by_role["start"]
    assert isinstance(start, dc.Lazy) and not start.loaded
    # the containers are still owner-bound persistent containers
    assert isinstance(trail.stops, PersistentList)
    assert isinstance(trail.by_role, PersistentDict)
    # .get() hydrates the right entity...
    first = trail.stops[0].get()
    assert first.qid == "LA"
    # ...does NOT force a sibling...
    assert not trail.stops[1].loaded
    # ...and preserves identity: the same OID yields the same live instance
    assert first is trail.stops[2].get()          # both → a
    assert first is trail.by_role["start"].get()  # dict value → a
    reopened.close()


def test_lazy_of_in_a_plain_list_collapses_to_eager(store_factory):
    # Laziness is annotation-driven: a plain list[Locality] reloads EAGER even
    # if you stuffed Lazy.of(...) in at write time (documented, by design).
    loc = Locality(qid="LC", name="Erongo")
    s = store_factory()
    # deliberately the wrong thing: a Lazy in a plain list[Locality] field
    s.root = Trail(name="T2", eager_stops=[dc.Lazy.of(loc)])  # pyright: ignore[reportArgumentType]
    s.commit()
    s.close()

    reopened = store_factory()
    el = reopened.root.eager_stops[0]
    assert not isinstance(el, dc.Lazy)  # eager — laziness lost
    assert el.qid == "LC"
    reopened.close()


def test_lazy_ref_list_cannot_be_indexed():
    # list[dc.Lazy[T]] is a list of refs, not scalars — dc.Index rejects it
    # (#13 _is_list_of_scalar), so the indexable path and the lazy-adjacency
    # path never collide.
    class Bad:
        edges: Annotated[list[dc.Lazy[Locality]], dc.Index] = field(default_factory=list)

    with pytest.raises(TypeError, match="must be scalar"):
        dc.entity(Bad)
