"""Self-referential lazy adjacency — the flagship tree/graph shape (#107).

`list[dc.Lazy[T]]` element-level laziness (pinned in #30) is what you use for
on-demand adjacency lists; #29 keeps those edges off the read path. The eval
that filed #107 (a recursive outline tree) asked the unanswered question: does
the *self-referential* case — a node whose `children` are `dc.Lazy` handles to
its OWN type, plus a lazy `parent` backlink — round-trip the same way? It does,
and this pins it. The engine already resolves self/mutual `Lazy[T]` hints via
the NameError fallback at `_entity.py` L357-365 (under
`from __future__ import annotations` the entity's own name isn't bound yet, so
the field specs resolve lazily once it is) — no `src/` change is needed; this
is the missing test for the object-graph database's headline structure.

Defines its OWN recursive mineral-domain node (`Region`) — never mutates the
shared, non-recursive `Locality` in conftest. Parametrized over both backends.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import field
from typing import Annotated

import datacrystal as dc

StoreFactory = Callable[[], dc.Store]


@dc.entity
class Region:
    """A node in a geographic containment tree (continent -> country -> region
    -> mine), the recursive mineral-cabinet shape. Its own type is the element
    type of a lazy adjacency list (`children`) and of a lazy `parent` backlink —
    spelled as a forward-ref STRING under `from __future__ import annotations`,
    the buildable self-reference (typing.Self is not a tested path here).
    """

    qid: Annotated[str, dc.Unique]
    name: str
    # Self-referential lazy adjacency: each child is an unloaded Lazy[Region]
    # after reopen — edges off the RAM/read budget (#30 + #29).
    children: list[dc.Lazy[Region]] = field(default_factory=list)
    # Lazy parent backlink — closes the parent<->child cycle lazily.
    parent: dc.Lazy[Region] | None = None


def _commit_region_tree(store_factory: StoreFactory) -> None:
    """Africa -> Namibia -> (Erongo, Otjozondjupa), committed and closed so reads start cold."""
    africa = Region(qid="R-AF", name="Africa")
    namibia = Region(qid="R-NA", name="Namibia")
    erongo = Region(qid="R-ER", name="Erongo")
    otjozondjupa = Region(qid="R-OT", name="Otjozondjupa")

    namibia.parent = dc.Lazy.of(africa)
    africa.children = [dc.Lazy.of(namibia)]

    erongo.parent = dc.Lazy.of(namibia)
    otjozondjupa.parent = dc.Lazy.of(namibia)
    namibia.children = [dc.Lazy.of(erongo), dc.Lazy.of(otjozondjupa)]

    seeding = store_factory()
    seeding.root = africa
    seeding.commit()
    seeding.close()


def test_reopened_root_children_are_unloaded_lazy_handles(store_factory: StoreFactory) -> None:
    """Given a committed Africa -> Namibia tree
    When the store is reopened and the root is read
    Then every child of the root is an unloaded Lazy carrying its oid
    """
    _commit_region_tree(store_factory)

    reopened = store_factory()
    root = reopened.root

    assert root.qid == "R-AF"
    assert all(isinstance(c, dc.Lazy) and not c.loaded and c.oid is not None for c in root.children)
    reopened.close()


def test_getting_one_lazy_child_hydrates_it_and_keeps_its_children_unloaded(
    store_factory: StoreFactory,
) -> None:
    """Given a reopened store whose root Africa has Namibia as a lazy child
    When Namibia is fetched with get()
    Then it is the right node
    And its own children are still unloaded Lazy handles
    """
    _commit_region_tree(store_factory)
    reopened = store_factory()

    namibia = reopened.root.children[0].get()

    assert namibia.qid == "R-NA"
    assert all(
        isinstance(c, dc.Lazy) and not c.loaded and c.oid is not None for c in namibia.children
    )
    reopened.close()


def test_parent_backlink_resolves_to_the_live_root_instance(store_factory: StoreFactory) -> None:
    """Given a reopened store with Namibia hydrated from the root
    When Namibia's parent backlink is fetched with get()
    Then it is the very same live instance as the root
    """
    _commit_region_tree(store_factory)
    reopened = store_factory()
    root = reopened.root
    namibia = root.children[0].get()

    parent = namibia.parent.get()

    assert parent is root
    reopened.close()


def test_climbing_from_a_leaf_returns_the_objects_descended_through(
    store_factory: StoreFactory,
) -> None:
    """Given a reopened store with Erongo reached by descending Africa -> Namibia -> Erongo
    When Erongo's parent chain is followed up to the root
    Then each step is the identical live instance and no RecursionError occurs
    """
    _commit_region_tree(store_factory)
    reopened = store_factory()
    root = reopened.root
    namibia = root.children[0].get()
    erongo = namibia.children[0].get()

    erongo_parent = erongo.parent.get()
    erongo_grandparent = erongo_parent.parent.get()

    assert erongo.qid == "R-ER"
    assert erongo_parent is namibia
    assert erongo_grandparent is root
    reopened.close()


def test_getting_one_leaf_leaves_its_sibling_unloaded(store_factory: StoreFactory) -> None:
    """Given a reopened store with Namibia hydrated
    When its first child Erongo is fetched with get()
    Then the sibling Otjozondjupa is still not loaded
    """
    _commit_region_tree(store_factory)
    reopened = store_factory()
    namibia = reopened.root.children[0].get()

    namibia.children[0].get()

    assert not namibia.children[1].loaded
    reopened.close()
