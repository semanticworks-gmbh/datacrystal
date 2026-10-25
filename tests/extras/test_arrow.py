"""datacrystal[arrow] — parquet mirrors as a certified COMMIT-DELTA-v1 consumer.

ROADMAP item 7: the watermark pipeline's second real consumer, validated
against the draft contract before the lock at the tag (the same rationale
that put the FTS extra in-tree pre-tag). Engine tests run over both
backends via ``store_factory``.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
from collections.abc import Callable, Iterable, Iterator
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, NamedTuple

import pytest

pytest.importorskip("pyarrow", reason="datacrystal[arrow] extra not installed")

import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.types as pa_types

import datacrystal as dc
from datacrystal import Snapshot
from datacrystal.arrow import (
    ArrowMirror,
    MirrorConfigError,
    decode_fallback,
)
from datacrystal.contract.applier import DeltaGapError
from datacrystal.testing import STREAM_TYPENAME, check_delta_consumer

_ids = itertools.count()
QUARRY_TYPENAME = "tests.extras.test_arrow:Quarry"


@dc.entity
class Quarry:
    """A quarry a find was recovered from."""

    qid: Annotated[str, dc.Unique]
    name: str


@dc.entity
class Find:
    """A mineral find with a measure, an indexed grade, a date and a lazy quarry."""

    label: str
    mass_g: float | None = None
    grade: Annotated[str | None, dc.Index] = None
    found_on: dt.date | None = None
    quarry: dc.Lazy[Quarry] | None = None
    tags: Any = None  # Any on purpose: the lattice tests feed it mixed shapes


def rows(mirror: ArrowMirror, target: type | str) -> list[dict]:
    """Return the mirror's table for a class or typename as a list of row dicts."""
    return mirror.table(target).to_pylist()


def content_probe(mirror: ArrowMirror) -> object:
    """Return the derived state for the conformance kit: every stream-type row."""
    return sorted(tuple(sorted(row.items())) for row in rows(mirror, STREAM_TYPENAME))


def by_column(table: pa.Table, key: str, value: str) -> dict:
    """Return a table's rows as a dict from the key column to the value column."""
    return {row[key]: row[value] for row in table.to_pylist()}


def oids_of(views: Iterable[Any]) -> list[int]:
    """Return the oids of the entity views."""
    return [view.oid for view in views]


class MirrorShop:
    """Opens parquet mirrors under one directory and closes every one of them afterwards."""

    def __init__(self, directory: Path) -> None:
        """Remember the directory the mirrors live in."""
        self.directory = directory
        self.opened: list[ArrowMirror] = []

    def open(
        self,
        name: str | None = None,
        only: Iterable[type | str] | None = None,
        flush_every: int | None = None,
        max_segments: int = 16,
    ) -> ArrowMirror:
        """Open a mirror by name, or a fresh one; flush_every None leaves the library default."""
        directory = self.directory / (name or f"mirror-{next(_ids)}")
        if flush_every is None:
            mirror = ArrowMirror(directory, only=only, max_segments=max_segments)
        else:
            mirror = ArrowMirror(
                directory, only=only, flush_every=flush_every, max_segments=max_segments
            )
        self.opened.append(mirror)
        return mirror

    def bootstrap(
        self, name: str, snapshot: Snapshot, batch: int = 50_000, max_segments: int = 16
    ) -> ArrowMirror:
        """Build a mirror directory from a snapshot."""
        mirror = ArrowMirror.bootstrap(
            self.directory / name, snapshot, batch=batch, max_segments=max_segments
        )
        self.opened.append(mirror)
        return mirror

    def close_all(self) -> None:
        """Close every mirror this shop opened."""
        for mirror in self.opened:
            mirror.close()


@pytest.fixture
def shop(tmp_path: Path) -> Iterator[MirrorShop]:
    """Provide a mirror shop under tmp_path and close its mirrors afterwards."""
    mirror_shop = MirrorShop(tmp_path)
    yield mirror_shop
    mirror_shop.close_all()


@pytest.fixture
def fresh_mirror_factory(shop: MirrorShop) -> Callable[[], ArrowMirror]:
    """Return a factory for fresh mirrors."""
    return shop.open


@pytest.fixture
def mirror(shop: MirrorShop, store: dc.Store) -> ArrowMirror:
    """Open a fresh mirror and attach it to the store."""
    attached = shop.open()
    store.attach(attached)
    return attached


# -- conformance ----------------------------------------------------------------


def test_conformance_kit_runs_the_prior_section_for_the_arrow_mirror(
    fresh_mirror_factory: Callable[[], ArrowMirror],
) -> None:
    """Given the arrow mirror as a consumer
    When the conformance kit certifies it
    Then the prior un-index section ran
    """
    ran = check_delta_consumer(fresh_mirror_factory, content=content_probe)

    assert any("prior" in label for label in ran)


def test_conformance_kit_runs_the_delete_section_for_the_arrow_mirror(
    fresh_mirror_factory: Callable[[], ArrowMirror],
) -> None:
    """Given the arrow mirror as a consumer
    When the conformance kit certifies it
    Then the delete totality section ran
    """
    ran = check_delta_consumer(fresh_mirror_factory, content=content_probe)

    assert any("delete" in label for label in ran)


# -- end-to-end over both backends ------------------------------------------------


class MirroredAzurite(NamedTuple):
    """A mirror of a committed quarry and an azurite find that refers to it."""

    quarry: Quarry
    find: Find
    mirror: ArrowMirror


@pytest.fixture
def mirrored_azurite(store: dc.Store, mirror: ArrowMirror) -> MirroredAzurite:
    """Commit a quarry and an azurite find with every column kind into a mirrored store."""
    quarry = Quarry(qid="Q1", name="Tsumeb")
    find = Find(
        label="azurite vug",
        mass_g=412.5,
        grade="A",
        found_on=dt.date(2026, 5, 1),
        quarry=dc.Lazy.of(quarry),
        tags=["blue", "vug"],
    )
    store.root = [quarry, find]
    store.commit()
    return MirroredAzurite(quarry, find, mirror)


def test_mirror_holds_one_row_per_committed_find(mirrored_azurite: MirroredAzurite) -> None:
    """Given a committed azurite find in a mirrored store
    When the Find table is read
    Then it has one row
    """
    table = mirrored_azurite.mirror.table(Find)

    assert table.num_rows == 1


@pytest.mark.parametrize(
    ("column", "expected"), [("label", "azurite vug"), ("mass_g", 412.5), ("grade", "A")]
)
def test_mirrored_row_carries_the_plain_columns(
    column: str, expected: object, mirrored_azurite: MirroredAzurite
) -> None:
    """Given a committed azurite find in a mirrored store
    When the Find row is read
    Then a plain column carries the committed value
    """
    row = rows(mirrored_azurite.mirror, Find)[0]

    assert row[column] == expected


def test_mirrored_row_carries_the_date_column(mirrored_azurite: MirroredAzurite) -> None:
    """Given a find committed with the date 2026-05-01
    When the Find row is read
    Then found_on is that date
    """
    row = rows(mirrored_azurite.mirror, Find)[0]

    assert row["found_on"] == dt.date(2026, 5, 1)


def test_mirrored_row_carries_the_list_column(mirrored_azurite: MirroredAzurite) -> None:
    """Given a find committed with the tags blue and vug
    When the Find row is read
    Then tags is the list blue, vug
    """
    row = rows(mirrored_azurite.mirror, Find)[0]

    assert row["tags"] == ["blue", "vug"]


def test_entity_reference_mirrors_as_the_oid_of_its_target(
    mirrored_azurite: MirroredAzurite,
) -> None:
    """Given a find that refers to a quarry
    When the Find row is read
    Then the reference column is the quarry row's int64 OID
    """
    row = rows(mirrored_azurite.mirror, Find)[0]
    quarry_oid = rows(mirrored_azurite.mirror, Quarry)[0]["__oid__"]

    assert row["quarry"] == quarry_oid


def test_update_folds_newest_wins(store: dc.Store, mirrored_azurite: MirroredAzurite) -> None:
    """Given a mirrored azurite find with 412.5 g
    When its mass is changed to 388.0 g and committed
    Then the Find row carries 388.0
    """
    mirrored_azurite.find.mass_g = 388.0

    store.commit()

    assert rows(mirrored_azurite.mirror, Find)[0]["mass_g"] == 388.0


def test_delete_drops_the_row(store: dc.Store, mirrored_azurite: MirroredAzurite) -> None:
    """Given a mirrored azurite find
    When it is deleted and committed
    Then the Find table has no row
    """
    store.delete(mirrored_azurite.find)
    store.root = [mirrored_azurite.quarry]

    store.commit()

    assert rows(mirrored_azurite.mirror, Find) == []


def test_delete_of_a_find_leaves_its_quarry_row(
    store: dc.Store, mirrored_azurite: MirroredAzurite
) -> None:
    """Given a mirrored azurite find and its quarry
    When the find is deleted and committed
    Then the Quarry table still has one row
    """
    store.delete(mirrored_azurite.find)
    store.root = [mirrored_azurite.quarry]

    store.commit()

    assert mirrored_azurite.mirror.table(Quarry).num_rows == 1


class ClosedQuarryMirror(NamedTuple):
    """A mirror closed after one committed quarry, with what it held."""

    name: str
    watermark: int
    quarry_rows: list[dict]


@pytest.fixture
def closed_quarry_mirror(store: dc.Store, shop: MirrorShop) -> ClosedQuarryMirror:
    """Mirror one quarry commit, detach and close the mirror."""
    mirror = shop.open("persist.mirror")
    store.attach(mirror)
    store.root = [Quarry(qid="Q1", name="Tsumeb")]
    store.commit()
    store.detach(mirror)
    held = ClosedQuarryMirror("persist.mirror", mirror.watermark, rows(mirror, Quarry))
    mirror.close()
    return held


def test_reopened_mirror_keeps_its_watermark(
    closed_quarry_mirror: ClosedQuarryMirror, shop: MirrorShop
) -> None:
    """Given a mirror closed after one commit
    When it is reopened
    Then its watermark is the one it had when closed
    """
    reopened = shop.open(closed_quarry_mirror.name)

    assert reopened.watermark == closed_quarry_mirror.watermark


def test_reopened_mirror_keeps_its_rows(
    closed_quarry_mirror: ClosedQuarryMirror, shop: MirrorShop
) -> None:
    """Given a mirror closed after one quarry commit
    When it is reopened
    Then its Quarry rows are the ones it held when closed
    """
    reopened = shop.open(closed_quarry_mirror.name)

    assert rows(reopened, Quarry) == closed_quarry_mirror.quarry_rows


def test_reopened_mirror_is_accepted_at_the_store_watermark_and_follows_on(
    store: dc.Store, closed_quarry_mirror: ClosedQuarryMirror, shop: MirrorShop
) -> None:
    """Given a mirror reopened at the store's last transaction id
    When it is attached and a second quarry is committed
    Then the Quarry table has two rows
    """
    reopened = shop.open(closed_quarry_mirror.name)
    store.attach(reopened)
    store.root = [*store.root, Quarry(qid="Q2", name="Broken Hill")]

    store.commit()

    assert reopened.table(Quarry).num_rows == 2


@pytest.fixture
def store_ahead_of_closed_mirror(store: dc.Store, shop: MirrorShop) -> dc.Store:
    """Mirror one quarry, detach and close the mirror, then commit a second quarry it misses."""
    mirror = shop.open("stale.mirror")
    store.attach(mirror)
    store.root = [Quarry(qid="Q1", name="Tsumeb")]
    store.commit()
    store.detach(mirror)
    mirror.close()
    store.root = [*store.root, Quarry(qid="Q2", name="Broken Hill")]
    store.commit()
    return store


def test_stale_mirror_is_refused_on_attach(
    store_ahead_of_closed_mirror: dc.Store, shop: MirrorShop
) -> None:
    """Given a mirror that missed the store's second delta
    When it is reopened and attached
    Then the attach is refused as a gap
    """
    reopened = shop.open("stale.mirror")

    with pytest.raises(DeltaGapError):
        store_ahead_of_closed_mirror.attach(reopened)


def test_stale_mirror_is_rebuilt_by_bootstrap_and_accepted(
    store_ahead_of_closed_mirror: dc.Store, shop: MirrorShop
) -> None:
    """Given a mirror that missed the store's second delta
    When it is rebuilt from a snapshot by bootstrap and attached
    Then the Quarry table has both rows
    """
    with store_ahead_of_closed_mirror.snapshot() as snap:
        rebuilt = shop.bootstrap("stale.mirror", snap)
    store_ahead_of_closed_mirror.attach(rebuilt)

    assert rebuilt.table(Quarry).num_rows == 2


@pytest.fixture
def incremental_mirror(store: dc.Store, shop: MirrorShop) -> ArrowMirror:
    """Bootstrap a mirror mid-life, attach it, then commit an update and an addition."""
    quarry = Quarry(qid="Q1", name="Tsumeb")
    find = Find(label="azurite", quarry=dc.Lazy.of(quarry))
    store.root = [quarry, find]
    store.commit()
    with store.snapshot() as snap:
        mirror = shop.bootstrap("boot.mirror", snap)
    store.attach(mirror)
    find.mass_g = 99.5
    store.root = [*store.root, Find(label="malachite", grade="B")]
    store.commit()
    return mirror


def test_bootstrapped_then_fed_mirror_equals_a_rebuild_for_quarries(
    store: dc.Store, shop: MirrorShop, incremental_mirror: ArrowMirror
) -> None:
    """Given a mirror bootstrapped mid-life and then fed deltas (fitness #13)
    When a from-scratch rebuild is made at the same watermark
    Then both Quarry tables hold the same rows
    """
    with store.snapshot() as snap:
        rebuilt = shop.bootstrap("rebuilt.mirror", snap)

    assert rows(incremental_mirror, Quarry) == rows(rebuilt, Quarry)


def test_bootstrapped_then_fed_mirror_equals_a_rebuild_for_finds(
    store: dc.Store, shop: MirrorShop, incremental_mirror: ArrowMirror
) -> None:
    """Given a mirror bootstrapped mid-life and then fed deltas (fitness #13)
    When a from-scratch rebuild is made at the same watermark
    Then both Find tables hold the same rows
    """
    with store.snapshot() as snap:
        rebuilt = shop.bootstrap("rebuilt.mirror", snap)

    assert rows(incremental_mirror, Find) == rows(rebuilt, Find)


def test_bootstrapped_then_fed_mirror_ends_at_the_rebuilt_watermark(
    store: dc.Store, shop: MirrorShop, incremental_mirror: ArrowMirror
) -> None:
    """Given a mirror bootstrapped mid-life and then fed deltas (fitness #13)
    When a from-scratch rebuild is made at the same watermark
    Then both stand at the same watermark
    """
    with store.snapshot() as snap:
        rebuilt = shop.bootstrap("rebuilt.mirror", snap)

    assert incremental_mirror.watermark == rebuilt.watermark


# -- schema lattice ---------------------------------------------------------------


def test_float_mass_mirrors_as_a_float64_column(store: dc.Store, mirror: ArrowMirror) -> None:
    """Given a find committed with a float mass
    When the Find table is read
    Then the mass_g column is float64
    """
    store.root = [Find(label="a", mass_g=100.0)]

    store.commit()

    assert mirror.table(Find).schema.field("mass_g").type == pa.float64()


@pytest.fixture
def mirrored_dict_tags(store: dc.Store, mirror: ArrowMirror) -> ArrowMirror:
    """Commit a find whose tags are a dict into a mirrored store."""
    store.root = [Find(label="x", tags={"color": "blue", "n": 3})]
    store.commit()
    return mirror


def test_mixed_shapes_mirror_as_msgpack_binary(mirrored_dict_tags: ArrowMirror) -> None:
    """Given a find whose tags field holds a dict, the top of the total lattice
    When the Find row is read
    Then tags is msgpack binary, never a wedged mirror
    """
    row = rows(mirrored_dict_tags, Find)[0]

    assert isinstance(row["tags"], bytes)


def test_decode_fallback_restores_the_dict_value(mirrored_dict_tags: ArrowMirror) -> None:
    """Given a find whose tags field holds a dict mirrored as msgpack binary
    When decode_fallback decodes the tags
    Then the original dict is restored
    """
    row = rows(mirrored_dict_tags, Find)[0]

    assert decode_fallback(row["tags"]) == {"color": "blue", "n": 3}


@pytest.fixture
def mirrored_promoted_tags(store: dc.Store, mirror: ArrowMirror) -> ArrowMirror:
    """Commit a list-tag find, then a dict-tag find that promotes the column to fallback."""
    store.root = [Find(label="first", tags=["plain", "strings"])]
    store.commit()
    store.root = [*store.root, Find(label="second", tags={"odd": True})]
    store.commit()
    return mirror


def test_promotion_recasts_the_old_segment_rows(mirrored_promoted_tags: ArrowMirror) -> None:
    """Given a later delta that promoted the tags column from list<str> to fallback
    When the earlier row is read back
    Then it decodes under the new tag to its original list
    """
    tags_by_label = by_column(mirrored_promoted_tags.table(Find), "label", "tags")

    assert decode_fallback(tags_by_label["first"]) == ["plain", "strings"]


def test_promotion_keeps_the_new_segment_rows(mirrored_promoted_tags: ArrowMirror) -> None:
    """Given a later delta that promoted the tags column from list<str> to fallback
    When the later row is read back
    Then it decodes to its original dict
    """
    tags_by_label = by_column(mirrored_promoted_tags.table(Find), "label", "tags")

    assert decode_fallback(tags_by_label["second"]) == {"odd": True}


CET = dt.timezone(dt.timedelta(hours=2))
AWARE = dt.datetime(2026, 6, 12, 14, 30, 5, tzinfo=CET)


@pytest.fixture
def mirrored_temporals(store: dc.Store, mirror: ArrowMirror) -> ArrowMirror:
    """Commit a find with a date and a list of one timezone-aware datetime."""
    store.root = [Find(label="t", tags=[AWARE], found_on=dt.date(2026, 6, 12))]
    store.commit()
    return mirror


def test_date_mirrors_as_a_date32_column(mirrored_temporals: ArrowMirror) -> None:
    """Given a find with a date
    When the Find table is read
    Then found_on is a date32 column
    """
    table = mirrored_temporals.table(Find)

    assert table.schema.field("found_on").type == pa.date32()


def test_date_value_survives_the_mirror(mirrored_temporals: ArrowMirror) -> None:
    """Given a find with the date 2026-06-12
    When the Find row is read
    Then found_on is that date
    """
    row = rows(mirrored_temporals, Find)[0]

    assert row["found_on"] == dt.date(2026, 6, 12)


def test_aware_datetimes_mirror_as_utc_timestamps(mirrored_temporals: ArrowMirror) -> None:
    """Given a find with a list of one aware datetime (format-v2 codec)
    When the Find table is read
    Then tags is a list of UTC microsecond timestamps
    """
    table = mirrored_temporals.table(Find)

    assert table.schema.field("tags").type == pa.list_(pa.timestamp("us", tz="UTC"))


def test_aware_datetime_keeps_its_instant(mirrored_temporals: ArrowMirror) -> None:
    """Given a find with an aware datetime at UTC+2
    When the Find row is read
    Then the mirrored value is the same instant, compared tz-aware
    """
    row = rows(mirrored_temporals, Find)[0]

    assert row["tags"][0] == AWARE


@pytest.fixture
def mirror_with_newer_manifest(store: dc.Store, shop: MirrorShop, tmp_path: Path) -> str:
    """Close a one-quarry mirror and bump its manifest to a newer format version."""
    mirror = shop.open("newer.mirror")
    store.attach(mirror)
    store.root = [Quarry(qid="Q1", name="Tsumeb")]
    store.commit()
    store.detach(mirror)
    mirror.close()
    manifest_path = tmp_path / "newer.mirror" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] += 1
    manifest_path.write_text(json.dumps(manifest))
    return "newer.mirror"


def test_newer_mirror_format_is_refused(mirror_with_newer_manifest: str, shop: MirrorShop) -> None:
    """Given a manifest from a newer datacrystal[arrow] (format honesty, invariant 9)
    When the mirror is opened
    Then it is refused loudly, never half-read
    """
    with pytest.raises(MirrorConfigError):
        shop.open(mirror_with_newer_manifest)


# -- W4-7: a class that flips to protected=True mid-life --------------------------
#
# A mirror already holding pre-protection parquet segments (columns: the plain
# fields, no _dc_* labels) keeps consuming once the class turns protected — the
# NEW segments carry the four _dc_* label columns. The segment-promotion lattice
# must fold the two shapes into one readable table (pre-protection rows get null
# labels — the incremental mirror never rewrites already-mirrored rows), and a
# root re-bootstrap (a full copy, migrate() precedent) must materialize the R7
# legacy fill for the pre-protection rows. Mirroring protected data demands the
# audited store root: a non-root bootstrap fails closed. Simulated with the same
# same-typename retrofit pattern the store's own R7 stamping tests use.

ORG = 1
ANNA = dc.Principal(uid=2, memberships={ORG: dc.CURATOR})
_RELIC = "tests.extras.test_arrow:Relic"


def _relic(*, protected: bool) -> type:
    """Define the 'Relic' entity again under the same typename.

    Persisted records decode through their own lineage row, simulating a protected=True
    retrofit between runs, exactly like the store's R7 stamping tests.
    """
    namespace: dict = {
        "__module__": __name__,
        "__qualname__": "Relic",
        "__annotations__": {"name": Annotated[str, dc.Unique], "grade": str},
        "grade": "common",
    }
    return dc.entity(type("Relic", (), namespace), protected=protected)  # pyrefly: ignore[no-matching-overload]


class UnprotectedRelics(NamedTuple):
    """The directory and the closed mirror of two unprotected relics."""

    name: str
    mirror: ArrowMirror


class FlippedRelics(NamedTuple):
    """The store and mirror after the Relic class flipped protected and one relic was born."""

    store: dc.Store
    mirror: ArrowMirror
    relic_class: type


@pytest.fixture
def unprotected_relics(store_factory: Callable[[], dc.Store], shop: MirrorShop) -> UnprotectedRelics:
    """Mirror two unprotected relics to an old segment without _dc_* label columns, then close."""
    relic = _relic(protected=False)
    store = store_factory()
    mirror = shop.open("relic.mirror")
    store.attach(mirror)
    store.root = [relic(name="w1-a", grade="fine"), relic(name="w1-b", grade="common")]
    store.commit()
    store.detach(mirror)
    mirror.close()
    store.close()  # single-writer sqlite: release the lock before reopening
    return UnprotectedRelics("relic.mirror", mirror)


@pytest.fixture
def flipped_relics(
    unprotected_relics: UnprotectedRelics, store_factory: Callable[[], dc.Store], shop: MirrorShop
) -> Iterator[FlippedRelics]:
    """Flip Relic to protected and commit a new protected relic through the reopened mirror."""
    relic = _relic(protected=True)
    store = store_factory()
    reopened = shop.open(unprotected_relics.name)
    store.attach(reopened)
    with store.acting_as(ANNA):
        fresh = relic(name="w2-new", grade="rare")
        store.store(fresh)
        dc.share(fresh, ORG, read=dc.VIEWER, write=dc.CURATOR)
        store.commit()
    yield FlippedRelics(store, reopened, relic)
    store.close()


def test_unprotected_segment_has_no_label_columns(unprotected_relics: UnprotectedRelics) -> None:
    """Given two unprotected relics mirrored to an old segment
    When the mirror's Relic columns are read
    Then there is no _dc_owner column
    """
    columns = unprotected_relics.mirror.table(_RELIC).column_names

    assert "_dc_owner" not in columns


def test_reopened_mirror_resumes_without_a_gap_after_the_flip(
    unprotected_relics: UnprotectedRelics, shop: MirrorShop
) -> None:
    """Given a mirror closed after two relics were mirrored while the class was unprotected
    When it is reopened after the Relic class flipped to protected
    Then its watermark is the closed mirror's, with no gap
    """
    _relic(protected=True)

    reopened = shop.open(unprotected_relics.name)

    assert reopened.watermark == unprotected_relics.mirror.watermark


def test_promotion_folds_both_segments_into_three_rows(flipped_relics: FlippedRelics) -> None:
    """Given two unlabelled relic rows and one labelled one
    When the Relic table is read
    Then it has three rows
    """
    table = flipped_relics.mirror.table(flipped_relics.relic_class)

    assert table.num_rows == 3


def test_promotion_adds_the_label_columns_to_the_schema(flipped_relics: FlippedRelics) -> None:
    """Given two unlabelled relic rows and one labelled one
    When the Relic table schema is read
    Then it includes _dc_owner
    """
    table = flipped_relics.mirror.table(flipped_relics.relic_class)

    assert "_dc_owner" in table.schema.names


def test_pre_protection_rows_promote_their_owner_to_null(flipped_relics: FlippedRelics) -> None:
    """Given a relic mirrored before the class turned protected
    When its row is read after the flip
    Then its _dc_owner is null, as the mirror never rewrites mirrored rows
    """
    table = flipped_relics.mirror.table(flipped_relics.relic_class)

    owner_by_name = by_column(table, "name", "_dc_owner")

    assert owner_by_name["w1-a"] is None


def test_pre_protection_rows_promote_their_read_floor_to_null(flipped_relics: FlippedRelics) -> None:
    """Given a relic mirrored before the class turned protected
    When its row is read after the flip
    Then its _dc_read_floor is null, as the mirror never rewrites mirrored rows
    """
    table = flipped_relics.mirror.table(flipped_relics.relic_class)

    floor_by_name = by_column(table, "name", "_dc_read_floor")

    assert floor_by_name["w1-b"] is None


def test_born_protected_row_carries_its_persisted_owner(flipped_relics: FlippedRelics) -> None:
    """Given a relic born protected under Anna
    When its row is read
    Then its _dc_owner is Anna's uid
    """
    table = flipped_relics.mirror.table(flipped_relics.relic_class)

    owner_by_name = by_column(table, "name", "_dc_owner")

    assert owner_by_name["w2-new"] == ANNA.uid


def test_born_protected_row_carries_its_shared_group(flipped_relics: FlippedRelics) -> None:
    """Given a relic born protected under Anna and shared to the org
    When its row is read
    Then its _dc_groups include the org
    """
    table = flipped_relics.mirror.table(flipped_relics.relic_class)

    groups_by_name = by_column(table, "name", "_dc_groups")

    assert ORG in groups_by_name["w2-new"]


def test_root_rebootstrap_copies_every_relic(
    flipped_relics: FlippedRelics, shop: MirrorShop
) -> None:
    """Given a protected-flipped store with three relics
    When a root re-bootstrap rebuilds the mirror
    Then all three relics are in the Relic table
    """
    with flipped_relics.store.snapshot(principal=dc.root_principal(1)) as root_snap:
        rebuilt = shop.bootstrap("relic.rebuilt", root_snap)

    assert rebuilt.table(flipped_relics.relic_class).num_rows == 3


def test_root_rebootstrap_fills_the_legacy_owner_with_nobody(
    flipped_relics: FlippedRelics, shop: MirrorShop
) -> None:
    """Given a relic from before the class turned protected
    When a root re-bootstrap decodes it (R7 legacy fill)
    Then its _dc_owner is nobody, 0
    """
    with flipped_relics.store.snapshot(principal=dc.root_principal(1)) as root_snap:
        rebuilt = shop.bootstrap("relic.rebuilt", root_snap)
    relic_table = rebuilt.table(flipped_relics.relic_class)
    owner_by_name = by_column(relic_table, "name", "_dc_owner")

    assert owner_by_name["w1-a"] == 0


def test_root_rebootstrap_fills_the_legacy_groups_with_world(
    flipped_relics: FlippedRelics, shop: MirrorShop
) -> None:
    """Given a relic from before the class turned protected
    When a root re-bootstrap decodes it (R7 legacy fill)
    Then it reads as before, in the WORLD group
    """
    with flipped_relics.store.snapshot(principal=dc.root_principal(1)) as root_snap:
        rebuilt = shop.bootstrap("relic.rebuilt", root_snap)
    relic_table = rebuilt.table(flipped_relics.relic_class)
    groups_by_name = by_column(relic_table, "name", "_dc_groups")

    assert list(groups_by_name["w1-a"]) == [dc.WORLD]


def test_root_rebootstrap_fills_the_legacy_read_floor_with_viewer(
    flipped_relics: FlippedRelics, shop: MirrorShop
) -> None:
    """Given a relic from before the class turned protected
    When a root re-bootstrap decodes it (R7 legacy fill)
    Then its read floor is viewer
    """
    with flipped_relics.store.snapshot(principal=dc.root_principal(1)) as root_snap:
        rebuilt = shop.bootstrap("relic.rebuilt", root_snap)
    relic_table = rebuilt.table(flipped_relics.relic_class)
    read_floor_by_name = by_column(relic_table, "name", "_dc_read_floor")

    assert read_floor_by_name["w1-a"] == dc.VIEWER


def test_root_rebootstrap_fences_the_legacy_write_floor_at_the_top(
    flipped_relics: FlippedRelics, shop: MirrorShop
) -> None:
    """Given a relic from before the class turned protected
    When a root re-bootstrap decodes it (R7 legacy fill)
    Then its write floor is admin
    """
    with flipped_relics.store.snapshot(principal=dc.root_principal(1)) as root_snap:
        rebuilt = shop.bootstrap("relic.rebuilt", root_snap)
    relic_table = rebuilt.table(flipped_relics.relic_class)
    write_floor_by_name = by_column(relic_table, "name", "_dc_write_floor")

    assert write_floor_by_name["w1-a"] == dc.ADMIN


def test_root_rebootstrap_keeps_the_persisted_stamp_of_the_born_protected_relic(
    flipped_relics: FlippedRelics, shop: MirrorShop
) -> None:
    """Given a relic born protected under Anna
    When a root re-bootstrap decodes it
    Then its _dc_owner is still Anna's uid
    """
    with flipped_relics.store.snapshot(principal=dc.root_principal(1)) as root_snap:
        rebuilt = shop.bootstrap("relic.rebuilt", root_snap)
    relic_table = rebuilt.table(flipped_relics.relic_class)
    owner_by_name = by_column(relic_table, "name", "_dc_owner")

    assert owner_by_name["w2-new"] == ANNA.uid


def test_non_root_bootstrap_over_a_protected_class_fails_closed(
    flipped_relics: FlippedRelics, shop: MirrorShop, tmp_path: Path
) -> None:
    """Given a class that is now protected
    When Anna bootstraps a mirror from her non-root snapshot
    Then it is denied, as mirroring protected data demands the audited root
    """
    with flipped_relics.store.snapshot(principal=ANNA) as anna_snap, pytest.raises(
        dc.ReadDeniedError
    ):
        shop.bootstrap("relic.denied", anna_snap)


def test_denied_bootstrap_writes_nothing(
    flipped_relics: FlippedRelics, shop: MirrorShop, tmp_path: Path
) -> None:
    """Given a class that is now protected
    When Anna's non-root bootstrap is denied
    Then nothing is wiped or written at the destination
    """
    with flipped_relics.store.snapshot(principal=ANNA) as anna_snap, pytest.raises(
        dc.ReadDeniedError
    ):
        shop.bootstrap("relic.denied", anna_snap)

    assert not (tmp_path / "relic.denied").exists()


# -- compaction + fold ---------------------------------------------------------------


@pytest.fixture
def ten_commit_mirror(store: dc.Store, shop: MirrorShop) -> ArrowMirror:
    """Commit ten quarries one per commit into a mirror that keeps at most four segments."""
    mirror = shop.open(max_segments=4)
    store.attach(mirror)
    quarries: list[Quarry] = []
    for i in range(10):
        quarries.append(Quarry(qid=f"Q{i}", name=f"site {i}"))
        store.root = list(quarries)
        store.commit()
    return mirror


def test_ten_commits_leave_ten_quarry_rows(ten_commit_mirror: ArrowMirror) -> None:
    """Given ten commits of one quarry each
    When the Quarry table is read
    Then it has ten rows
    """
    before = rows(ten_commit_mirror, Quarry)

    assert len(before) == 10


def test_auto_compaction_bounds_the_segments(ten_commit_mirror: ArrowMirror) -> None:
    """Given ten commits into a mirror with max_segments=4
    When its Quarry segments are counted
    Then there are at most four
    """
    segments = list(ten_commit_mirror.parquet_dir(Quarry).glob("seg-*.parquet"))

    assert len(segments) <= 4


def test_compact_preserves_the_content(ten_commit_mirror: ArrowMirror) -> None:
    """Given ten quarry rows in a mirror with max_segments=4
    When the mirror is compacted
    Then the Quarry rows are unchanged
    """
    before = rows(ten_commit_mirror, Quarry)

    ten_commit_mirror.compact()

    assert rows(ten_commit_mirror, Quarry) == before


def test_compact_leaves_one_plain_parquet_file(ten_commit_mirror: ArrowMirror) -> None:
    """Given ten commits into a mirror with max_segments=4
    When the mirror is compacted
    Then the Quarry data dir holds one plain parquet file
    """
    ten_commit_mirror.compact()

    files = list(ten_commit_mirror.parquet_dir(Quarry).glob("seg-*.parquet"))

    assert len(files) == 1


def test_compacted_parquet_file_holds_the_current_state_only(ten_commit_mirror: ArrowMirror) -> None:
    """Given ten quarry rows in a mirror with max_segments=4
    When the mirror is compacted and its one parquet file is read
    Then it holds ten rows
    """
    ten_commit_mirror.compact()
    [parquet_file] = ten_commit_mirror.parquet_dir(Quarry).glob("seg-*.parquet")

    table = pq.read_table(parquet_file)

    assert table.num_rows == 10


@pytest.fixture
def batched_mirror(store: dc.Store, shop: MirrorShop) -> ArrowMirror:
    """Commit two quarries into a mirror that flushes only every tenth commit."""
    mirror = shop.open("batched.mirror", flush_every=10)
    store.attach(mirror)
    store.root = [Quarry(qid="Q1", name="one")]
    store.commit()
    store.root = [*store.root, Quarry(qid="Q2", name="two")]
    store.commit()
    return mirror


def test_batched_mirror_advances_the_live_watermark(batched_mirror: ArrowMirror) -> None:
    """Given a mirror with flush_every=10 after two commits
    When its watermark is read
    Then it is 2
    """
    assert batched_mirror.watermark == 2


def test_batched_mirror_shows_its_unflushed_rows(batched_mirror: ArrowMirror) -> None:
    """Given a mirror with flush_every=10 after two commits
    When the Quarry table is read
    Then the unflushed rows are visible
    """
    assert batched_mirror.table(Quarry).num_rows == 2


def test_flush_makes_the_batched_commits_durable(batched_mirror: ArrowMirror, shop: MirrorShop) -> None:
    """Given a mirror with flush_every=10 after two commits
    When it is flushed and reopened
    Then the reopened watermark is 2
    """
    batched_mirror.flush()

    reopened = shop.open("batched.mirror", flush_every=10)

    assert reopened.watermark == 2


# -- configuration honesty -------------------------------------------------------------


@pytest.fixture
def only_quarry_mirror(store: dc.Store, shop: MirrorShop) -> ArrowMirror:
    """Attach a mirror restricted to Quarry to the store."""
    mirror = shop.open("only.mirror", only=[Quarry])
    store.attach(mirror)
    return mirror


def test_only_filter_mirrors_the_listed_class(
    store: dc.Store, only_quarry_mirror: ArrowMirror
) -> None:
    """Given a mirror restricted to Quarry
    When a quarry and a find are committed
    Then the Quarry table has one row
    """
    store.root = [Quarry(qid="Q1", name="Tsumeb"), Find(label="azurite")]
    store.commit()

    assert only_quarry_mirror.table(Quarry).num_rows == 1


def test_only_filter_leaves_other_classes_out(
    store: dc.Store, only_quarry_mirror: ArrowMirror
) -> None:
    """Given a mirror restricted to Quarry
    When a quarry and a find are committed
    Then the Find table has no rows
    """
    store.root = [Quarry(qid="Q1", name="Tsumeb"), Find(label="azurite")]
    store.commit()

    assert only_quarry_mirror.table(Find).num_rows == 0


def test_only_drift_is_refused_on_reopen(
    store: dc.Store, only_quarry_mirror: ArrowMirror, shop: MirrorShop
) -> None:
    """Given a mirror created restricted to Quarry
    When it is reopened without the filter
    Then the drift is refused: rebuild, do not guess
    """
    store.detach(only_quarry_mirror)
    only_quarry_mirror.close()

    with pytest.raises(MirrorConfigError):
        shop.open("only.mirror")


@pytest.fixture
def mirror_with_orphan_segment(store: dc.Store, shop: MirrorShop) -> Path:
    """Close a one-quarry mirror and plant a segment its manifest never named."""
    mirror = shop.open("crash.mirror")
    store.attach(mirror)
    store.root = [Quarry(qid="Q1", name="Tsumeb")]
    store.commit()
    store.detach(mirror)
    mirror.close()
    segment_dir = mirror.parquet_dir(Quarry)
    orphan = segment_dir / "seg-000002.parquet"
    orphan.write_bytes((segment_dir / "seg-000001.parquet").read_bytes())
    return orphan


def test_crash_debris_segment_is_swept_on_open(
    mirror_with_orphan_segment: Path, shop: MirrorShop
) -> None:
    """Given a segment without a manifest entry, debris from a crash mid-flush
    When the mirror is reopened
    Then the debris is swept
    """
    shop.open("crash.mirror")

    assert not mirror_with_orphan_segment.exists()


def test_reopen_after_a_crash_resumes_from_the_durable_watermark(
    mirror_with_orphan_segment: Path, shop: MirrorShop
) -> None:
    """Given a segment without a manifest entry, debris from a crash mid-flush
    When the mirror is reopened
    Then its watermark is the durable 1
    """
    reopened = shop.open("crash.mirror")

    assert reopened.watermark == 1


def test_reopen_after_a_crash_keeps_the_durable_rows(
    mirror_with_orphan_segment: Path, shop: MirrorShop
) -> None:
    """Given a segment without a manifest entry, debris from a crash mid-flush
    When the mirror is reopened
    Then the Quarry table has its one durable row
    """
    reopened = shop.open("crash.mirror")

    assert reopened.table(Quarry).num_rows == 1


@pytest.fixture
def indexed_corpus(
    shop: MirrorShop, tmp_path: Path
) -> Iterator[Callable[[int], tuple[dc.Store, ArrowMirror]]]:
    """Return a factory for a committed, mirrored corpus; close the stores after."""
    stores: list[dc.Store] = []

    def build(size: int) -> tuple[dc.Store, ArrowMirror]:
        corpus_store = dc.Store.open(tmp_path / f"corpus-{size}", durability="never")
        stores.append(corpus_store)
        mirror = shop.open()
        corpus_store.attach(mirror)
        corpus_store.root = [Quarry(qid=f"Q{size}-{i}", name=f"s{i}") for i in range(size)]
        corpus_store.commit()
        return corpus_store, mirror

    yield build
    for corpus_store in stores:
        corpus_store.close()


def test_apply_cost_does_not_grow_with_the_corpus(
    indexed_corpus: Callable[[int], tuple[dc.Store, ArrowMirror]],
) -> None:
    """Given two independent stores mirroring 10 and 100 quarries (fitness #9)
    When one fixed-size quarry is committed into each
    Then the same number of rows is flushed for both
    """
    small_store, small_mirror = indexed_corpus(10)
    large_store, large_mirror = indexed_corpus(100)
    small_store.root = [*small_store.root, Quarry(qid="Qx-10", name="probe")]
    large_store.root = [*large_store.root, Quarry(qid="Qx-100", name="probe")]

    small_store.commit()
    large_store.commit()

    assert large_mirror.rows_flushed == small_mirror.rows_flushed


@pytest.fixture
def mirrored_hoard(store: dc.Store, mirror: ArrowMirror) -> ArrowMirror:
    """Commit a find whose tags are a list of two quarry references."""
    a = Quarry(qid="QA", name="a")
    b = Quarry(qid="QB", name="b")
    store.root = [a, b, Find(label="hoard", tags=[a, b])]
    store.commit()
    return mirror


def test_list_of_refs_mirrors_the_oids_of_its_targets(mirrored_hoard: ArrowMirror) -> None:
    """Given a find whose tags list refers to two quarries
    When the Find row is read
    Then the tags are the OIDs of the two Quarry rows
    """
    row = rows(mirrored_hoard, Find)[0]

    assert set(row["tags"]) == {quarry["__oid__"] for quarry in rows(mirrored_hoard, Quarry)}


def test_list_of_refs_mirrors_as_a_list_of_int64(mirrored_hoard: ArrowMirror) -> None:
    """Given a find whose tags list refers to two quarries
    When the Find table schema is read
    Then tags is list<int64>
    """
    field = mirrored_hoard.table(Find).schema.field("tags")

    assert field.type == pa.list_(pa.int64())


def test_decode_fallback_restores_the_oid_of_a_referenced_entity(
    store: dc.Store, mirror: ArrowMirror
) -> None:
    """Given a find whose dict tags hold a date, a mixed list and a reference to a quarry
    When the Find row's tags are decoded with decode_fallback
    Then the date and the list are restored and the reference carries the quarry's OID
    """
    quarry = Quarry(qid="Q1", name="Tsumeb")
    quarry_oid = store.store(quarry)
    store.root = [Find(label="x", tags={"ref": quarry, "when": dt.date(2026, 1, 1), "xs": [1, "two"]})]
    store.commit()

    decoded = decode_fallback(rows(mirror, Find)[0]["tags"])

    assert decoded["ref"].oid == quarry_oid
    assert decoded["when"] == dt.date(2026, 1, 1)
    assert decoded["xs"] == [1, "two"]


# -- #16: streaming bootstrap (larger-than-RAM) -----------------------------------


@pytest.fixture
def commit_finds(store: dc.Store) -> Callable[[int], dc.Store]:
    """Return a function that commits n finds into the store and returns the store."""

    def commit(count: int) -> dc.Store:
        store.root = [Find(label=f"f{i}", mass_g=float(i)) for i in range(count)]
        store.commit()
        return store

    return commit


def test_streamed_bootstrap_caches_nothing(
    commit_finds: Callable[[int], dc.Store], shop: MirrorShop
) -> None:
    """Given a store of 50 finds
    When a mirror is bootstrapped with batch=10 from the cache-bypassing stream
    Then the snapshot's cache stays empty
    """
    store = commit_finds(50)

    with store.snapshot() as snap:
        shop.bootstrap("boot", snap, batch=10)
        cached = len(snap._core.cache)

    assert cached == 0


def test_streamed_bootstrap_mirrors_every_find(
    commit_finds: Callable[[int], dc.Store], shop: MirrorShop
) -> None:
    """Given a store of 50 finds
    When a mirror is bootstrapped with batch=10
    Then the Find table has 50 rows
    """
    store = commit_finds(50)

    with store.snapshot() as snap:
        streamed = shop.bootstrap("boot", snap, batch=10)

    assert streamed.table(Find).num_rows == 50


def test_streamed_bootstrap_equals_a_single_shot_bootstrap(
    commit_finds: Callable[[int], dc.Store], shop: MirrorShop
) -> None:
    """Given a store of 50 finds
    When one mirror is bootstrapped with batch=10 and one with batch=10000
    Then both Find tables hold the same rows
    """
    store = commit_finds(50)
    with store.snapshot() as snap:
        streamed = shop.bootstrap("boot", snap, batch=10)
    with store.snapshot() as snap2:
        single_shot = shop.bootstrap("ref", snap2, batch=10_000)

    assert rows(streamed, Find) == rows(single_shot, Find)


class FlushSpy:
    """Records the watermark ArrowMirror.flush saw at each call."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.watermarks: list[int] = []


@pytest.fixture
def flush_spy(monkeypatch: pytest.MonkeyPatch) -> FlushSpy:
    """Replace ArrowMirror.flush with a recording wrapper around the real one."""
    spy = FlushSpy()
    real_flush = ArrowMirror.flush

    def recording_flush(self: ArrowMirror) -> None:
        spy.watermarks.append(self.watermark)
        real_flush(self)

    monkeypatch.setattr(ArrowMirror, "flush", recording_flush)
    return spy


def test_bootstrap_flushes_at_least_once(
    commit_finds: Callable[[int], dc.Store], shop: MirrorShop, flush_spy: FlushSpy
) -> None:
    """Given a store of 50 finds
    When a mirror is bootstrapped with batch=10
    Then flush ran
    """
    store = commit_finds(50)

    with store.snapshot() as snap:
        shop.bootstrap("boot", snap, batch=10)

    assert flush_spy.watermarks != []


def test_bootstrap_segments_stay_bounded_by_the_batch(
    commit_finds: Callable[[int], dc.Store], shop: MirrorShop
) -> None:
    """Given a store of 50 finds, where chunking by flush_every=1 would write a segment per row
    When a mirror is bootstrapped with batch=10
    Then no parquet segment holds more than 10 rows, so peak residency is O(batch), not O(extent)
    """
    store = commit_finds(50)

    with store.snapshot() as snap:
        mirror = shop.bootstrap("boot", snap, batch=10)

    segments = mirror.parquet_dir(Find).glob("seg-*.parquet")
    assert max(pq.ParquetFile(segment).metadata.num_rows for segment in segments) <= 10


def test_bootstrap_defers_the_real_watermark_to_the_final_flush(
    commit_finds: Callable[[int], dc.Store], shop: MirrorShop, flush_spy: FlushSpy
) -> None:
    """Given a store of 30 finds
    When a mirror is bootstrapped with batch=10
    Then there are at least two flushes
    And every flush but the last still saw watermark 0, so a mid-stream crash forces a re-bootstrap
    And the last flush saw the snapshot's tid
    And the mirror ends at that tid
    """
    store = commit_finds(30)

    with store.snapshot() as snap:
        tid = snap.tid
        mirror = shop.bootstrap("boot", snap, batch=10)

    assert len(flush_spy.watermarks) >= 2
    assert all(watermark == 0 for watermark in flush_spy.watermarks[:-1]), flush_spy.watermarks
    assert flush_spy.watermarks[-1] == tid
    assert mirror.watermark == tid


def test_bootstrap_default_batch_writes_few_segments(
    commit_finds: Callable[[int], dc.Store], shop: MirrorShop, tmp_path: Path
) -> None:
    """Given a store of 50 finds, where a segment per row would be catastrophically slow at scale
    When a mirror is bootstrapped with all defaults
    Then at most two parquet segments are written
    """
    store = commit_finds(50)

    with store.snapshot() as snap:
        shop.bootstrap("boot", snap)

    segments = list((tmp_path / "boot").rglob("*.parquet"))
    assert len(segments) <= 2, f"default bootstrap wrote {len(segments)} segments for 50 rows"


def test_bootstrap_suppresses_compaction_thrash(
    commit_finds: Callable[[int], dc.Store], shop: MirrorShop
) -> None:
    """Given a store of 40 finds, whose many small batches would compact repeatedly
    When a mirror is bootstrapped with batch=1 and max_segments=4
    Then all 40 one-row segments are still on disk
    """
    store = commit_finds(40)

    with store.snapshot() as snap:
        mirror = shop.bootstrap("boot", snap, batch=1, max_segments=4)

    assert len(list(mirror.parquet_dir(Find).glob("seg-*.parquet"))) == 40


# -- analytics: "filter in datacrystal, aggregate in DuckDB" (#53) ----------------
#
# The documented recipe (GUIDE → Arrow mirrors → "Analytics at scale"): a
# datacrystal-side bitmap query yields OIDs; DuckDB aggregates over the parquet
# mirror restricted to those OIDs via ``ArrowMirror.OID_COLUMN``. These tests run
# that exact path. DuckDB is a dev dependency, not part of the [arrow] extra, so
# each test requests the fixture that importorskips it (so the pyarrow-only tests
# above still run when it is absent).


@pytest.fixture
def duckdb() -> ModuleType:
    """Import duckdb, skipping the test when it is absent."""
    return pytest.importorskip("duckdb", reason="pip install duckdb")


@pytest.fixture
def mirrored_cabinet(store: dc.Store, mirror: ArrowMirror) -> ArrowMirror:
    """Commit seven finds: indexed grades A, B, C, a null grade, a null mass and a mass."""
    store.root = [
        Find(label="azurite", mass_g=412.5, grade="A"),
        Find(label="malachite", mass_g=120.0, grade="A"),
        Find(label="pyrite", mass_g=66.0, grade="B"),
        Find(label="quartz", mass_g=300.0, grade="B"),
        Find(label="fluorite", mass_g=None, grade="B"),  # null measure
        Find(label="opal", mass_g=58.0, grade="C"),
        Find(label="rubble", mass_g=9.0, grade=None),  # null group key
    ]
    store.commit()
    return mirror


def test_groupby_aggregate_over_the_mirror_matches_the_known_totals(
    duckdb: ModuleType, mirrored_cabinet: ArrowMirror
) -> None:
    """Given a cabinet of seven finds with a null measure and a null grade
    When DuckDB aggregates count, total and mean mass per grade over the whole mirror
    Then each grade carries its known figures and the null grade is left out
    """
    finds_tbl = mirrored_cabinet.table(Find)  # noqa: F841 — DuckDB replacement-scans it

    got = duckdb.query(
        "SELECT grade, count(*) AS n, sum(mass_g) AS total, avg(mass_g) AS mean "
        "FROM finds_tbl WHERE grade IS NOT NULL GROUP BY grade ORDER BY grade"
    ).fetchall()

    assert got == [("A", 2, 532.5, 266.25), ("B", 3, 366.0, 183.0), ("C", 1, 58.0, 58.0)]


def test_bitmap_hits_come_from_the_mirrors_watermark(
    store: dc.Store, mirrored_cabinet: ArrowMirror
) -> None:
    """Given a datacrystal bitmap query on the indexed grade
    When its snapshot tid is compared with the mirror's watermark
    Then they are equal
    """
    with store.snapshot() as snap:
        snap.query(dc.fields(Find).grade == "B")  # pyrefly: ignore[bad-argument-type]
        snapshot_tid = snap.tid

    assert snapshot_tid == mirrored_cabinet.watermark


def test_bitmap_filter_restricts_the_handoff_to_the_grade_b_hits(
    store: dc.Store, mirrored_cabinet: ArrowMirror
) -> None:
    """Given a cabinet of seven finds of which three are grade B
    When the bitmap query on grade B yields OIDs
    Then there are three, not the whole extent
    """
    with store.snapshot() as snap:
        views = snap.query(dc.fields(Find).grade == "B")  # pyrefly: ignore[bad-argument-type]
        oids = oids_of(views)

    assert len(oids) == 3


def test_duckdb_sums_the_measure_over_only_the_handed_off_oids(
    duckdb: ModuleType, store: dc.Store, mirrored_cabinet: ArrowMirror
) -> None:
    """Given the OIDs of the grade B finds from a datacrystal bitmap query
    When DuckDB sums the measure over those rows via OID_COLUMN
    Then the total is 366.0
    """
    with store.snapshot() as snap:
        views = snap.query(dc.fields(Find).grade == "B")  # pyrefly: ignore[bad-argument-type]
        grade_b_oids = oids_of(views)
    finds_tbl = mirrored_cabinet.table(Find)  # noqa: F841 — DuckDB replacement-scans it

    (total,) = duckdb.execute(
        f"SELECT sum(mass_g) FROM finds_tbl WHERE {ArrowMirror.OID_COLUMN} IN "
        "(SELECT * FROM UNNEST(?))",
        [grade_b_oids],
    ).fetchone()

    assert total == 366.0


def test_pluck_path_sums_the_same_total_as_the_handoff(
    store: dc.Store, mirrored_cabinet: ArrowMirror
) -> None:
    """Given a cabinet of seven finds of which three are grade B
    When the slow pluck path sums the grade B masses in Python
    Then the total is 366.0, the figure the DuckDB handoff must equal
    """
    masses = store.pluck(dc.fields(Find).grade == "B", "mass_g")  # pyrefly: ignore[bad-argument-type]

    assert sum(mass for mass in masses if mass is not None) == 366.0


@pytest.fixture
def compacted_cabinet(store: dc.Store, mirrored_cabinet: ArrowMirror) -> ArrowMirror:
    """Detach the cabinet mirror and compact it to plain parquet."""
    store.detach(mirrored_cabinet)
    mirrored_cabinet.compact()
    return mirrored_cabinet


def test_compacted_mirror_has_a_parquet_directory(compacted_cabinet: ArrowMirror) -> None:
    """Given a compacted cabinet mirror
    When the Find parquet_dir is asked for
    Then it is a directory
    """
    directory = compacted_cabinet.parquet_dir(Find)

    assert directory.is_dir()


def test_duckdb_reads_the_compacted_directory_as_the_exact_live_set(
    duckdb: ModuleType, compacted_cabinet: ArrowMirror
) -> None:
    """Given a compacted cabinet mirror, one fold-free file with tombstones already dropped
    When DuckDB read_parquet reads the directory off the owner thread
    Then it counts seven finds with a mass total of 965.5
    """
    glob = str(compacted_cabinet.parquet_dir(Find) / "*.parquet").replace("'", "''")

    (count, total) = duckdb.execute(
        f"SELECT count(*), sum(mass_g) FROM read_parquet('{glob}')"
    ).fetchone()

    assert (count, total) == (7, 965.5)


# -- Decimal (#184) ----------------------------------------------------------------


@dc.entity
class Valuation:
    """A specimen valuation with an exact decimal price."""

    specimen: str
    price: Decimal | None = None


@dc.entity
class ValuationLot:
    """A specimen valuation lot with exact decimal parts."""

    specimen: str
    parts: list[Decimal] | None = None


def test_decimal_field_mirrors_as_an_arrow_decimal_column(
    store: dc.Store, mirror: ArrowMirror
) -> None:
    """Given valuations with Decimal prices
    When the Valuation table is read
    Then the price column is an Arrow decimal
    """
    store.root = [Valuation(specimen="a", price=Decimal("72.46"))]
    store.commit()

    table = mirror.table(Valuation)

    assert pa_types.is_decimal(table.schema.field("price").type)


def test_decimal_prices_keep_their_digits_and_nulls(store: dc.Store, mirror: ArrowMirror) -> None:
    """Given valuations of 72.46, -0.30 and no price
    When the Valuation table is read
    Then each specimen carries its exact Decimal or None
    """
    store.root = [
        Valuation(specimen="a", price=Decimal("72.46")),
        Valuation(specimen="b", price=Decimal("-0.30")),
        Valuation(specimen="c", price=None),
    ]
    store.commit()

    price_by_specimen = by_column(mirror.table(Valuation), "specimen", "price")

    assert price_by_specimen == {"a": Decimal("72.46"), "b": Decimal("-0.30"), "c": None}


@pytest.fixture
def widened_price_mirror(store: dc.Store, mirror: ArrowMirror) -> ArrowMirror:
    """Commit a 1.5 price, then a 26-digit price that widens the column; return the mirror."""
    first = Valuation(specimen="a", price=Decimal("1.5"))
    store.root = [first]
    store.commit()
    store.root = [first, Valuation(specimen="b", price=Decimal("12345678901234567890.123456"))]
    store.commit()
    return mirror


def test_widened_decimal_column_keeps_the_early_price(widened_price_mirror: ArrowMirror) -> None:
    """Given a decimal column widened by a later segment
    When the early price is read back
    Then it is still 1.5
    """
    prices = by_column(widened_price_mirror.table(Valuation), "specimen", "price")

    assert prices["a"] == Decimal("1.5")


def test_widened_decimal_column_keeps_every_digit_of_the_wide_price(
    widened_price_mirror: ArrowMirror,
) -> None:
    """Given a decimal column widened by a later segment
    When the wide price is read back
    Then every digit survives
    """
    prices = by_column(widened_price_mirror.table(Valuation), "specimen", "price")

    assert prices["b"] == Decimal("12345678901234567890.123456")


def test_decimal_beyond_arrow_precision_falls_back_to_exact_text(
    store: dc.Store, mirror: ArrowMirror
) -> None:
    """Given a price with 41 significant digits, beyond Arrow's decimal precision
    When the Valuation row is read and its fallback decoded
    Then the exact Decimal is restored
    """
    too_wide = Decimal("1." + "1" * 40)
    store.root = [Valuation(specimen="w", price=too_wide)]
    store.commit()

    row = rows(mirror, Valuation)[0]

    assert decode_fallback(row["price"]) == too_wide


@pytest.fixture
def widened_lot_mirror(store: dc.Store, mirror: ArrowMirror) -> ArrowMirror:
    """Commit a 1.5 part, then a 26-digit part that widens the list column; return the mirror."""
    first = ValuationLot(specimen="a", parts=[Decimal("1.5")])
    store.root = [first]
    store.commit()
    store.root = [
        first,
        ValuationLot(specimen="b", parts=[Decimal("12345678901234567890.123456")]),
    ]
    store.commit()
    return mirror


def test_decimal_list_column_stays_a_list(widened_lot_mirror: ArrowMirror) -> None:
    """Given a decimal list column widened by a later segment
    When its schema is read
    Then parts is a list
    """
    lot_table = widened_lot_mirror.table(ValuationLot)

    assert pa_types.is_list(lot_table.schema.field("parts").type)


def test_decimal_list_column_keeps_decimal_values(widened_lot_mirror: ArrowMirror) -> None:
    """Given a decimal list column widened by a later segment
    When its schema is read
    Then the list values are decimals
    """
    lot_table = widened_lot_mirror.table(ValuationLot)

    assert pa_types.is_decimal(lot_table.schema.field("parts").type.value_type)


def test_widened_decimal_list_keeps_the_early_part(widened_lot_mirror: ArrowMirror) -> None:
    """Given a decimal list column widened by a later segment
    When the early row is read back
    Then its part is still 1.5
    """
    parts_by_specimen = by_column(widened_lot_mirror.table(ValuationLot), "specimen", "parts")

    assert parts_by_specimen["a"] == [Decimal("1.5")]


def test_widened_decimal_list_keeps_every_digit_of_the_wide_part(
    widened_lot_mirror: ArrowMirror,
) -> None:
    """Given a decimal list column widened by a later segment
    When the wide row is read back
    Then every digit survives
    """
    parts_by_specimen = by_column(widened_lot_mirror.table(ValuationLot), "specimen", "parts")

    assert parts_by_specimen["b"] == [Decimal("12345678901234567890.123456")]
