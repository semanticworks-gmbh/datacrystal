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

from collections.abc import Callable
from dataclasses import field
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._containers import PersistentDict, PersistentList
from tests.conftest import Locality

StoreFactory = Callable[[], dc.Store]


@dc.entity
class Trail:
    """A route over localities, with lazy and eager collections side by side."""

    name: Annotated[str, dc.Unique]
    stops: list[dc.Lazy[Locality]] = field(default_factory=list)  # lazy adjacency
    by_role: dict[str, dc.Lazy[Locality]] = field(default_factory=dict)  # lazy dict values
    eager_stops: list[Locality] = field(default_factory=list)  # the eager counter-case


def _commit_trail(store_factory: StoreFactory) -> None:
    """Tsumeb appears twice in `stops` and once in `by_role`, Broken Hill once in `stops`."""
    tsumeb = Locality(qid="LA", name="Tsumeb")
    broken_hill = Locality(qid="LB", name="Broken Hill")
    seeding = store_factory()
    seeding.root = Trail(
        name="T1",
        stops=[dc.Lazy.of(tsumeb), dc.Lazy.of(broken_hill), dc.Lazy.of(tsumeb)],
        by_role={"start": dc.Lazy.of(tsumeb)},
    )
    seeding.commit()
    seeding.close()


def test_list_elements_reload_as_unloaded_lazy_handles(store_factory: StoreFactory) -> None:
    """Given a committed trail with a list[dc.Lazy[Locality]] of three stops
    When the store is reopened and the root trail is read
    Then every stop is an unloaded Lazy carrying its oid
    """
    _commit_trail(store_factory)

    reopened = store_factory()
    stops = reopened.root.stops

    assert all(isinstance(x, dc.Lazy) and not x.loaded and x.oid is not None for x in stops)
    reopened.close()


def test_dict_values_reload_as_unloaded_lazy_handles(store_factory: StoreFactory) -> None:
    """Given a committed trail with a dict[str, dc.Lazy[Locality]] holding "start"
    When the store is reopened and the root trail is read
    Then the "start" value is an unloaded Lazy
    """
    _commit_trail(store_factory)

    reopened = store_factory()
    start = reopened.root.by_role["start"]

    assert isinstance(start, dc.Lazy)
    assert not start.loaded
    reopened.close()


def test_lazy_collections_stay_owner_bound_persistent_containers(
    store_factory: StoreFactory,
) -> None:
    """Given a committed trail with a lazy list and a lazy dict
    When the store is reopened and the root trail is read
    Then the list is a PersistentList
    And the dict is a PersistentDict
    """
    _commit_trail(store_factory)

    reopened = store_factory()
    trail = reopened.root

    assert isinstance(trail.stops, PersistentList)
    assert isinstance(trail.by_role, PersistentDict)
    reopened.close()


def test_getting_one_lazy_stop_hydrates_it_and_leaves_its_sibling_unloaded(
    store_factory: StoreFactory,
) -> None:
    """Given a reopened trail whose first stop is Tsumeb and second is Broken Hill
    When the first stop is fetched with get()
    Then it is the Tsumeb locality
    And the second stop is still not loaded
    """
    _commit_trail(store_factory)
    reopened = store_factory()
    trail = reopened.root

    first = trail.stops[0].get()

    assert first.qid == "LA"
    assert not trail.stops[1].loaded
    reopened.close()


def test_same_oid_in_list_and_dict_yields_one_live_instance(store_factory: StoreFactory) -> None:
    """Given a reopened trail where Tsumeb is stops[0], stops[2] and by_role["start"]
    When all three handles are fetched with get()
    Then they are the identical live instance
    """
    _commit_trail(store_factory)
    reopened = store_factory()
    trail = reopened.root

    first = trail.stops[0].get()
    third = trail.stops[2].get()
    start = trail.by_role["start"].get()

    assert first is third
    assert first is start
    reopened.close()


def test_lazy_of_in_a_plain_list_reloads_eager(store_factory: StoreFactory) -> None:
    """Given a plain list[Locality] field stuffed with Lazy.of(Erongo) at write time
    When the store is reopened and the element is read
    Then it is a Locality, not a Lazy
    And it is Erongo
    """
    erongo = Locality(qid="LC", name="Erongo")
    seeding = store_factory()
    # deliberately the wrong thing: laziness follows the declared element type, by design
    seeding.root = Trail(name="T2", eager_stops=[dc.Lazy.of(erongo)])  # pyrefly: ignore[bad-argument-type]
    seeding.commit()
    seeding.close()

    reopened = store_factory()
    element = reopened.root.eager_stops[0]

    assert not isinstance(element, dc.Lazy)
    assert element.qid == "LC"
    reopened.close()


def test_indexing_a_list_of_lazy_refs_is_rejected() -> None:
    """Given an entity class indexing list[dc.Lazy[Locality]] with dc.Index
    When dc.entity decorates it
    Then it raises TypeError saying the field must be scalar
    """

    class Bad:
        edges: Annotated[list[dc.Lazy[Locality]], dc.Index] = field(default_factory=list)

    with pytest.raises(TypeError, match="must be scalar"):
        dc.entity(Bad)
