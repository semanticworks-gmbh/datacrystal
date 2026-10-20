"""datacrystal.deltalog — the retained delta log as a certified COMMIT-DELTA-v1 consumer.

ROADMAP item 23, first post-tag PR.

Unlike [arrow]/[fts] this is a *core* module (stdlib + msgspec, no extra),
but it is the watermark pipeline's third real consumer and is tested with
the same rigor: conformance kit certification, end-to-end over both backends
via ``store_factory``, reopen-resume, and the headline guarantee — replaying
the log reproduces the exact committed state (time-travel-by-replay).
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterator
from dataclasses import field
from pathlib import Path
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal.contract.applier import DeltaFormatError, DeltaGapError, ReferenceApplier
from datacrystal.deltalog import DeltaLog, DeltaLogConfigError
from datacrystal.testing import check_delta_consumer

_ids = itertools.count()
PINNED_AT_NS = 1_700_000_000_000_000_000

OpenLog = Callable[..., DeltaLog]
StoreFactory = Callable[[], dc.Store]


@dc.entity
class Cabinet:
    """A drawer of the mineral cabinet."""

    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Specimen:
    """A mineral specimen with a lazy cabinet reference and tags."""

    label: str
    mohs: float | None = None
    cabinet: dc.Lazy[Cabinet] | None = None
    tags: list = field(default_factory=list)


def pinned_clock() -> int:
    """Return the fixed commit time every audit stamp must carry."""
    return PINNED_AT_NS


def replayed_state_of(log: DeltaLog) -> str:
    """Return the digest of the state a log's replay reconstructs."""
    return log.replayed_state()


def tids_of(deltas: list[dict]) -> list[int]:
    """Return the transaction ids of the deltas, in the order given."""
    return [delta["tid"] for delta in deltas]


def oid_created_with(deltas: list[dict], text: bytes) -> int:
    """Return the oid of the first record created in the first delta whose payload has the text."""
    return next(
        op["oid"]
        for op in deltas[0]["ops"]
        if op["prior"] is None and text in op["payload"]
    )


def edits_of(deltas: list[dict], oid: int) -> list[tuple[int, int]]:
    """Return the (tid, actor) of every delta that updated the record."""
    return [
        (delta["tid"], delta["actor"])
        for delta in deltas
        for op in delta["ops"]
        if op["oid"] == oid and op["prior"] is not None
    ]


@pytest.fixture
def open_log(tmp_path: Path) -> Iterator[OpenLog]:
    """Return a factory opening a DeltaLog under tmp_path, by name or fresh; close all after."""
    opened: list[DeltaLog] = []

    def open_(name: str | None = None, **settings: int) -> DeltaLog:
        log = DeltaLog(tmp_path / (name or f"log-{next(_ids)}.deltalog"), **settings)
        opened.append(log)
        return log

    yield open_
    for log in opened:
        log.close()


@pytest.fixture
def recorded_history(
    store: dc.Store, open_log: OpenLog
) -> tuple[ReferenceApplier, DeltaLog]:
    """Commit a create, an update and a delete with a live applier and a log attached."""
    truth = ReferenceApplier()
    log = open_log()
    store.attach(truth)
    store.attach(log)
    cab = Cabinet(qid="C1", name="Tsumeb drawer")
    q = Specimen(label="quartz", mohs=7.0, cabinet=dc.Lazy.of(cab), tags=["clear"])
    store.root = [cab, q]
    store.commit()
    q.mohs = 7.5
    store.commit()
    store.delete(q)
    store.root = [cab]
    store.commit()
    return truth, log


@pytest.fixture
def two_commit_log(store: dc.Store, open_log: OpenLog) -> DeltaLog:
    """Commit three drawers and then a fourth, with a log attached."""
    log = open_log()
    store.attach(log)
    store.root = [Cabinet(qid=f"C{i}", name=f"drawer {i}") for i in range(3)]
    store.commit()
    store.root = [*store.root, Cabinet(qid="C9", name="late drawer")]
    store.commit()
    return log


@pytest.fixture
def closed_log_name(store: dc.Store, open_log: OpenLog) -> str:
    """Commit one drawer through a log, detach and close it, and return its name."""
    log = open_log("persist.deltalog")
    store.attach(log)
    store.root = [Cabinet(qid="C1", name="Tsumeb")]
    store.commit()
    store.detach(log)
    log.close()
    return "persist.deltalog"


@pytest.fixture
def buffered_log(store: dc.Store, open_log: OpenLog) -> DeltaLog:
    """Commit two drawers into a log that fsyncs only every third commit."""
    log = open_log(flush_every=3)
    store.attach(log)
    store.root = [Cabinet(qid="C1", name="a")]
    store.commit()
    store.root = [*store.root, Cabinet(qid="C2", name="b")]
    store.commit()
    return log


@pytest.fixture
def store_ahead_of_lossy_log(store: dc.Store, open_log: OpenLog) -> dc.Store:
    """Commit tid 1 into a log that never fsynced it, then detach the log."""
    log = open_log("lossy.deltalog", flush_every=5)
    store.attach(log)
    store.root = [Cabinet(qid="C1", name="a")]
    store.commit()
    store.detach(log)
    return store


@pytest.fixture
def rolled_log(store: dc.Store, open_log: OpenLog) -> DeltaLog:
    """Commit twenty drawers, one per commit, into a log with 64-byte segments."""
    log = open_log("rolled.deltalog", max_segment_bytes=64)
    store.attach(log)
    for i in range(20):
        store.root = [Cabinet(qid=f"C{i}", name=f"drawer {i}")]
        store.commit()
    return log


@pytest.fixture
def closed_rolled_log(rolled_log: DeltaLog) -> str:
    """Close the rolled log and return its name."""
    rolled_log.close()
    return "rolled.deltalog"


@pytest.fixture
def store_with_logless_tid_1(store: dc.Store) -> dc.Store:
    """Commit tid 1 before any log exists."""
    store.root = [Cabinet(qid="C1", name="before logging")]
    store.commit()
    return store


@pytest.fixture
def log_with_torn_tail(store: dc.Store, open_log: OpenLog, tmp_path: Path) -> tuple[Path, int]:
    """Close a one-commit log and forge crash debris past its committed length.

    Returns the segment file and the committed length of the segment.
    """
    log = open_log("torn.deltalog")
    store.attach(log)
    store.root = [Cabinet(qid="C1", name="a")]
    store.commit()
    log.close()
    segment = tmp_path / "torn.deltalog" / "data" / log._segments[-1].name
    with open(segment, "ab") as debris:
        debris.write(b"\x00\x00\x00\x00\x00\x00\x00\x05hello")
    return segment, log._segments[-1].nbytes


@pytest.fixture
def log_with_orphan_segment(store: dc.Store, open_log: OpenLog, tmp_path: Path) -> Path:
    """Close a one-commit log and plant a segment file its manifest never named."""
    log = open_log("orphan.deltalog")
    store.attach(log)
    store.root = [Cabinet(qid="C1", name="a")]
    store.commit()
    log.close()
    orphan = tmp_path / "orphan.deltalog" / "data" / "seg-009999.dlog"
    orphan.write_bytes(b"garbage from a crashed roll")
    return orphan


@pytest.fixture
def audited_log(store: dc.Store, open_log: OpenLog) -> DeltaLog:
    """Commit as anna, as the swarm and outside any scope, with a log and a pinned clock."""
    store._clock = pinned_clock
    log = open_log()
    store.attach(log)
    with store.acting_as(dc.Principal(uid=2)):
        store.store(dc.Actor(uid=2, display="Anna", human=True))
        cab = Cabinet(qid="Q1", name="drawer 7")
        store.store(cab)
        store.commit()
    with store.acting_as(dc.Principal(uid=900)):
        cab.name = "drawer 7 (oxides)"
        store.commit()
    store.store(Cabinet(qid="Q2", name="drawer 8"))
    store.commit()
    return log


@pytest.fixture
def pre_v2_log_name(store: dc.Store, tmp_path: Path) -> str:
    """Write a log dir through the store, then doctor its manifest to contract version 1."""
    log = DeltaLog(tmp_path / "vlog")
    store.attach(log)
    store.store(Cabinet(qid="Q1", name="drawer 7"))
    store.commit()
    store.close()
    manifest = tmp_path / "vlog" / "manifest.json"
    doctored = json.loads(manifest.read_text())
    doctored["contract_version"] = 1
    manifest.write_text(json.dumps(doctored))
    return "vlog"


@pytest.fixture
def stamped_delta() -> dict:
    """Build a well-formed tid 1 delta with every required v2 key."""
    return {
        "f": "datacrystal-delta",
        "v": 2,
        "tid": 1,
        "actor": 0,
        "at": 0,
        "ops": [],
        "types": [],
        "root": None,
    }


@pytest.fixture
def delta_without_actor(stamped_delta: dict) -> dict:
    """Build the tid 1 delta with its required actor key removed."""
    return {key: value for key, value in stamped_delta.items() if key != "actor"}


# -- conformance ----------------------------------------------------------------


def test_conformance_kit_runs_the_prior_unindex_section_for_the_delta_log(open_log: OpenLog) -> None:
    """Given the delta log, which replays through the reference applier
    When the conformance kit certifies it
    Then a prior un-index section ran
    """
    ran = check_delta_consumer(open_log, content=replayed_state_of)

    assert any("prior" in label for label in ran)


def test_conformance_kit_runs_the_delete_section_for_the_delta_log(open_log: OpenLog) -> None:
    """Given the delta log, which replays through the reference applier
    When the conformance kit certifies it
    Then a delete totality section ran
    """
    ran = check_delta_consumer(open_log, content=replayed_state_of)

    assert any("delete" in label for label in ran)


# -- end-to-end over both backends ------------------------------------------------


def test_replay_lists_every_commit_gapless_in_tid_order(
    recorded_history: tuple[ReferenceApplier, DeltaLog],
) -> None:
    """Given a log attached through a create, an update and a delete commit
    When the log is replayed
    Then the deltas are tids 1, 2 and 3 in order
    """
    _, log = recorded_history

    deltas = list(log.replay())

    assert tids_of(deltas) == [1, 2, 3]


def test_log_watermark_is_the_last_commit(
    recorded_history: tuple[ReferenceApplier, DeltaLog],
) -> None:
    """Given a log attached through a create, an update and a delete commit
    When the log's watermark is read
    Then it is tid 3
    """
    _, log = recorded_history

    watermark = log.watermark

    assert watermark == 3


def test_replaying_the_log_reproduces_the_live_applier_state(
    recorded_history: tuple[ReferenceApplier, DeltaLog],
) -> None:
    """Given a log and a live applier that both followed a create, an update and a delete
    When the state reconstructed from the log is digested
    Then it equals the state digest the live applier folded
    """
    truth, log = recorded_history

    replayed = log.replayed_state()

    assert replayed == truth.state_digest()


def test_replay_after_tid_yields_only_the_later_commits(two_commit_log: DeltaLog) -> None:
    """Given a log with two commits
    When it is replayed after tid 1
    Then only tid 2 is yielded
    """
    later = list(two_commit_log.replay(after_tid=1))

    assert tids_of(later) == [2]


def test_replay_without_a_tid_yields_every_commit(two_commit_log: DeltaLog) -> None:
    """Given a log with two commits
    When it is replayed without a tid
    Then tids 1 and 2 are yielded
    """
    everything = list(two_commit_log.replay())

    assert tids_of(everything) == [1, 2]


# -- durability: reopen resume, fsync window --------------------------------------


def test_reopened_log_resumes_at_the_persisted_watermark(
    closed_log_name: str, open_log: OpenLog
) -> None:
    """Given a log closed after one commit
    When it is reopened
    Then its watermark is 1
    """
    reopened = open_log(closed_log_name)

    assert reopened.watermark == 1


def test_reopened_log_has_the_persisted_commit_durable(
    closed_log_name: str, open_log: OpenLog
) -> None:
    """Given a log closed after one commit
    When it is reopened
    Then its durable watermark is 1
    """
    reopened = open_log(closed_log_name)

    assert reopened.durable_watermark == 1


def test_reopened_log_replays_the_persisted_commit(
    closed_log_name: str, open_log: OpenLog
) -> None:
    """Given a log closed after one commit
    When it is reopened and replayed
    Then tid 1 is yielded
    """
    reopened = open_log(closed_log_name)

    deltas = list(reopened.replay())

    assert tids_of(deltas) == [1]


def test_reopened_log_accepts_the_store_and_records_the_next_commit(
    store: dc.Store, closed_log_name: str, open_log: OpenLog
) -> None:
    """Given a log closed after one commit, and reopened at the store's watermark
    When it is attached and a second drawer is committed
    Then replay yields tids 1 and 2
    """
    reopened = open_log(closed_log_name)
    store.attach(reopened)
    store.root = [*store.root, Cabinet(qid="C2", name="Broken Hill")]

    store.commit()

    assert tids_of(list(reopened.replay())) == [1, 2]


def test_buffered_commits_advance_the_live_watermark(buffered_log: DeltaLog) -> None:
    """Given a log that fsyncs every third commit, after two commits
    When its watermark is read
    Then it is 2
    """
    watermark = buffered_log.watermark

    assert watermark == 2


def test_buffered_commits_are_not_yet_durable(buffered_log: DeltaLog) -> None:
    """Given a log that fsyncs every third commit, after two commits
    When its durable watermark is read
    Then it is 0
    """
    durable = buffered_log.durable_watermark

    assert durable == 0


def test_third_commit_flushes_the_durable_watermark(
    store: dc.Store, buffered_log: DeltaLog
) -> None:
    """Given a log that fsyncs every third commit, after two buffered commits
    When a third drawer is committed
    Then the durable watermark is 3
    And bytes were flushed
    """
    store.root = [*store.root, Cabinet(qid="C3", name="c")]

    store.commit()

    assert buffered_log.durable_watermark == 3
    assert buffered_log.bytes_flushed > 0


def test_lossy_log_reopens_behind_the_store(
    store_ahead_of_lossy_log: dc.Store, open_log: OpenLog
) -> None:
    """Given a log that lost its buffered tid 1 in a crash before the flush
    When it is reopened from disk
    Then its watermark is 0
    """
    reopened = open_log("lossy.deltalog")

    assert reopened.watermark == 0


def test_log_behind_the_store_is_refused_on_reattach(
    store_ahead_of_lossy_log: dc.Store, open_log: OpenLog
) -> None:
    """Given a log reopened from disk at watermark 0 after the store reached tid 1
    When it is attached to the store
    Then the engine refuses it as a gap, to be rebuilt via bootstrap (spec §5)
    """
    reopened = open_log("lossy.deltalog")

    with pytest.raises(DeltaGapError):
        store_ahead_of_lossy_log.attach(reopened)


# -- segment rolling --------------------------------------------------------------


def test_rolling_reaches_the_last_commit_in_the_watermark(rolled_log: DeltaLog) -> None:
    """Given a log with 64-byte segments after twenty commits
    When its watermark is read
    Then it is 20
    """
    watermark = rolled_log.watermark

    assert watermark == 20


def test_tiny_segments_force_the_log_to_roll(rolled_log: DeltaLog) -> None:
    """Given a log with 64-byte segments after twenty commits
    When its segments are counted
    Then there is more than one
    """
    segment_count = len(rolled_log._segments)

    assert segment_count > 1


def test_rolled_log_replays_in_tid_order(rolled_log: DeltaLog) -> None:
    """Given a log with 64-byte segments after twenty commits
    When it is replayed
    Then tids 1 to 20 are yielded in order
    """
    deltas = list(rolled_log.replay())

    assert tids_of(deltas) == list(range(1, 21))


def test_reopened_rolled_log_replays_in_tid_order(
    closed_rolled_log: str, open_log: OpenLog
) -> None:
    """Given a log with 64-byte segments closed after twenty commits
    When it is reopened and replayed
    Then tids 1 to 20 are yielded in order
    """
    reopened = open_log(closed_rolled_log, max_segment_bytes=64)

    deltas = list(reopened.replay())

    assert tids_of(deltas) == list(range(1, 21))


# -- mid-life bootstrap -----------------------------------------------------------


def test_bootstrap_pins_the_log_to_the_join_point(
    store_with_logless_tid_1: dc.Store, tmp_path: Path
) -> None:
    """Given a store that committed tid 1 before any log existed
    When a log is bootstrapped from a snapshot
    Then its genesis tid is 1
    And its watermark is pinned to 1
    And it keeps the snapshot's type lineage
    """
    with store_with_logless_tid_1.snapshot() as snap:
        expected_types = set(snap.types)
        log = DeltaLog.bootstrap(tmp_path / "joined.deltalog", snap)

    assert log.genesis_tid == 1
    assert log.watermark == 1
    assert set(log.genesis_types) == expected_types
    log.close()


def test_bootstrapped_log_records_only_post_join_history(
    store_with_logless_tid_1: dc.Store, tmp_path: Path
) -> None:
    """Given a log bootstrapped at tid 1 and attached
    When a second drawer is committed
    Then replay yields only tid 2
    """
    store = store_with_logless_tid_1
    with store.snapshot() as snap:
        log = DeltaLog.bootstrap(tmp_path / "joined.deltalog", snap)
    store.attach(log)
    store.root = [*store.root, Cabinet(qid="C2", name="after logging")]

    store.commit()

    assert tids_of(list(log.replay())) == [2]
    log.close()


# -- crash-debris reconciliation (the open-time invariant) ------------------------


def test_reopen_truncates_bytes_past_the_committed_length(
    log_with_torn_tail: tuple[Path, int], open_log: OpenLog
) -> None:
    """Given a segment with crash debris past the manifest's committed length
    When the log is reopened
    Then the segment is truncated to the committed length
    """
    segment, committed = log_with_torn_tail

    open_log("torn.deltalog")

    assert segment.stat().st_size == committed


def test_reopen_after_a_torn_append_replays_an_exact_commit_prefix(
    log_with_torn_tail: tuple[Path, int], open_log: OpenLog
) -> None:
    """Given a segment with crash debris past the manifest's committed length
    When the log is reopened and replayed
    Then only tid 1 is yielded
    """
    reopened = open_log("torn.deltalog")

    deltas = list(reopened.replay())

    assert tids_of(deltas) == [1]


def test_reopen_sweeps_a_segment_the_manifest_never_named(
    log_with_orphan_segment: Path, open_log: OpenLog
) -> None:
    """Given an orphan segment file left by a crash after a roll, before the manifest commit
    When the log is reopened
    Then the orphan file is gone
    """
    open_log("orphan.deltalog")

    assert not log_with_orphan_segment.exists()


def test_reopen_after_an_orphan_sweep_replays_the_commit(
    log_with_orphan_segment: Path, open_log: OpenLog
) -> None:
    """Given an orphan segment file left by a crash after a roll, before the manifest commit
    When the log is reopened and replayed
    Then only tid 1 is yielded
    """
    reopened = open_log("orphan.deltalog")

    deltas = list(reopened.replay())

    assert tids_of(deltas) == [1]


# -- config honesty ---------------------------------------------------------------


def test_directory_that_is_not_a_delta_log_is_refused(tmp_path: Path) -> None:
    """Given a directory whose manifest names another format
    When a DeltaLog is opened on it
    Then the config error is raised
    """
    path = tmp_path / "bogus.deltalog"
    (path / "data").mkdir(parents=True)
    (path / "manifest.json").write_text('{"format": "something-else"}')

    with pytest.raises(DeltaLogConfigError):
        DeltaLog(path)


def test_flush_every_below_one_is_refused(tmp_path: Path) -> None:
    """Given a flush_every of 0
    When a DeltaLog is opened with it
    Then the config error is raised
    """
    with pytest.raises(DeltaLogConfigError):
        DeltaLog(tmp_path / "x.deltalog", flush_every=0)


def test_replay_stamps_each_commit_with_the_committing_actor(audited_log: DeltaLog) -> None:
    """Given commits as anna, as the swarm and outside any scope, through an attached log
    When the log is replayed
    Then the actors are 2, 900 and 0 in commit order
    """
    deltas = list(audited_log.replay())

    assert [delta["actor"] for delta in deltas] == [2, 900, 0]


def test_replay_stamps_each_commit_with_the_clock(audited_log: DeltaLog) -> None:
    """Given commits through an attached log under a pinned clock
    When the log is replayed
    Then every delta carries the pinned time
    """
    deltas = list(audited_log.replay())

    assert all(delta["at"] == 1_700_000_000_000_000_000 for delta in deltas)


def test_history_recipe_finds_who_edited_a_record_from_payload_and_prior(
    audited_log: DeltaLog,
) -> None:
    """Given anna created drawer 7 in tid 1 and the swarm renamed it in tid 2
    When the history recipe walks replay for updates of that record
    Then the one edit is tid 2 by actor 900
    """
    deltas = list(audited_log.replay())
    drawer_7 = oid_created_with(deltas, b"drawer 7")

    edits = edits_of(deltas, drawer_7)

    assert edits == [(2, 900)]


def test_audited_edit_is_what_the_store_serves(store: dc.Store, audited_log: DeltaLog) -> None:
    """Given the swarm renamed drawer 7 through an attached log
    When the drawer is fetched from the store
    Then it exists
    And it carries the swarm's name
    """
    fetched = store.get(Cabinet, qid="Q1")

    assert fetched is not None
    assert fetched.name == "drawer 7 (oxides)"


def test_pre_v2_log_dir_is_refused_at_reopen(pre_v2_log_name: str, tmp_path: Path) -> None:
    """Given a log dir whose manifest records contract version 1
    When it is reopened
    Then it is refused as never migrated, not appended to with v2 frames
    """
    with pytest.raises(DeltaLogConfigError, match="never migrated"):
        DeltaLog(tmp_path / pre_v2_log_name)


def test_frame_missing_a_required_key_is_refused_at_write_time(
    open_log: OpenLog, delta_without_actor: dict
) -> None:
    """Given a delta without its actor key
    When the log applies it
    Then it is refused as missing a required key
    And the watermark stays 0
    """
    log = open_log()

    with pytest.raises(DeltaFormatError, match="missing required key"):
        log.apply(delta_without_actor)

    assert log.watermark == 0


def test_well_formed_delta_lands_after_a_refused_frame(
    open_log: OpenLog, stamped_delta: dict, delta_without_actor: dict
) -> None:
    """Given a log that refused a delta without its actor key
    When the well-formed delta is applied
    Then it lands
    """
    log = open_log()
    with pytest.raises(DeltaFormatError, match="missing required key"):
        log.apply(delta_without_actor)

    landed = log.apply(stamped_delta)

    assert landed is True
