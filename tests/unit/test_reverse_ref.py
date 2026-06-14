"""Reverse-reference index + incoming() (#20 / ROADMAP item 8, sub-story A).

incoming(x) = every committed entity that references x — direct (eager) refs and
Lazy refs, in scalar fields and inside list/dict containers. Built lazily from a
store scan, then maintained incrementally at commit. Validated against a
brute-force oracle. (Delete-fold and Snapshot.incoming() parity are the next
sub-stories.) Parametrized over both backends.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import field
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of
from tests.conftest import Locality, Mineral


@dc.entity
class Collector:
    name: Annotated[str, dc.Unique]
    favorites: list[dc.Lazy[Mineral]] = field(default_factory=list)  # lazy adjacency
    home: Locality | None = None                                    # eager scalar ref


def test_incoming_finds_lazy_scalar_referrers(store):
    tsumeb = Locality(qid="LT", name="Tsumeb")
    azurite = Mineral(qid="QA", name="azurite", type_locality=dc.Lazy.of(tsumeb))
    malachite = Mineral(qid="QM", name="malachite", type_locality=dc.Lazy.of(tsumeb))
    quartz = Mineral(qid="QQ", name="quartz")  # references nothing
    for e in (tsumeb, azurite, malachite, quartz):
        store.store(e)
    store.commit()
    assert sorted(m.qid for m in store.incoming(tsumeb)) == ["QA", "QM"]
    assert store.incoming(quartz) == []  # nothing references quartz


def test_incoming_through_eager_ref(store):
    loc = Locality(qid="LH", name="home")
    c = Collector(name="collector", home=loc)
    store.store(loc)
    store.store(c)
    store.commit()
    assert [x.name for x in store.incoming(loc)] == ["collector"]


def test_incoming_through_lazy_list(store):
    a = Mineral(qid="A", name="a")
    b = Mineral(qid="B", name="b")
    col = Collector(name="col", favorites=[dc.Lazy.of(a), dc.Lazy.of(b)])
    for e in (a, b, col):
        store.store(e)
    store.commit()
    assert [x.name for x in store.incoming(a)] == ["col"]
    assert [x.name for x in store.incoming(b)] == ["col"]


def test_incoming_matches_brute_force(store):
    locs = [Locality(qid=f"L{i}", name=f"loc{i}") for i in range(3)]
    mins = [Mineral(qid=f"M{i}", name=f"m{i}", type_locality=dc.Lazy.of(locs[i % 3]))
            for i in range(10)]
    for e in [*locs, *mins]:
        store.store(e)
    store.commit()
    for loc in locs:
        expected = sorted(m.qid for m in mins
                          if m.type_locality is not None and m.type_locality.peek() is loc)
        assert sorted(m.qid for m in store.incoming(loc)) == expected


def test_incoming_updates_incrementally(store):
    loc = Locality(qid="L", name="loc")
    m1 = Mineral(qid="M1", name="m1", type_locality=dc.Lazy.of(loc))
    store.store(loc)
    store.store(m1)
    store.commit()
    assert sorted(m.qid for m in store.incoming(loc)) == ["M1"]  # first call builds the index

    m2 = Mineral(qid="M2", name="m2", type_locality=dc.Lazy.of(loc))
    store.store(m2)
    store.commit()  # incremental fold adds the new referrer
    assert sorted(m.qid for m in store.incoming(loc)) == ["M1", "M2"]

    m1.type_locality = None
    store.commit()  # diff removes the re-pointed referrer
    assert sorted(m.qid for m in store.incoming(loc)) == ["M2"]


def test_incoming_built_from_scan_after_reopen(store_factory):
    s = store_factory()
    loc = Locality(qid="L", name="loc")
    m = Mineral(qid="M", name="m", type_locality=dc.Lazy.of(loc))
    s.store(loc)
    s.store(m)
    s.commit()
    s.close()
    # fresh store: the reverse index is built from a cold scan on first incoming()
    reopened = store_factory()
    loc2 = reopened.get(Locality, qid="L")
    assert [x.qid for x in reopened.incoming(loc2)] == ["M"]
    reopened.close()


def test_incoming_rejects_non_entity_and_unstored(store):
    with pytest.raises(dc.NotAnEntityError):
        store.incoming({"not": "an entity"})
    assert store.incoming(Locality(qid="ghost", name="never stored")) == []


# --- #20-B: delete-fold + rebuild equivalence -------------------------------

def test_incoming_drops_a_deleted_referrer(store):
    loc = Locality(qid="L", name="loc")
    m1 = Mineral(qid="M1", name="m1", type_locality=dc.Lazy.of(loc))
    m2 = Mineral(qid="M2", name="m2", type_locality=dc.Lazy.of(loc))
    for e in (loc, m1, m2):
        store.store(e)
    store.commit()
    assert sorted(m.qid for m in store.incoming(loc)) == ["M1", "M2"]  # builds the index
    store.delete(m1)
    store.commit()  # delete-fold removes m1 as a referrer
    assert sorted(m.qid for m in store.incoming(loc)) == ["M2"]


def test_incoming_on_deleted_target_names_dangling_referrers(store):
    # ADR-003: deleting a TARGET leaves its referrers dangling — incoming(dead)
    # enumerates exactly them (the checked-delete seam).
    loc = Locality(qid="L", name="loc")
    m = Mineral(qid="M", name="m", type_locality=dc.Lazy.of(loc))
    for e in (loc, m):
        store.store(e)
    store.commit()
    assert [x.qid for x in store.incoming(loc)] == ["M"]  # builds the index
    store.delete(loc)
    store.commit()
    # loc's record is gone, but m still points at its OID → incoming() still
    # names m as the now-dangling referrer (OIDs are never reused).
    assert [x.qid for x in store.incoming(loc)] == ["M"]


def test_reverse_index_rebuild_equals_incremental(store_factory):
    s = store_factory()
    locs = [Locality(qid=f"L{i}", name=f"l{i}") for i in range(3)]
    mins = [Mineral(qid=f"M{i}", name=f"m{i}", type_locality=dc.Lazy.of(locs[i % 3]))
            for i in range(9)]
    for e in [*locs, *mins]:
        s.store(e)
    s.commit()
    _ = s.incoming(locs[0])         # build the index → maintained incrementally below
    mins[0].type_locality = dc.Lazy.of(locs[1])  # re-point a referrer
    s.delete(mins[1])                            # delete a referrer
    s.commit()
    incremental = {loc.qid: sorted(m.qid for m in s.incoming(loc)) for loc in locs}
    s.close()

    s2 = store_factory()  # fresh IndexManager → reverse index rebuilt from a cold scan
    rebuilt = {q: sorted(m.qid for m in s2.incoming(s2.get(Locality, qid=q)))
               for q in incremental}
    s2.close()
    assert rebuilt == incremental  # invariant 11: incremental == rebuilt


# --- #20-C: Snapshot.incoming() parity (ADR-002 read views) -----------------

def test_snapshot_incoming_matches_store(store):
    loc = Locality(qid="L", name="loc")
    mins = [Mineral(qid=f"M{i}", name=f"m{i}", type_locality=dc.Lazy.of(loc))
            for i in range(5)]
    for e in (loc, *mins):
        store.store(e)
    store.commit()
    live = sorted(m.qid for m in store.incoming(loc))  # the live-store answer
    with store.snapshot() as snap:
        loc_view = next(v for v in snap.query(Locality) if v.qid == "L")
        # the snapshot accepts its own currency — EntityView, Ref, or raw OID
        by_view = sorted(v.qid for v in snap.incoming(loc_view))
        by_ref = sorted(v.qid for v in snap.incoming(dc.Ref(loc_view.oid)))
        by_oid = sorted(v.qid for v in snap.incoming(loc_view.oid))
    assert by_view == by_ref == by_oid == live == ["M0", "M1", "M2", "M3", "M4"]


def test_snapshot_incoming_excludes_referrers_committed_later(store):
    loc = Locality(qid="L", name="loc")
    m1 = Mineral(qid="M1", name="m1", type_locality=dc.Lazy.of(loc))
    for e in (loc, m1):
        store.store(e)
    store.commit()
    with store.snapshot() as snap:
        # a referrer committed AFTER the snapshot's watermark
        m2 = Mineral(qid="M2", name="m2", type_locality=dc.Lazy.of(loc))
        store.store(m2)
        store.commit()
        loc_view = next(v for v in snap.query(Locality) if v.qid == "L")
        # the pinned view (built lazily, even now) sees only M1
        assert [v.qid for v in snap.incoming(loc_view)] == ["M1"]
    assert sorted(m.qid for m in store.incoming(loc)) == ["M1", "M2"]  # live sees both


def test_snapshot_incoming_names_dangling_referrer_after_target_delete(store):
    # ADR-003 seam at a watermark: a deleted TARGET keeps no record, but its
    # referrers still point at the dead OID — incoming(dead) enumerates them.
    loc = Locality(qid="L", name="loc")
    m = Mineral(qid="M", name="m", type_locality=dc.Lazy.of(loc))
    for e in (loc, m):
        store.store(e)
    store.commit()
    loc_oid = oid_of(loc)
    assert loc_oid is not None
    store.delete(loc)
    store.commit()
    with store.snapshot() as snap:
        with pytest.raises(dc.DanglingRefError):
            snap.get(loc_oid)  # the target's record is gone at this watermark
        # incoming() still names the dangling referrer (no record needed)
        assert [v.qid for v in snap.incoming(loc_oid)] == ["M"]


def test_snapshot_incoming_callable_from_foreign_thread(store):
    loc = Locality(qid="L", name="loc")
    m = Mineral(qid="M", name="m", type_locality=dc.Lazy.of(loc))
    for e in (loc, m):
        store.store(e)
    store.commit()
    loc_oid = oid_of(loc)
    with store.snapshot() as snap:
        with ThreadPoolExecutor(max_workers=1) as pool:
            qids = pool.submit(
                lambda: [v.qid for v in snap.incoming(loc_oid)]
            ).result()
    assert qids == ["M"]
