"""The capstone read-fence world: one protected record, three principals, one seed.

Shared by ``test_read_fence_capstone.py`` and the web helper ``fence_web.py``, so the entities
register under one typename.
"""

from __future__ import annotations

from typing import Annotated, NamedTuple

import datacrystal as dc

TEAM = 5
OTHER = 6

# A owns the record; B has no standing on it; ROOT is the audited break-glass.
A = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})  # owner
B = dc.Principal(uid=4, memberships={OTHER: dc.CURATOR})  # denied reader
ROOT = dc.root_principal(99)
PRINCIPALS = {"A": A, "B": B, "ROOT": ROOT}

LABEL = "hidden"  # the one protected record under test
ANCHOR = "anchor"  # a WORLD-readable Specimen it links to (the incoming seam)
TERM = "crystal"  # a word only the hidden record's fulltext note carries
SECRET_BYTES = b"top-secret-bytes"


@dc.entity(protected=True)
class Specimen:
    """The single protected record every surface is asked about.

    One class carrying a fulltext field (the FTS tier), a blob field (the blob tier), and a
    self-referential Lazy edge (so it can be an ``incoming()`` referrer of the WORLD anchor, and
    a deref target).
    """

    label: Annotated[str, dc.Unique]
    note: Annotated[str | None, dc.FullText(language="en")] = None
    scan: Annotated[bytes | None, dc.Blob] = None
    linked: dc.Lazy[Specimen] | None = None


@dc.entity
class Cabinet:
    """An UNPROTECTED referrer holding a direct Lazy handle onto the record.

    The deref seam (``Lazy.get()`` / ``get_many([oid])``): a reader who holds a reference gets
    a masked twin, never the data.
    """

    tag: Annotated[str, dc.Unique]
    holds: dc.Lazy[Specimen] | None = None


SPECIMEN_TYPENAME = "tests.extras.fence_world:Specimen"
CAPSTONE_FULLTEXT = {SPECIMEN_TYPENAME: {"note": "en"}}


class Seeded(NamedTuple):
    """The seeded world: hidden record, WORLD-readable anchor, holding cabinet and the two oids."""

    hidden: Specimen
    anchor: Specimen
    cabinet: Cabinet
    hidden_oid: int
    anchor_oid: int


def seed(store: dc.Store) -> Seeded:
    """Commit the anchor, the hidden record that links to it, and a cabinet holding it.

    ``anchor`` is WORLD-readable, ``hidden`` is shared to nobody so only ``A`` and ``ROOT`` may
    read it, and the unprotected ``Cabinet`` holds a Lazy handle onto ``hidden``.
    """
    with store.acting_as(A):
        anchor = Specimen(label=ANCHOR)
        anchor_oid = store.store(anchor)
        dc.share(anchor, dc.WORLD, read=dc.VIEWER, write=dc.VIEWER)

        hidden = Specimen(
            label=LABEL,
            note="the crystal lattice diagram",
            scan=SECRET_BYTES,
            linked=dc.Lazy.of(anchor),
        )
        hidden_oid = store.store(hidden)  # owner-only — the record under test

        cabinet = Cabinet(tag="drawer-1", holds=dc.Lazy.of(hidden))
        store.store(cabinet)
        store.commit()
    return Seeded(hidden, anchor, cabinet, hidden_oid, anchor_oid)
