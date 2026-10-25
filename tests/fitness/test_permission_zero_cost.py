"""Fitness gate #21 — permission-zero-cost (ADR-008, W2-9).

"Unprotected classes bypass all of this at zero cost — the fence exists
only where the flag is set" (ADR-008): the unprotected commit path does NO
gate work. Mechanism = the counting-storage-wrapper idiom (#19 pattern):
the fence's only honest observable is prior-record I/O at the storage seam,
plus a raise-sentinel on the gate's single entry point.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, cast

import pytest

import datacrystal as dc
import datacrystal._snapshot as snapshot_module
import datacrystal._store as store_module
from datacrystal import Snapshot
from datacrystal._entity import oid_of as _oid_of
from datacrystal._storage.memory import MemoryBackend
from tests.fitness._counting_backend import CountingBackend

OWNER = dc.Principal(uid=5, memberships={dc.WORLD: dc.CURATOR})


@dc.entity
class PlainRow:
    """An unprotected class: the fence does no work for it."""

    tag: Annotated[str, dc.Unique]
    mass_g: float = 0.0


@dc.entity(protected=True)
class FencedRow:
    """A protected class: every write and read goes through the fence."""

    tag: Annotated[str, dc.Unique]
    mass_g: float = 0.0


@dataclass
class Cabinet:
    """A committed store of 4 plain and 3 fenced rows over a counting backend."""

    store: dc.Store
    counting: CountingBackend
    plain: list[PlainRow]
    fenced: list[FencedRow]


def _oids_of(rows: list[PlainRow]) -> list[int]:
    return [cast(int, _oid_of(row)) for row in rows]


@pytest.fixture
def cabinet() -> Iterator[Cabinet]:
    """Yield a committed cabinet whose load counters were reset after seeding."""
    counting = CountingBackend(MemoryBackend())
    store = dc.Store._from_backend(counting, principal=OWNER)
    plain = [PlainRow(tag=f"p{i}") for i in range(4)]
    fenced = [FencedRow(tag=f"f{i}") for i in range(3)]
    for row in (*plain, *fenced):
        store.store(row)
    store.commit()
    counting.reset()
    yield Cabinet(store, counting, plain, fenced)
    store.close()


@pytest.fixture
def snapshot(cabinet: Cabinet) -> Iterator[Snapshot]:
    """Yield an OWNER snapshot of the cabinet."""
    with cabinet.store.snapshot(principal=OWNER) as snap:
        yield snap


@pytest.fixture
def write_gate_calls(monkeypatch: pytest.MonkeyPatch, cabinet: Cabinet) -> list[str]:
    """Replace the write gate's single entry point by a sentinel that records and raises."""
    calls: list[str] = []

    def sentinel(self: object, *args: object, **kwargs: object) -> None:
        calls.append("gate")
        raise AssertionError("gate ran on a commit")

    monkeypatch.setattr(type(cabinet.store), "_check_write_gate", sentinel)
    return calls


@pytest.fixture
def store_read_fence_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the live store's readable_bitmap compiler raise when it is called."""

    def sentinel(*args: object, **kwargs: object) -> None:
        raise AssertionError("readable_bitmap ran on a store read")

    monkeypatch.setattr(store_module, "readable_bitmap", sentinel)


@pytest.fixture
def snapshot_read_fence_sentinels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the snapshot's readable_bitmap compiler and can_read_row predicate raise."""

    def bitmap_sentinel(*args: object, **kwargs: object) -> None:
        raise AssertionError("readable_bitmap ran on a snapshot read")

    def predicate_sentinel(*args: object, **kwargs: object) -> None:
        raise AssertionError("can_read_row ran on a snapshot deref")

    monkeypatch.setattr(snapshot_module, "readable_bitmap", bitmap_sentinel)
    monkeypatch.setattr(snapshot_module, "can_read_row", predicate_sentinel)


# Scenario A, B, C: the commit path's prior-record I/O at the storage seam.


def test_unprotected_commit_issues_no_load_call(cabinet: Cabinet) -> None:
    """Given four committed unprotected rows, all dirtied, and no consumers
    When the store commits
    Then the commit issues no load_many call
    """
    cabinet.plain[0].mass_g += 1.0
    cabinet.plain[1].mass_g += 1.0
    cabinet.plain[2].mass_g += 1.0
    cabinet.plain[3].mass_g += 1.0

    cabinet.store.commit()

    assert cabinet.counting.load_calls == 0


def test_unprotected_commit_loads_no_prior_record(cabinet: Cabinet) -> None:
    """Given four committed unprotected rows, all dirtied, and no consumers
    When the store commits
    Then no prior record was loaded
    """
    cabinet.plain[0].mass_g += 1.0
    cabinet.plain[1].mass_g += 1.0
    cabinet.plain[2].mass_g += 1.0
    cabinet.plain[3].mass_g += 1.0

    cabinet.store.commit()

    assert cabinet.counting.records_loaded == 0


def test_protected_commit_loads_each_dirty_prior_once(cabinet: Cabinet) -> None:
    """Given three committed protected rows, all dirtied (the positive control)
    When the store commits
    Then exactly the three prior records were loaded
    """
    cabinet.fenced[0].mass_g += 1.0
    cabinet.fenced[1].mass_g += 1.0
    cabinet.fenced[2].mass_g += 1.0

    cabinet.store.commit()

    assert cabinet.counting.records_loaded == 3


def test_mixed_commit_loads_exactly_the_protected_priors(cabinet: Cabinet) -> None:
    """Given four unprotected and three protected committed rows, all dirtied
    When the store commits
    Then only the three protected priors were loaded, never the whole batch
    """
    cabinet.plain[0].mass_g += 1.0
    cabinet.plain[1].mass_g += 1.0
    cabinet.plain[2].mass_g += 1.0
    cabinet.plain[3].mass_g += 1.0
    cabinet.fenced[0].mass_g += 1.0
    cabinet.fenced[1].mass_g += 1.0
    cabinet.fenced[2].mass_g += 1.0

    cabinet.store.commit()

    assert cabinet.counting.records_loaded == 3


# Scenario D: the write gate's raise-sentinel (Store._check_write_gate, the W2-9 seam).


def test_gate_entry_is_not_called_for_unprotected_pending(
    cabinet: Cabinet, write_gate_calls: list[str]
) -> None:
    """Given a raising sentinel on the write gate and four dirtied unprotected rows
    When the store commits
    Then the gate was never called
    """
    cabinet.plain[0].mass_g += 1.0
    cabinet.plain[1].mass_g += 1.0
    cabinet.plain[2].mass_g += 1.0
    cabinet.plain[3].mass_g += 1.0

    cabinet.store.commit()

    assert write_gate_calls == []


def test_gate_entry_is_called_for_protected_pending(
    cabinet: Cabinet, write_gate_calls: list[str]
) -> None:
    """Given a raising sentinel on the write gate and three dirtied protected rows (the control)
    When the store commits
    Then the commit reaches the gate and raises
    """
    cabinet.fenced[0].mass_g += 1.0
    cabinet.fenced[1].mass_g += 1.0
    cabinet.fenced[2].mass_g += 1.0

    with pytest.raises(AssertionError, match="gate ran"):
        cabinet.store.commit()

    assert write_gate_calls == ["gate"]


# Scenario E (ADR-008 W3-2): readable_bitmap, the ONE compiler every query/count/pluck/explain/
# query_iter branch calls, is structurally unreachable for an unprotected class: every call site
# guards on `ti.protected` first. A protected control class DOES call it, on every surface.


@pytest.mark.usefixtures("store_read_fence_sentinel")
def test_unprotected_query_never_compiles_the_read_fence(cabinet: Cabinet) -> None:
    """Given a raising sentinel on readable_bitmap and four unprotected rows
    When the store queries PlainRow
    Then all four rows are returned
    """
    hits = cabinet.store.query(PlainRow)

    assert len(hits) == 4


@pytest.mark.usefixtures("store_read_fence_sentinel")
def test_unprotected_count_never_compiles_the_read_fence(cabinet: Cabinet) -> None:
    """Given a raising sentinel on readable_bitmap and four unprotected rows
    When the store counts PlainRow
    Then the count is four
    """
    assert cabinet.store.count(PlainRow) == 4


@pytest.mark.usefixtures("store_read_fence_sentinel")
def test_unprotected_pluck_never_compiles_the_read_fence(cabinet: Cabinet) -> None:
    """Given a raising sentinel on readable_bitmap and four unprotected rows
    When the store plucks the tags of PlainRow
    Then four tags are returned
    """
    tags = cabinet.store.pluck(PlainRow, "tag")

    assert len(tags) == 4


@pytest.mark.usefixtures("store_read_fence_sentinel")
def test_unprotected_explain_never_compiles_the_read_fence(cabinet: Cabinet) -> None:
    """Given a raising sentinel on readable_bitmap and four unprotected rows
    When the store explains a query over PlainRow
    Then the plan's extent is four
    """
    plan = cabinet.store.explain(PlainRow)

    assert plan.extent == 4


@pytest.mark.usefixtures("store_read_fence_sentinel")
def test_unprotected_query_iter_never_compiles_the_read_fence(cabinet: Cabinet) -> None:
    """Given a raising sentinel on readable_bitmap and four unprotected rows
    When the store streams PlainRow through query_iter
    Then four rows are streamed
    """
    streamed = list(cabinet.store.query_iter(PlainRow))

    assert len(streamed) == 4


@pytest.mark.usefixtures("store_read_fence_sentinel")
def test_protected_query_compiles_the_read_fence(cabinet: Cabinet) -> None:
    """Given a raising sentinel on readable_bitmap and three protected rows (the control)
    When the store queries FencedRow
    Then the query reaches the compiler and raises
    """
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        cabinet.store.query(FencedRow)


@pytest.mark.usefixtures("store_read_fence_sentinel")
def test_protected_count_compiles_the_read_fence(cabinet: Cabinet) -> None:
    """Given a raising sentinel on readable_bitmap and three protected rows (the control)
    When the store counts FencedRow
    Then the count reaches the compiler and raises
    """
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        cabinet.store.count(FencedRow)


@pytest.mark.usefixtures("store_read_fence_sentinel")
def test_protected_pluck_compiles_the_read_fence(cabinet: Cabinet) -> None:
    """Given a raising sentinel on readable_bitmap and three protected rows (the control)
    When the store plucks the tags of FencedRow
    Then the pluck reaches the compiler and raises
    """
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        cabinet.store.pluck(FencedRow, "tag")


@pytest.mark.usefixtures("store_read_fence_sentinel")
def test_protected_explain_compiles_the_read_fence(cabinet: Cabinet) -> None:
    """Given a raising sentinel on readable_bitmap and three protected rows (the control)
    When the store explains a query over FencedRow
    Then the explain reaches the compiler and raises
    """
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        cabinet.store.explain(FencedRow)


@pytest.mark.usefixtures("store_read_fence_sentinel")
def test_protected_query_iter_compiles_the_read_fence(cabinet: Cabinet) -> None:
    """Given a raising sentinel on readable_bitmap and three protected rows (the control)
    When the store streams FencedRow through query_iter
    Then the stream reaches the compiler and raises
    """
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        list(cabinet.store.query_iter(FencedRow))


# Scenario E, snapshot tier (ADR-008 W4/R15): the same sentinels over datacrystal._snapshot's
# readable_bitmap AND can_read_row (deref surfaces check the latter). Every snapshot surface
# guards on ti.protected first; a protected control class DOES trip them.


@pytest.mark.usefixtures("snapshot_read_fence_sentinels")
def test_unprotected_snapshot_query_never_fires_the_read_fence(snapshot: Snapshot) -> None:
    """Given raising sentinels on the snapshot's fence and four unprotected rows
    When the snapshot queries PlainRow
    Then all four rows are returned
    """
    hits = snapshot.query(PlainRow)

    assert len(hits) == 4


@pytest.mark.usefixtures("snapshot_read_fence_sentinels")
def test_unprotected_snapshot_count_never_fires_the_read_fence(snapshot: Snapshot) -> None:
    """Given raising sentinels on the snapshot's fence and four unprotected rows
    When the snapshot counts PlainRow
    Then the count is four
    """
    assert snapshot.count(PlainRow) == 4


@pytest.mark.usefixtures("snapshot_read_fence_sentinels")
def test_unprotected_snapshot_all_never_fires_the_read_fence(snapshot: Snapshot) -> None:
    """Given raising sentinels on the snapshot's fence and four unprotected rows
    When the snapshot reads all of PlainRow
    Then four rows are returned
    """
    rows = snapshot.all(PlainRow)

    assert len(rows) == 4


@pytest.mark.usefixtures("snapshot_read_fence_sentinels")
def test_unprotected_snapshot_explain_never_fires_the_read_fence(snapshot: Snapshot) -> None:
    """Given raising sentinels on the snapshot's fence and four unprotected rows
    When the snapshot explains a query over PlainRow
    Then the plan's extent is four
    """
    plan = snapshot.explain(PlainRow)

    assert plan.extent == 4


@pytest.mark.usefixtures("snapshot_read_fence_sentinels")
def test_unprotected_snapshot_get_never_fires_can_read_row(
    snapshot: Snapshot, cabinet: Cabinet
) -> None:
    """Given raising sentinels on the snapshot's fence and the unprotected row p0
    When the snapshot gets it by oid
    Then the row with tag p0 is returned
    """
    oid = cast(int, _oid_of(cabinet.plain[0]))

    row = snapshot.get(oid)

    assert row.tag == "p0"


@pytest.mark.usefixtures("snapshot_read_fence_sentinels")
def test_unprotected_snapshot_get_many_never_fires_can_read_row(
    snapshot: Snapshot, cabinet: Cabinet
) -> None:
    """Given raising sentinels on the snapshot's fence and four unprotected rows
    When the snapshot gets them by oid in one batch
    Then the first one is found
    """
    oids = _oids_of(cabinet.plain)

    rows = snapshot.get_many(oids)

    assert rows[0] is not None


@pytest.mark.usefixtures("snapshot_read_fence_sentinels")
def test_protected_snapshot_query_fires_the_read_fence(snapshot: Snapshot) -> None:
    """Given raising sentinels on the snapshot's fence and three protected rows (the control)
    When the snapshot queries FencedRow
    Then the query reaches readable_bitmap and raises
    """
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        snapshot.query(FencedRow)


@pytest.mark.usefixtures("snapshot_read_fence_sentinels")
def test_protected_snapshot_count_fires_the_read_fence(snapshot: Snapshot) -> None:
    """Given raising sentinels on the snapshot's fence and three protected rows (the control)
    When the snapshot counts FencedRow
    Then the count reaches readable_bitmap and raises
    """
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        snapshot.count(FencedRow)


@pytest.mark.usefixtures("snapshot_read_fence_sentinels")
def test_protected_snapshot_all_fires_the_read_fence(snapshot: Snapshot) -> None:
    """Given raising sentinels on the snapshot's fence and three protected rows (the control)
    When the snapshot reads all of FencedRow
    Then the read reaches readable_bitmap and raises
    """
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        snapshot.all(FencedRow)


@pytest.mark.usefixtures("snapshot_read_fence_sentinels")
def test_protected_snapshot_explain_fires_the_read_fence(snapshot: Snapshot) -> None:
    """Given raising sentinels on the snapshot's fence and three protected rows (the control)
    When the snapshot explains a query over FencedRow
    Then the explain reaches readable_bitmap and raises
    """
    with pytest.raises(AssertionError, match="readable_bitmap ran"):
        snapshot.explain(FencedRow)


@pytest.mark.usefixtures("snapshot_read_fence_sentinels")
def test_protected_snapshot_get_fires_can_read_row(snapshot: Snapshot, cabinet: Cabinet) -> None:
    """Given raising sentinels on the snapshot's fence and the protected row f0
    When the snapshot gets it by oid
    Then the deref reaches can_read_row and raises
    """
    oid = cast(int, _oid_of(cabinet.fenced[0]))

    with pytest.raises(AssertionError, match="can_read_row ran"):
        snapshot.get(oid)


# R15 economics: a sibling handle builds nothing and shares every index / cache / reverse posting
# with the origin, so the pool stays O(n)/commit, not O(n)/principal.


def test_for_principal_shares_the_core_of_the_origin(snapshot: Snapshot) -> None:
    """Given an OWNER snapshot
    When a sibling handle is made for another principal
    Then both handles share one core
    """
    sibling = snapshot.for_principal(dc.Principal(uid=7, memberships={dc.WORLD: dc.STAFF}))

    assert snapshot._core is sibling._core


def test_for_principal_returns_a_distinct_handle(snapshot: Snapshot) -> None:
    """Given an OWNER snapshot
    When a sibling handle is made for another principal
    Then it is a different handle than the origin
    """
    sibling = snapshot.for_principal(dc.Principal(uid=7, memberships={dc.WORLD: dc.STAFF}))

    assert sibling is not snapshot


def test_for_principal_handle_carries_the_new_principal(snapshot: Snapshot) -> None:
    """Given an OWNER snapshot
    When a sibling handle is made for the principal with uid 7
    Then the sibling's principal has uid 7
    """
    sibling = snapshot.for_principal(dc.Principal(uid=7, memberships={dc.WORLD: dc.STAFF}))

    assert sibling.principal.uid == 7
