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

from dataclasses import field

from typing import Annotated

import datacrystal as dc


@dc.entity
class Region:
    """A node in a geographic containment tree (continent -> country -> region
    -> mine), the recursive mineral-cabinet shape. Its own type is the element
    type of a lazy adjacency list (`children`) and of a lazy `parent` backlink —
    spelled as a forward-ref STRING under `from __future__ import annotations`,
    the buildable self-reference (typing.Self is not a tested path here)."""

    qid: Annotated[str, dc.Unique]
    name: str
    # Self-referential lazy adjacency: each child is an unloaded Lazy[Region]
    # after reopen — edges off the RAM/read budget (#30 + #29).
    children: list[dc.Lazy["Region"]] = field(default_factory=list)
    # Lazy parent backlink — closes the parent<->child cycle lazily.
    parent: dc.Lazy["Region"] | None = None


def test_selfref_lazy_adjacency_roundtrips_unloaded(store_factory):
    # Build a small containment tree: continent -> country -> two regions.
    earth = Region(qid="R-AF", name="Africa")
    namibia = Region(qid="R-NA", name="Namibia")
    erongo = Region(qid="R-ER", name="Erongo")
    otjozondjupa = Region(qid="R-OT", name="Otjozondjupa")

    namibia.parent = dc.Lazy.of(earth)
    earth.children = [dc.Lazy.of(namibia)]

    erongo.parent = dc.Lazy.of(namibia)
    otjozondjupa.parent = dc.Lazy.of(namibia)
    namibia.children = [dc.Lazy.of(erongo), dc.Lazy.of(otjozondjupa)]

    s = store_factory()
    s.root = earth
    s.commit()
    s.close()

    # --- reopen: a cold store, nothing hydrated yet ----------------------
    reopened = store_factory()
    root = reopened.root
    assert root.qid == "R-AF"

    # Each child edge rehydrates as an UNLOADED dc.Lazy carrying its OID.
    assert all(
        isinstance(c, dc.Lazy) and not c.loaded and c.oid is not None
        for c in root.children
    )

    # .get() hydrates the right node and does NOT force a sibling.
    na = root.children[0].get()
    assert na.qid == "R-NA"
    assert all(
        isinstance(c, dc.Lazy) and not c.loaded and c.oid is not None
        for c in na.children
    )

    # Identity is stable: the parent backlink resolves to the SAME live
    # instance as the root (one live entity per OID — the WeakValueDictionary
    # registry contract, invariant 6).
    assert na.parent.get() is root

    # The parent<->child cycle round-trips with no RecursionError: descending
    # to a leaf and climbing back returns the very objects we started from.
    er = na.children[0].get()
    assert er.qid == "R-ER"
    assert er.parent.get() is na              # child -> parent is identity-stable
    assert er.parent.get().parent.get() is root  # ...and all the way up
    # The other branch is still untouched (lazy, sibling not forced).
    assert not na.children[1].loaded

    reopened.close()
