"""The watermark pipeline: delta emission + consumer attachment (M3).

Engine side of COMMIT-DELTA-v1 (ROADMAP item 3): commits build a delta only
while consumers are attached, deliver it on the owner thread strictly after
durability, carry prior payloads for updates (the index-shaped consumers'
un-indexing fuel), and never let a broken consumer hold the store hostage.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import CommitBatch
from datacrystal.contract import DeltaGapError, ReferenceApplier
from datacrystal.testing import CountingConsumer
from tests.conftest import Locality, Mineral

StoreFactory = Callable[[], dc.Store]
Delta = dict[str, Any]


class _Collector:
    """Spec-obedient consumer that keeps every delta for inspection."""

    def __init__(self, watermark: int = 0) -> None:
        self.deltas: list[Delta] = []
        self.out_of_order: list[int] = []
        self._watermark = watermark

    @property
    def watermark(self) -> int:
        return self._watermark

    def apply(self, delta: Delta) -> bool:
        if delta["tid"] <= self._watermark:
            return False
        if delta["tid"] != self._watermark + 1:
            self.out_of_order.append(delta["tid"])
        self.deltas.append(delta)
        self._watermark = delta["tid"]
        return True


class _Exploding(_Collector):
    """A consumer whose sidecar disk is full."""

    def apply(self, delta: Delta) -> bool:
        raise RuntimeError("sidecar disk full")


class _Lazybones(_Collector):
    """A consumer that never advances its watermark."""

    def apply(self, delta: Delta) -> bool:
        return False


class _FailOnce(MemoryBackend):
    """A memory backend whose first apply() fails."""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    def apply(self, batch: CommitBatch) -> None:
        if self.failures:
            self.failures -= 1
            raise OSError("injected P2 fault")
        super().apply(batch)


def _present[T](value: T | None) -> T:
    """Narrow an Optional for the type checker; a fixture cannot assert."""
    if value is None:
        raise LookupError("expected a value, got None")
    return value


def _type_names(applier: ReferenceApplier) -> set[str]:
    return {name for name, _ in applier.types.values()}


@pytest.fixture
def collector_attached(store_factory: StoreFactory) -> tuple[dc.Store, _Collector]:
    """Attach a collector to a store that has committed nothing."""
    consumer = _Collector()
    store = store_factory()
    store.attach(consumer)
    return store, consumer


@pytest.fixture
def watched_two_commits(
    collector_attached: tuple[dc.Store, _Collector],
) -> tuple[dc.Store, _Collector, Mineral]:
    """Commit a one-mineral root, then rename the mineral and commit again, with a collector."""
    store, consumer = collector_attached
    store.root = [Mineral(qid="Q1", name="quartz", crystal_system="trigonal")]
    store.commit()
    quartz = _present(store.get(Mineral, qid="Q1"))
    quartz.name = "rock crystal"
    store.commit()
    return store, consumer, quartz


def test_consumer_watermark_follows_every_commit(
    collector_attached: tuple[dc.Store, _Collector],
) -> None:
    """Given a consumer attached before two commits
    When both commits are done
    Then the consumer's watermark is 2
    And the store's last_tid is 2
    """
    store, consumer = collector_attached
    store.root = [Mineral(qid="Q1", name="quartz", crystal_system="trigonal")]
    store.commit()
    quartz = _present(store.get(Mineral, qid="Q1"))
    quartz.name = "rock crystal"
    store.commit()

    assert consumer.watermark == 2
    assert store.last_tid == 2
    store.close()


def test_consumer_receives_one_delta_per_commit_in_order(
    collector_attached: tuple[dc.Store, _Collector],
) -> None:
    """Given a consumer attached before two commits
    When both commits are done
    Then the consumer received the TIDs 1 and 2
    And none arrived out of order
    """
    store, consumer = collector_attached
    store.root = [Mineral(qid="Q1", name="quartz", crystal_system="trigonal")]
    store.commit()
    quartz = _present(store.get(Mineral, qid="Q1"))
    quartz.name = "rock crystal"
    store.commit()

    assert [delta["tid"] for delta in consumer.deltas] == [1, 2]
    assert consumer.out_of_order == []
    store.close()


def test_delta_is_a_version_two_datacrystal_delta(
    watched_two_commits: tuple[dc.Store, _Collector, Mineral],
) -> None:
    """Given a consumer attached before the first commit
    When the first delta is read
    Then its format is "datacrystal-delta"
    And its version is 2
    """
    store, consumer, _ = watched_two_commits
    create = consumer.deltas[0]

    assert create["f"] == "datacrystal-delta"
    assert create["v"] == 2
    store.close()


def test_delta_stamps_the_anonymous_actor_and_an_integer_time(
    watched_two_commits: tuple[dc.Store, _Collector, Mineral],
) -> None:
    """COMMIT-DELTA-v2 section 2: always stamp both. `at` is int nanoseconds.

    Given a store opened without a principal and a consumer attached before the first commit
    When the first delta is read
    Then its actor is 0, anonymous
    And its `at` is an int
    """
    store, consumer, _ = watched_two_commits
    create = consumer.deltas[0]

    assert create["actor"] == 0
    assert isinstance(create["at"], int)
    store.close()


def test_first_delta_upserts_everything_without_priors(
    watched_two_commits: tuple[dc.Store, _Collector, Mineral],
) -> None:
    """Given a consumer attached before the commit that created everything
    When the first delta is read
    Then every op is an upsert
    And no op carries a prior payload
    And it names the root
    """
    store, consumer, _ = watched_two_commits
    create = consumer.deltas[0]

    assert {op["op"] for op in create["ops"]} == {"upsert"}
    assert all(op["prior"] is None for op in create["ops"])
    assert create["root"] is not None
    store.close()


def test_update_delta_carries_one_op_for_the_renamed_mineral(
    watched_two_commits: tuple[dc.Store, _Collector, Mineral],
) -> None:
    """Given a consumer attached before a commit that renamed quartz only
    When the second delta is read
    Then it holds exactly one op
    And that op is for quartz's oid
    """
    store, consumer, quartz = watched_two_commits
    update = consumer.deltas[1]

    assert len(update["ops"]) == 1
    assert update["ops"][0]["oid"] == store.store(quartz)
    store.close()


def test_update_op_prior_is_exactly_the_first_commits_payload(
    watched_two_commits: tuple[dc.Store, _Collector, Mineral],
) -> None:
    """The prior payloads are the index-shaped consumers' un-indexing fuel.

    Given a consumer attached before a commit that renamed quartz
    When the update op's prior is compared with quartz's payload in the first delta
    Then they are equal
    """
    store, consumer, quartz = watched_two_commits
    (update_op,) = consumer.deltas[1]["ops"]
    created_ops = consumer.deltas[0]["ops"]

    quartz_oid = store.store(quartz)

    assert update_op["prior"] == next(
        op["payload"] for op in created_ops if op["oid"] == quartz_oid
    )
    store.close()


def test_update_op_prior_differs_from_its_new_payload(
    watched_two_commits: tuple[dc.Store, _Collector, Mineral],
) -> None:
    """Given a consumer attached before a commit that renamed quartz
    When the update op is read
    Then its prior differs from its payload
    """
    store, consumer, _ = watched_two_commits
    (update_op,) = consumer.deltas[1]["ops"]

    assert update_op["prior"] != update_op["payload"]
    store.close()


@pytest.fixture
def applier_attached(store_factory: StoreFactory) -> tuple[dc.Store, ReferenceApplier]:
    """Attach a ReferenceApplier to a store that has committed nothing."""
    applier = ReferenceApplier()
    store = store_factory()
    store.attach(applier)
    return store, applier


@pytest.fixture
def mirrored_by_applier(
    applier_attached: tuple[dc.Store, ReferenceApplier],
) -> tuple[dc.Store, ReferenceApplier]:
    """Commit a cabinet, then update azurite and commit again, with a ReferenceApplier attached.

    The applier verifies priors strictly, so a bug would raise on the second commit.
    """
    store, applier = applier_attached
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine", country="NA")
    store.root = [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"),
        Mineral(
            qid="Q193563",
            name="azurite",
            crystal_system="monoclinic",
            type_locality=dc.Lazy.of(tsumeb),
        ),
    ]
    store.commit()
    azurite = _present(store.get(Mineral, qid="Q193563"))
    azurite.mohs = 3.8
    store.commit()
    return store, applier


def test_reference_applier_reaches_the_stores_watermark(
    applier_attached: tuple[dc.Store, ReferenceApplier],
) -> None:
    """Given a ReferenceApplier attached before two commits
    When both commits are done
    Then the applier's watermark equals the store's last_tid
    """
    store, applier = applier_attached
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine", country="NA")
    store.root = [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"),
        Mineral(
            qid="Q193563",
            name="azurite",
            crystal_system="monoclinic",
            type_locality=dc.Lazy.of(tsumeb),
        ),
    ]
    store.commit()
    azurite = _present(store.get(Mineral, qid="Q193563"))
    azurite.mohs = 3.8
    store.commit()

    assert applier.watermark == store.last_tid
    store.close()


def test_reference_applier_state_equals_the_durable_records(
    mirrored_by_applier: tuple[dc.Store, ReferenceApplier],
) -> None:
    """Given a ReferenceApplier attached before two commits
    When its payload per OID is compared with the store's durable records
    Then every payload is equal
    """
    store, applier = mirrored_by_applier

    assert all(
        store._backend.load_many([oid])[oid].payload == payload
        for oid, payload in applier.objects.items()
    )
    store.close()


def test_reference_applier_tracks_the_root_oid(
    applier_attached: tuple[dc.Store, ReferenceApplier],
) -> None:
    """Given a ReferenceApplier attached before two commits
    When both commits are done
    Then the applier's root oid is the store's root oid
    """
    store, applier = applier_attached
    tsumeb = Locality(qid="Q571997", name="Tsumeb Mine", country="NA")
    store.root = [
        Mineral(qid="Q43010", name="quartz", crystal_system="trigonal"),
        Mineral(
            qid="Q193563",
            name="azurite",
            crystal_system="monoclinic",
            type_locality=dc.Lazy.of(tsumeb),
        ),
    ]
    store.commit()
    azurite = _present(store.get(Mineral, qid="Q193563"))
    azurite.mohs = 3.8
    store.commit()

    with store.snapshot() as snap:
        [holder] = snap.incoming(snap.root[0])

    assert applier.root_oid == holder.oid
    store.close()


@pytest.fixture
def backend_reads(store: dc.Store, monkeypatch: pytest.MonkeyPatch) -> list[list[int]]:
    """Commit a one-mineral root, then record every later backend load_many call."""
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()
    reads: list[list[int]] = []
    original = store._backend.load_many

    def counting_load_many(oids: list[int]) -> dict:
        reads.append(list(oids))
        return original(oids)

    monkeypatch.setattr(store._backend, "load_many", counting_load_many)
    return reads


def test_unwatched_commit_reads_no_prior_payloads(
    store: dc.Store, backend_reads: list[list[int]]
) -> None:
    """Build-only-when-watched (spec section 5): an unwatched commit must not pay the read-back.

    Given a committed quartz and no consumer attached
    When quartz is renamed and the store commits
    Then P1 read nothing back from the backend
    """
    quartz = store.get(Mineral, qid="Q1")
    assert quartz is not None
    quartz.name = "rock crystal"

    store.commit()

    assert backend_reads == []


def test_watched_commit_reads_exactly_the_changed_prior(
    store: dc.Store, backend_reads: list[list[int]]
) -> None:
    """Given a committed quartz and a consumer attached at the store's watermark
    When quartz is renamed and the store commits
    Then exactly one O(delta) read of quartz's oid happened
    """
    quartz = store.get(Mineral, qid="Q1")
    assert quartz is not None
    quartz_oid = store.store(quartz)
    store.attach(_Collector(watermark=store.last_tid))
    quartz.name = "Bergkristall"

    store.commit()

    assert backend_reads == [[quartz_oid]]


@pytest.fixture
def store_at_tid_one(store_factory: StoreFactory) -> dc.Store:
    """Commit a one-mineral root as TID 1."""
    store = store_factory()
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()
    return store


def test_attach_refuses_a_consumer_behind_the_store(store_at_tid_one: dc.Store) -> None:
    """Given a store at TID 1
    When a consumer with watermark 0 is attached
    Then DeltaGapError says to rebuild from store.snapshot
    """
    with pytest.raises(DeltaGapError, match=r"rebuild from store\.snapshot"):
        store_at_tid_one.attach(_Collector())

    store_at_tid_one.close()


def test_attach_refuses_a_consumer_ahead_of_the_store(store_at_tid_one: dc.Store) -> None:
    """Given a store at TID 1
    When a consumer with watermark 6 is attached
    Then DeltaGapError says it is ahead of the store
    """
    with pytest.raises(DeltaGapError, match="ahead of the store"):
        store_at_tid_one.attach(_Collector(watermark=store_at_tid_one.last_tid + 5))

    store_at_tid_one.close()


def test_attach_refuses_the_same_consumer_twice(store_at_tid_one: dc.Store) -> None:
    """Given a consumer attached at the store's watermark
    When the same consumer is attached again
    Then DataCrystalError says it is already attached
    """
    current = _Collector(watermark=store_at_tid_one.last_tid)
    store_at_tid_one.attach(current)

    with pytest.raises(dc.DataCrystalError, match="already attached"):
        store_at_tid_one.attach(current)

    store_at_tid_one.close()


@pytest.fixture
def detached_after_one_commit(store_factory: StoreFactory) -> tuple[dc.Store, _Collector]:
    """Attach a collector, commit once, then detach it."""
    consumer = _Collector()
    store = store_factory()
    store.attach(consumer)
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()
    store.detach(consumer)
    return store, consumer


def test_detached_consumer_receives_no_further_deltas(
    detached_after_one_commit: tuple[dc.Store, _Collector],
) -> None:
    """Given a consumer that saw one commit and was then detached
    When the store commits again
    Then the consumer holds exactly one delta
    """
    store, consumer = detached_after_one_commit
    store.root = [Mineral(qid="Q2", name="azurite")]

    store.commit()

    assert len(consumer.deltas) == 1
    store.close()


def test_detaching_a_detached_consumer_is_refused(
    detached_after_one_commit: tuple[dc.Store, _Collector],
) -> None:
    """Given a consumer that was detached
    When it is detached again
    Then DataCrystalError says it is not attached
    """
    store, consumer = detached_after_one_commit

    with pytest.raises(dc.DataCrystalError, match="not attached"):
        store.detach(consumer)

    store.close()


@pytest.fixture
def store_with_exploding_and_surviving(
    store_factory: StoreFactory,
) -> tuple[dc.Store, _Exploding, _Collector]:
    """Attach an exploding and a healthy consumer."""
    exploding = _Exploding()
    surviving = _Collector()
    store = store_factory()
    store.attach(exploding)
    store.attach(surviving)
    return store, exploding, surviving


def test_commit_stays_durable_when_a_consumer_explodes(
    store_with_exploding_and_surviving: tuple[dc.Store, _Exploding, _Collector],
) -> None:
    """Given an exploding and a healthy consumer attached
    When the store commits a one-mineral root
    Then the commit returns TID 1
    And the healthy consumer already received delta 1 in that commit
    """
    store, _, surviving = store_with_exploding_and_surviving
    store.root = [Mineral(qid="Q1", name="quartz")]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tid = store.commit()

    assert tid == 1
    assert surviving.watermark == 1
    store.close()


def test_exploding_consumer_is_detached_with_a_warning(
    store_with_exploding_and_surviving: tuple[dc.Store, _Exploding, _Collector],
) -> None:
    """Given an exploding and a healthy consumer attached
    When the store commits
    Then ConsumerDetachedWarning is raised
    """
    store, _, _ = store_with_exploding_and_surviving
    store.root = [Mineral(qid="Q1", name="quartz")]

    with pytest.warns(dc.ConsumerDetachedWarning):
        store.commit()

    store.close()


def test_healthy_consumer_keeps_receiving_after_another_explodes(
    store_with_exploding_and_surviving: tuple[dc.Store, _Exploding, _Collector],
) -> None:
    """Given an exploding and a healthy consumer attached, one commit done
    When the store commits a second time
    Then the healthy consumer's watermark is 2
    """
    store, _, surviving = store_with_exploding_and_surviving
    store.root = [Mineral(qid="Q1", name="quartz")]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        store.commit()
    store.root = [Mineral(qid="Q2", name="azurite")]

    store.commit()

    assert surviving.watermark == 2
    store.close()


def test_exploding_consumer_never_sees_a_delta_again(
    store_with_exploding_and_surviving: tuple[dc.Store, _Exploding, _Collector],
) -> None:
    """Given an exploding consumer detached by a failed first commit
    When the store commits a second time
    Then the exploding consumer's watermark is still 0
    """
    store, exploding, _ = store_with_exploding_and_surviving
    store.root = [Mineral(qid="Q1", name="quartz")]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        store.commit()
    store.root = [Mineral(qid="Q2", name="azurite")]

    store.commit()

    assert exploding.watermark == 0
    store.close()


def test_detached_exploding_consumer_cannot_reattach_until_rebuilt(
    store_with_exploding_and_surviving: tuple[dc.Store, _Exploding, _Collector],
) -> None:
    """Given an exploding consumer detached, now behind the store
    When it is attached again
    Then DeltaGapError is raised
    """
    store, exploding, _ = store_with_exploding_and_surviving
    store.root = [Mineral(qid="Q1", name="quartz")]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        store.commit()

    with pytest.raises(DeltaGapError):
        store.attach(exploding)

    store.close()


def test_consumer_that_silently_skips_is_detached_with_a_warning(
    store_factory: StoreFactory,
) -> None:
    """Given a consumer that never advances its watermark
    When the store commits
    Then a warning cites spec section 4.3
    """
    store = store_factory()
    store.attach(_Lazybones())
    store.root = [Mineral(qid="Q1", name="quartz")]

    with pytest.warns(Warning, match="§4.3"):
        store.commit()

    store.close()


def test_store_stays_healthy_after_the_skipping_consumer_is_detached(
    store_factory: StoreFactory,
) -> None:
    """Given a silently skipping consumer detached by the first commit
    When the store commits again with no consumer left
    Then the commit returns TID 2
    """
    store = store_factory()
    store.attach(_Lazybones())
    store.root = [Mineral(qid="Q1", name="quartz")]
    with pytest.warns(Warning, match="§4.3"):
        store.commit()
    store.root = [Mineral(qid="Q2", name="azurite")]

    tid = store.commit()

    assert tid == 2
    store.close()


def test_empty_commit_returns_no_tid(store_factory: StoreFactory) -> None:
    """Given a store with a consumer attached and nothing pending
    When the store commits
    Then nothing is committed
    """
    store = store_factory()
    store.attach(_Collector())

    tid = store.commit()

    assert tid is None
    store.close()


def test_empty_commit_emits_nothing(store_factory: StoreFactory) -> None:
    """Given a store with a consumer attached and nothing pending
    When the store commits
    Then the consumer received no delta
    """
    consumer = _Collector()
    store = store_factory()
    store.attach(consumer)

    store.commit()

    assert consumer.deltas == []
    store.close()


def test_failed_p2_delivers_no_delta() -> None:
    """Given a consumer attached to a store whose first apply fails
    When the store commits
    Then OSError is raised
    And the consumer received no delta
    """
    consumer = _Collector()
    store = dc.Store._from_backend(_FailOnce())
    store.attach(consumer)
    store.root = [Mineral(qid="Q1", name="quartz")]

    with pytest.raises(OSError, match="injected P2 fault"):
        store.commit()

    assert consumer.deltas == []
    store.close()


def test_retry_after_failed_p2_reuses_the_tid() -> None:
    """The retry reuses the TID: gapless, invariant 5.

    Given a consumer attached to a store whose first commit failed in P2
    When the store commits again
    Then the commit returns TID 1
    """
    store = dc.Store._from_backend(_FailOnce())
    store.attach(_Collector())
    store.root = [Mineral(qid="Q1", name="quartz")]
    with pytest.raises(OSError, match="injected P2 fault"):
        store.commit()

    tid = store.commit()

    assert tid == 1
    store.close()


def test_retry_after_failed_p2_emits_exactly_one_delta() -> None:
    """Given a consumer attached to a store whose first commit failed in P2
    When the store commits again
    Then the consumer received exactly the TID 1
    """
    consumer = _Collector()
    store = dc.Store._from_backend(_FailOnce())
    store.attach(consumer)
    store.root = [Mineral(qid="Q1", name="quartz")]
    with pytest.raises(OSError, match="injected P2 fault"):
        store.commit()

    store.commit()

    assert [delta["tid"] for delta in consumer.deltas] == [1]
    store.close()


@pytest.fixture
def midlife_counter(store_factory: StoreFactory) -> tuple[dc.Store, CountingConsumer]:
    """Bootstrap a consumer from one snapshot (journal scene_pipeline) and attach it.

    Lineage, initial state and watermark come from the snapshot, so the attach is gap-free.
    """
    store = store_factory()
    store.root = [Mineral(qid="Q1", name="quartz"), Mineral(qid="Q2", name="azurite")]
    store.commit()
    with store.snapshot() as snap:
        counter = CountingConsumer.bootstrap(snap)
    store.attach(counter)
    return store, counter


def test_midlife_consumer_reaches_the_stores_watermark(
    midlife_counter: tuple[dc.Store, CountingConsumer],
) -> None:
    """Given a consumer bootstrapped from a snapshot at TID 1 and attached gap-free
    When one more commit is done
    Then its watermark is the store's last_tid
    And it is 2
    """
    store, counter = midlife_counter
    store.store(Mineral(qid="Q3", name="topaz"))

    store.commit()

    assert counter.watermark == store.last_tid
    assert counter.watermark == 2
    store.close()


def test_midlife_consumer_counts_bootstrapped_and_live_minerals(
    midlife_counter: tuple[dc.Store, CountingConsumer],
) -> None:
    """Given a consumer bootstrapped from a snapshot of 2 minerals and attached gap-free
    When one more mineral is committed
    Then it counts 3 minerals
    """
    store, counter = midlife_counter
    store.store(Mineral(qid="Q3", name="topaz"))

    store.commit()

    assert counter.content()["tests.conftest:Mineral"] == 3
    store.close()


def test_types_rows_precede_their_ops(store_factory: StoreFactory) -> None:
    """The ReferenceApplier raises if an op references a cid before its types row arrived.

    Given a ReferenceApplier attached to a store
    When a mineral with a lazy locality is committed
    Then the applier knows the Mineral, Locality and root types
    """
    applier = ReferenceApplier()
    store = store_factory()
    store.attach(applier)
    store.root = [
        Mineral(qid="Q1", name="quartz", type_locality=dc.Lazy.of(Locality(qid="Q5", name="Alps")))
    ]

    store.commit()

    assert {
        "tests.conftest:Mineral",
        "tests.conftest:Locality",
        "datacrystal._store:_Root",
    } <= _type_names(applier)
    store.close()
