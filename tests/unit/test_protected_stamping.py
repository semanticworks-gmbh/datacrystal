"""W2-2 + W2-3: owner stamping, write-time inheritance, the R7 legacy fill,
and the upsert label shield (ADR-008, epic #168).

The retrofit tests fabricate same-typename classes to simulate "the class
turned protected between runs" (the schema-evolution house pattern); the
per-file pyright relaxations exist only for that.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import TYPES_BY_NAME, type_info
from datacrystal._errors import WriteDeniedError

StoreFactory = Callable[[], dc.Store]

ORG = 1
ANNA = dc.Principal(uid=2, memberships={ORG: dc.CURATOR})
BOB = dc.Principal(uid=3, memberships={ORG: dc.STAFF})
STORE_ADMIN = dc.Principal(uid=9, memberships={dc.WORLD: dc.ADMIN})

REQUIRED = object()


def _vault(*, protected: bool, **fields: tuple[object, object]) -> type:
    """(Re)define the 'Vault' entity with these fields.

    The typename is the same on every call, so persisted records decode through their own
    lineage row (the schema-evolution pattern, here simulating a protected=True retrofit).
    """
    annotations = {}
    namespace: dict = {
        "__module__": __name__,
        "__qualname__": "Vault",
        "__annotations__": annotations,
    }
    for name, (annotation, default) in fields.items():
        annotations[name] = annotation
        if default is not REQUIRED:
            namespace[name] = default
    return dc.entity(protected=protected)(type("Vault", (), namespace))


def _vault_v1() -> type:
    return _vault(
        protected=False,
        name=(Annotated[str, dc.Unique], REQUIRED),
        grade=(Annotated[str, dc.Index], "common"),
    )


def _vault_v2() -> type:
    return _vault(
        protected=True,
        name=(Annotated[str, dc.Unique], REQUIRED),
        grade=(Annotated[str, dc.Index], "common"),
    )


# --- owner stamping + inheritance (W2-2) --------------------------------------


@dc.entity(protected=True)
class Drawer:
    """A protected drawer holding gems by Lazy handles only (R11).

    Gem is protected, so every ref to it — direct, inside a list, or a bare Lazy handle —
    must be Lazy; there is no eager form.
    """

    label: Annotated[str, dc.Unique]
    prize: dc.Lazy[Gem] | None = None
    gems: list[dc.Lazy[Gem]] = dataclasses.field(default_factory=list)
    lazy_gem: dc.Lazy[Gem] | None = None


@dc.entity(protected=True)
class Gem:
    """A protected gem."""

    name: Annotated[str, dc.Unique]


@dc.entity
class PlainShelf:
    """An unprotected shelf; R11 binds unprotected containers too."""

    label: Annotated[str, dc.Unique]
    prize: dc.Lazy[Gem] | None = None


def _labeled_drawer(store: dc.Store, label: str) -> Drawer:
    drawer = Drawer(label=label)
    store.store(drawer)
    drawer._dc_groups.append(ORG)  # pyrefly: ignore[missing-attribute]
    drawer._dc_read_floor = dc.VIEWER  # pyrefly: ignore[missing-attribute]
    drawer._dc_write_floor = dc.STAFF  # pyrefly: ignore[missing-attribute]
    return drawer


def _commit_drawer_holding_three_gems(store: dc.Store) -> None:
    with store.acting_as(ANNA):
        drawer = _labeled_drawer(store, "top")
        drawer.prize = dc.Lazy.of(Gem(name="amethyst"))  # a Lazy scalar field
        drawer.gems.append(dc.Lazy.of(Gem(name="citrine")))  # inside a Lazy list field
        drawer.lazy_gem = dc.Lazy.of(Gem(name="opal"))  # another Lazy scalar field
        store.commit()


def _commit_labeled_drawer_then_anonymous_smuggle(store: dc.Store) -> int | None:
    """Commit an empty shelf as ANNA, then buffer a new protected gem on it anonymously."""
    with store.acting_as(ANNA):
        shelf = PlainShelf(label="s1")
        store.store(shelf)
        tid_before = store.commit()
    shelf.prize = dc.Lazy.of(Gem(name="smuggled"))
    return tid_before


class TestOwnerStamping:
    """A fresh protected child takes its owner from the acting principal."""

    @pytest.mark.parametrize("gem_name", ["amethyst", "citrine", "opal"])
    def test_fresh_child_inherits_container_labels_never_owner(
        self, store: dc.Store, gem_name: str
    ) -> None:
        """Given a labeled drawer committed by Anna with a gem in each Lazy position
        When Anna reads the gem back
        Then its owner is Anna, the acting principal and not the container's
        And its groups are the drawer's
        And its read floor is the drawer's viewer
        And its write floor is the drawer's staff
        """
        _commit_drawer_holding_three_gems(store)
        with store.acting_as(ANNA):
            gem = store.query(dc.fields(Gem).name == gem_name)[0]
        assert gem._dc_owner == 2
        assert list(gem._dc_groups) == [ORG]
        assert gem._dc_read_floor == dc.VIEWER
        assert gem._dc_write_floor == dc.STAFF

    def test_child_without_protected_container_keeps_birth_labels(self, store: dc.Store) -> None:
        """Given an unprotected shelf holding a new gem, committed by Anna
        When Anna reads the gem back
        Then its owner is Anna
        And it has no groups
        And its write floor is viewer
        """
        with store.acting_as(ANNA):
            shelf = PlainShelf(label="loose", prize=dc.Lazy.of(Gem(name="quartzite")))
            store.store(shelf)
            store.commit()
        with store.acting_as(ANNA):
            gem = store.query(dc.fields(Gem).name == "quartzite")[0]
        assert gem._dc_owner == 2
        assert list(gem._dc_groups) == []
        assert gem._dc_write_floor == dc.VIEWER

    def test_first_registration_wins(self, store: dc.Store) -> None:
        """Given a gem registered through a drawer with a staff write floor
        When a second drawer with a curator write floor also links the gem
        Then the gem keeps the first drawer's staff write floor
        """
        with store.acting_as(ANNA):
            first = _labeled_drawer(store, "first")
            second = Drawer(label="second")
            store.store(second)
            second._dc_groups.append(ORG)  # pyrefly: ignore[missing-attribute]
            second._dc_write_floor = dc.CURATOR  # pyrefly: ignore[missing-attribute]
            shared = Gem(name="shared")
            first.prize = dc.Lazy.of(shared)
            second.prize = dc.Lazy.of(shared)
            store.commit()
        with store.acting_as(ANNA):
            gem = store.query(dc.fields(Gem).name == "shared")[0]
        assert gem._dc_write_floor == dc.STAFF

    def test_commit_time_discovery_stamps_the_owner_under_the_commit_principal(
        self, store: dc.Store
    ) -> None:
        """Given a committed labeled drawer and Bob linking a new gem only at commit time
        When Bob commits and reads the gem back
        Then its owner is Bob, the commit-time principal
        And its groups are inherited from the drawer
        """
        with store.acting_as(ANNA):
            drawer = _labeled_drawer(store, "late")
            store.commit()
        with store.acting_as(BOB):
            drawer.prize = dc.Lazy.of(Gem(name="latecomer"))
            store.commit()
        with store.acting_as(BOB):
            gem = store.query(dc.fields(Gem).name == "latecomer")[0]
        assert gem._dc_owner == 3
        assert list(gem._dc_groups) == [ORG]


class TestAnonymousRefusal:
    """An anonymous session cannot create a protected record."""

    def test_store_of_protected_record_refuses(self, store: dc.Store) -> None:
        """Given an anonymous session
        When it stores a protected gem
        Then store raises WriteDeniedError naming anonymous
        """
        with pytest.raises(WriteDeniedError, match="anonymous"):
            store.store(Gem(name="orphan"))

    def test_refused_store_buffers_nothing(self, store: dc.Store) -> None:
        """Given an anonymous store of a protected gem that was refused
        When the session commits
        Then there is nothing to commit
        """
        with pytest.raises(WriteDeniedError):
            store.store(Gem(name="orphan"))
        committed = store.commit()
        assert committed is None

    def test_p1_discovery_refuses_an_anonymous_commit(self, store: dc.Store) -> None:
        """Given a committed shelf with a new protected gem buffered on it anonymously
        When the anonymous session commits
        Then the commit raises WriteDeniedError naming anonymous, before any TID is taken
        """
        _commit_labeled_drawer_then_anonymous_smuggle(store)
        with pytest.raises(WriteDeniedError, match="anonymous"):
            store.commit()

    def test_retry_under_the_owner_stamps_the_smuggled_gem(self, store: dc.Store) -> None:
        """Given a refused anonymous commit whose buffer is intact
        When Anna retries the commit
        Then the smuggled gem is stamped with Anna as owner
        """
        _commit_labeled_drawer_then_anonymous_smuggle(store)
        with pytest.raises(WriteDeniedError):
            store.commit()
        with store.acting_as(ANNA):
            store.commit()
            gem = store.query(dc.fields(Gem).name == "smuggled")[0]
        assert gem._dc_owner == 2

    def test_refused_commit_burns_no_tid(self, store: dc.Store) -> None:
        """Given a refused anonymous commit whose buffer is intact
        When Anna retries the commit
        Then the TID is the one right after the last commit (invariant 5)
        """
        tid_before = _commit_labeled_drawer_then_anonymous_smuggle(store)
        with pytest.raises(WriteDeniedError):
            store.commit()
        with store.acting_as(ANNA):
            tid_after = store.commit()
        assert tid_after == tid_before + 1  # pyrefly: ignore[unsupported-operation]

    def test_unprotected_writes_stay_anonymous_friendly(self, store: dc.Store) -> None:
        """Given an anonymous session
        When it stores an unprotected shelf and commits
        Then the commit returns a TID
        """
        store.store(PlainShelf(label="anon-ok"))
        committed = store.commit()
        assert committed is not None


# --- the R7 legacy fill (retrofit) ---------------------------------------------


@pytest.fixture
def legacy_vault(store_factory: StoreFactory) -> type:
    """Commit one row of the unprotected Vault, then return the protected retrofit class."""
    legacy = _vault_v1()
    store = store_factory()
    store.store(legacy(name="w1-era", grade="fine"))
    store.commit()
    store.close()
    return _vault_v2()


class TestLegacyFill:
    """Rows written before a class turned protected read as readable-as-before, admin-write."""

    def test_live_read_fills_the_legacy_labels(
        self, store_factory: StoreFactory, legacy_vault: type
    ) -> None:
        """Given a W1-era row of a class that is now protected
        When the row is read from a reopened store
        Then its owner is nobody
        And its groups are the world, read as before
        And its read floor is viewer
        And its write floor is admin, writes fenced at the top
        """
        with store_factory() as reopened:
            row = reopened.query(dc.fields(legacy_vault).name == "w1-era")[0]
        assert row._dc_owner == 0
        assert list(row._dc_groups) == [dc.WORLD]
        assert row._dc_read_floor == dc.VIEWER
        assert row._dc_write_floor == dc.ADMIN

    def test_decode_level_pluck_agrees_with_the_fill(
        self, store_factory: StoreFactory, legacy_vault: type
    ) -> None:
        """Given a W1-era row of a class that is now protected
        When the write floor is plucked without constructing entities
        Then it is admin
        """
        with store_factory() as reopened:
            floors = reopened.pluck(legacy_vault, "_dc_write_floor")
        assert floors == [dc.ADMIN]

    def test_verify_finds_no_decode_failure(
        self, store_factory: StoreFactory, legacy_vault: type
    ) -> None:
        """Given a W1-era row of a class that is now protected
        When the reopened store is verified
        Then it reports nothing
        """
        with store_factory() as reopened:
            problems = reopened.verify()
        assert problems == []

    def test_snapshot_fills_identically(
        self, store_factory: StoreFactory, legacy_vault: type
    ) -> None:
        """Given a W1-era row of a class that is now protected
        When a snapshot of the reopened store queries the row
        Then its owner is nobody
        And its groups are the world
        And its write floor is admin, as web readers must see the same labels as live ones
        """
        with store_factory() as reopened:
            [view] = reopened.snapshot().query(dc.fields(legacy_vault).name == "w1-era")
        assert view._dc_owner == 0
        assert tuple(view._dc_groups) == (dc.WORLD,)
        assert view._dc_write_floor == dc.ADMIN

    def test_index_build_plans_the_read_floor_as_an_indexed_lookup(
        self, store_factory: StoreFactory, legacy_vault: type
    ) -> None:
        """Given a W1-era row and a post-flip row with a curator read floor
        When the plan for read floor at most viewer is explained
        Then the plan uses the sorted index
        And it has no residual
        """
        with store_factory() as reopened:
            with reopened.acting_as(ANNA):
                fresh = legacy_vault(name="post-flip")
                reopened.store(fresh)
                fresh._dc_read_floor = dc.CURATOR
                reopened.commit()
            plan = reopened.explain(dc.fields(legacy_vault)._dc_read_floor <= dc.VIEWER)
        assert plan.indexed
        assert plan.residual is None

    def test_index_build_fills_the_sorted_read_floor(
        self, store_factory: StoreFactory, legacy_vault: type
    ) -> None:
        """Given a W1-era row and a post-flip row with a curator read floor
        When read floor at most viewer is queried
        Then only the W1-era row matches
        """
        with store_factory() as reopened:
            with reopened.acting_as(ANNA):
                fresh = legacy_vault(name="post-flip")
                reopened.store(fresh)
                fresh._dc_read_floor = dc.CURATOR
                reopened.commit()
            found = reopened.query(dc.fields(legacy_vault)._dc_read_floor <= dc.VIEWER)
        assert {row.name for row in found} == {"w1-era"}

    def test_fresh_records_never_hit_the_fill(self, store_factory: StoreFactory) -> None:
        """Given a record born protected under Anna and committed
        When Anna reads it from a reopened store
        Then its owner is the persisted stamp, Anna
        And its write floor is the R6 birth value viewer, not R7's admin
        """
        born_protected = _vault_v2()
        with store_factory() as first, first.acting_as(ANNA):
            first.store(born_protected(name="born-protected"))
            first.commit()
        with store_factory() as reopened, reopened.acting_as(ANNA):
            row = reopened.query(dc.fields(born_protected).name == "born-protected")[0]
        assert row._dc_owner == 2
        assert row._dc_write_floor == dc.VIEWER

    def test_migrate_is_denied_to_a_curator_below_the_r7_admin_floor(
        self, store_factory: StoreFactory, legacy_vault: type
    ) -> None:
        """Given a W1-era row whose R7 write floor is admin
        When a curator migrates the store
        Then migrate raises WriteDeniedError, migrate riding the write gate (W2-6)
        """
        with (
            store_factory() as reopened,
            reopened.acting_as(ANNA),
            pytest.raises(dc.WriteDeniedError),
        ):
            reopened.migrate()

    def test_migrate_by_a_store_wide_admin_rewrites_the_legacy_row(
        self, store_factory: StoreFactory, legacy_vault: type
    ) -> None:
        """Given a W1-era row whose R7 write floor is admin
        When a store-wide admin migrates the store
        Then one row is migrated
        """
        with store_factory() as reopened, reopened.acting_as(STORE_ADMIN):
            migrated = reopened.migrate()
        assert migrated == 1

    def test_migrate_materializes_the_admin_write_floor(
        self, store_factory: StoreFactory, legacy_vault: type
    ) -> None:
        """Given a W1-era row migrated by a store-wide admin
        When the row is read from a reopened store
        Then its write floor is admin, which a rewrite through the R6 defaults could not give
        And its groups are the world
        """
        with store_factory() as migrating, migrating.acting_as(STORE_ADMIN):
            migrating.migrate()
        with store_factory() as reopened:
            row = reopened.query(dc.fields(legacy_vault).name == "w1-era")[0]
        assert row._dc_write_floor == dc.ADMIN
        assert list(row._dc_groups) == [dc.WORLD]


def _commit_w1_era_actor(store_factory: StoreFactory) -> None:
    """Persist an Actor row in the W1-era unprotected shape under the shipped typename."""
    typename = "datacrystal._actors:Actor"
    real_info = TYPES_BY_NAME[typename]
    try:
        annotations = {
            "uid": Annotated[int, dc.Unique],
            "subject": Annotated[str, dc.Index],
            "display": str,
            "human": bool,
            "sponsor": int | None,
            "memberships": dict[int, int],
        }
        namespace: dict = {
            "__module__": "datacrystal._actors",
            "__qualname__": "Actor",
            "__annotations__": annotations,
            "subject": "",
            "display": "",
            "human": False,
            "sponsor": None,
            "memberships": dataclasses.field(default_factory=dict[int, int]),
        }
        w1_actor = dc.entity(type("Actor", (), namespace))
        store = store_factory()
        store.store(w1_actor(uid=2, display="Anna", human=True, memberships={ORG: dc.CURATOR}))
        store.commit()
        store.close()
    finally:
        TYPES_BY_NAME[typename] = real_info


class TestActorFlip:
    """Actor rows written before Actor turned protected still decode and resolve."""

    def test_actor_class_is_protected(self) -> None:
        """Given the shipped Actor class
        When its type info is read
        Then it is marked protected
        """
        info = type_info(dc.Actor)
        assert info.protected is True

    def test_w1_era_actor_row_decodes_with_the_admin_write_floor(
        self, store_factory: StoreFactory
    ) -> None:
        """Given an Actor row persisted anonymously in the W1-era unprotected shape
        When the row is read from a reopened store
        Then its write floor is admin, R7 fencing it at the top
        """
        _commit_w1_era_actor(store_factory)
        with store_factory() as reopened:
            anna = reopened.query(dc.fields(dc.Actor).uid == 2)[0]
        assert anna._dc_write_floor == dc.ADMIN

    def test_w1_era_actor_row_stays_readable_as_before(self, store_factory: StoreFactory) -> None:
        """Given an Actor row persisted anonymously in the W1-era unprotected shape
        When the row is read from a reopened store
        Then its groups are the world
        """
        _commit_w1_era_actor(store_factory)
        with store_factory() as reopened:
            anna = reopened.query(dc.fields(dc.Actor).uid == 2)[0]
        assert list(anna._dc_groups) == [dc.WORLD]

    def test_acting_as_still_resolves_the_w1_era_actor(self, store_factory: StoreFactory) -> None:
        """Given an Actor row persisted anonymously in the W1-era unprotected shape
        When a reopened store is acted on by the plain uid 2
        Then the resolved principal has uid 2
        And it carries Anna's memberships
        """
        _commit_w1_era_actor(store_factory)
        with store_factory() as reopened, reopened.acting_as(2):
            principal = reopened.principal
        assert principal.uid == 2
        assert principal.memberships == {ORG: dc.CURATOR}


# --- the upsert label shield (W2-3) ---------------------------------------------


class TestUpsertShield:
    """upsert merges data fields and never the labels."""

    def test_merge_returns_the_committed_survivor(self, store: dc.Store) -> None:
        """Given Anna's committed gem with a curated group and a curator write floor
        When Anna upserts a fresh probe with the same name and birth defaults
        Then the survivor is the committed gem
        """
        with store.acting_as(ANNA):
            gem = Gem(name="tourmaline")
            store.store(gem)
            gem._dc_groups.append(ORG)  # pyrefly: ignore[missing-attribute]
            gem._dc_write_floor = dc.CURATOR  # pyrefly: ignore[missing-attribute]
            store.commit()
            survivor = store.upsert(Gem(name="tourmaline"))
        assert survivor is gem

    def test_merge_does_not_reset_the_owner_of_a_committed_survivor(self, store: dc.Store) -> None:
        """Given Anna's committed gem with a curated group and a curator write floor
        When Anna upserts a fresh probe with the same name and birth defaults
        Then the survivor's owner is still Anna, not reset to the probe's nobody
        """
        with store.acting_as(ANNA):
            gem = Gem(name="tourmaline")
            store.store(gem)
            store.commit()
            survivor = store.upsert(Gem(name="tourmaline"))
        assert survivor._dc_owner == 2

    def test_merge_does_not_reset_the_write_floor_of_a_committed_survivor(
        self, store: dc.Store
    ) -> None:
        """Given Anna's committed gem with a curated group and a curator write floor
        When Anna upserts a fresh probe with the same name and birth defaults
        Then the survivor's write floor is still curator, not reset to viewer
        """
        with store.acting_as(ANNA):
            gem = Gem(name="tourmaline")
            store.store(gem)
            gem._dc_write_floor = dc.CURATOR  # pyrefly: ignore[missing-attribute]
            store.commit()
            survivor = store.upsert(Gem(name="tourmaline"))
        assert survivor._dc_write_floor == dc.CURATOR

    def test_merge_does_not_reset_the_groups_of_a_committed_survivor(self, store: dc.Store) -> None:
        """Given Anna's committed gem with a curated group and a curator write floor
        When Anna upserts a fresh probe with the same name and birth defaults
        Then the survivor's groups are still the org
        """
        with store.acting_as(ANNA):
            gem = Gem(name="tourmaline")
            store.store(gem)
            gem._dc_groups.append(ORG)  # pyrefly: ignore[missing-attribute]
            store.commit()
            survivor = store.upsert(Gem(name="tourmaline"))
        assert list(survivor._dc_groups) == [ORG]

    def test_same_batch_reupsert_returns_the_pending_survivor(self, store: dc.Store) -> None:
        """Given a gem upserted by Anna and still pending
        When Anna upserts the same name again in the same batch
        Then the second survivor is the first
        """
        with store.acting_as(ANNA):
            first = store.upsert(Gem(name="zircon"))
            second = store.upsert(Gem(name="zircon"))
        assert second is first

    def test_same_batch_pending_survivor_keeps_its_stamp(self, store: dc.Store) -> None:
        """Given a gem upserted by Anna and still pending
        When Anna upserts the same name again in the same batch
        Then the gem's owner is still Anna, the probe's defaults never copied
        """
        with store.acting_as(ANNA):
            first = store.upsert(Gem(name="zircon"))
            store.upsert(Gem(name="zircon"))
        assert first._dc_owner == 2

    def test_same_batch_pending_survivor_commits_with_its_stamp(self, store: dc.Store) -> None:
        """Given a gem upserted twice by Anna in one batch
        When Anna commits and reads the gem back
        Then its owner is Anna
        """
        with store.acting_as(ANNA):
            store.upsert(Gem(name="zircon"))
            store.upsert(Gem(name="zircon"))
            store.commit()
            gem = store.query(dc.fields(Gem).name == "zircon")[0]
        assert gem._dc_owner == 2

    def test_data_fields_still_merge(self, store: dc.Store) -> None:
        """Given a committed labeled drawer
        When Anna upserts a probe with the same label and a new prize
        Then the committed drawer holds the new prize
        """
        with store.acting_as(ANNA):
            drawer = _labeled_drawer(store, "merge-me")
            store.commit()
            probe = Drawer(label="merge-me")
            probe.prize = dc.Lazy.of(Gem(name="new-prize"))
            store.upsert(probe)
            store.commit()
            merged_prize = drawer.prize.get().name  # pyrefly: ignore[missing-attribute]
        assert merged_prize == "new-prize"

    def test_merge_keeps_the_survivors_labels_when_data_merges(self, store: dc.Store) -> None:
        """Given a committed labeled drawer with a staff write floor
        When Anna upserts a probe with the same label and a new prize
        Then the drawer's write floor is still staff
        """
        with store.acting_as(ANNA):
            drawer = _labeled_drawer(store, "merge-me")
            store.commit()
            probe = Drawer(label="merge-me")
            probe.prize = dc.Lazy.of(Gem(name="new-prize"))
            store.upsert(probe)
            store.commit()
        assert drawer._dc_write_floor == dc.STAFF  # pyrefly: ignore[missing-attribute]

    def test_unchanged_reimport_buffers_nothing(self, store: dc.Store) -> None:
        """Given Anna's committed gem
        When Anna upserts an identical gem with default labels
        Then there is nothing to commit, the label skip keeping it a no-op
        """
        with store.acting_as(ANNA):
            store.upsert(Gem(name="idempotent"))
            store.commit()
            store.upsert(Gem(name="idempotent"))
            committed = store.commit()
        assert committed is None

    def test_unprotected_class_merges_every_field(self) -> None:
        """Given an unprotected class
        When its type info is read
        Then its data field names are its field names, zero-cost and structurally
        """
        info = type_info(PlainShelf)
        assert info.data_field_names is info.field_names

    def test_protected_class_merges_only_its_data_fields(self) -> None:
        """Given a protected class
        When its type info is read
        Then its data field names exclude the label columns
        """
        info = type_info(Gem)
        assert info.data_field_names == ("name",)


# --- index cache x retrofit (ADR-005: rebuild on marker mismatch) ---------------


def _commit_cached_legacy_vault(path: Path) -> type:
    """Commit one unprotected Vault with a warm index cache, then return the retrofit class."""
    legacy = _vault_v1()
    with dc.Store.open(path, cache_index=True) as store:
        store.store(legacy(name="cached-era", grade="fine"))
        store.commit()
        store.query(dc.fields(legacy).grade == "fine")
    return _vault_v2()


def test_retrofit_invalidates_the_index_cache_for_the_read_floor_plan(tmp_path: Path) -> None:
    """Given an index cache written while the class was unprotected, the class now protected
    When the plan for read floor at most viewer is explained on the reopened store
    Then the plan uses the sorted index with no residual, the cache rebuilt on the marker change
    """
    retrofit = _commit_cached_legacy_vault(tmp_path / "store")
    with dc.Store.open(tmp_path / "store", cache_index=True) as reopened:
        plan = reopened.explain(dc.fields(retrofit)._dc_read_floor <= dc.VIEWER)
    assert plan.indexed
    assert plan.residual is None


def test_retrofit_read_floor_query_finds_the_legacy_row(tmp_path: Path) -> None:
    """Given an index cache written while the class was unprotected, the class now protected
    When read floor at most viewer is queried on the reopened store
    Then the legacy row is found
    """
    retrofit = _commit_cached_legacy_vault(tmp_path / "store")
    with dc.Store.open(tmp_path / "store", cache_index=True) as reopened:
        found = reopened.query(dc.fields(retrofit)._dc_read_floor <= dc.VIEWER)
    assert {row.name for row in found} == {"cached-era"}


def test_retrofit_keeps_the_existing_grade_index_working(tmp_path: Path) -> None:
    """Given an index cache written while the class was unprotected, the class now protected
    When the grade index is queried on the reopened store
    Then the legacy row is found
    """
    retrofit = _commit_cached_legacy_vault(tmp_path / "store")
    with dc.Store.open(tmp_path / "store", cache_index=True) as reopened:
        found = reopened.query(dc.fields(retrofit).grade == "fine")
    assert {row.name for row in found} == {"cached-era"}


def test_anonymous_dirty_write_is_now_fenced(store: dc.Store) -> None:
    """Given Anna's committed gem
    When an anonymous session renames it and commits
    Then the commit raises WriteDeniedError, the W2-5 gate binding every write path
    """
    with store.acting_as(ANNA):
        gem = Gem(name="fenced")
        store.store(gem)
        store.commit()
    gem.name = "fenced-edited"
    with pytest.raises(dc.WriteDeniedError):
        store.commit()
    store.discard()
