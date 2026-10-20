"""dc.Principal, dc.Actor, and the ladder constants (epic #168, W1 — #171).

W1 shipped identity as pure data; W2 (ADR-008) flips Actor to protected, so
registering actors now needs a non-anonymous session — the config-trusted
bootstrap principal below is the documented recipe ("authenticate outside").
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from itertools import pairwise

import pytest

import datacrystal as dc

# The bootstrap identity: whoever opens the store registers the first actors
# (ADR-008 R6 — the anonymous principal cannot create protected records).
# Since W5 (F1) minting an Actor is bounded by the registrar's own authority
# (memberships ARE authority), so the config-trusted bootstrap is ROOT — the
# "whoever syncs the registry" app-side trust of ADR-008:200.
BOOT = dc.root_principal(1)


@dc.entity
class Actor:
    """A user's own domain class named Actor, unrelated to the shipped registry entity."""

    name: str = ""


@pytest.fixture
def registered_actors(store_factory: Callable[[], dc.Store]) -> Iterator[dc.Store]:
    """Yield a reopened store whose registry Anna (a human) and a sponsored swarm were committed."""
    first = store_factory()
    with first.acting_as(BOOT):
        first.store(
            dc.Actor(
                uid=2,
                subject="oidc|anna",
                display="Anna",
                human=True,
                memberships={1: dc.STAFF, 2: dc.CURATOR},
            )
        )
        first.store(
            dc.Actor(
                uid=900,
                display="parser swarm",
                human=False,
                sponsor=2,
                memberships={2: dc.AGENT},
            )
        )
        first.commit()
    first.close()
    reopened = store_factory()
    yield reopened
    reopened.close()


class TestLadder:
    """The ladder constants and what their order means."""

    def test_levels_ascend_from_no_standing_to_executive(self) -> None:
        """Given the ladder constants
        When they are compared in ladder order
        Then each level is above the one before
        """
        assert (
            dc.NO_STANDING
            < dc.VIEWER
            < dc.AGENT
            < dc.AUTOMATION
            < dc.STAFF
            < dc.CURATOR
            < dc.ADMIN
            < dc.EXECUTIVE
        )

    def test_spacing_allows_insertions(self) -> None:
        """Given the seven grantable levels
        When the gaps between neighbours are measured
        Then every gap is 100
        """
        ladder = [dc.VIEWER, dc.AGENT, dc.AUTOMATION, dc.STAFF, dc.CURATOR, dc.ADMIN, dc.EXECUTIVE]

        assert all(b - a == 100 for a, b in pairwise(ladder))

    def test_world_group_is_zero(self) -> None:
        """Given the WORLD group constant
        When its value is read
        Then it is 0
        """
        assert dc.WORLD == 0

    def test_no_standing_is_minus_one(self) -> None:
        """Given the NO_STANDING constant, the absence of standing and not a grantable level
        When its value is read
        Then it is -1
        """
        assert dc.NO_STANDING == -1


class TestPrincipal:
    """Principal is pure, frozen data."""

    def test_assigning_a_field_is_refused(self) -> None:
        """Given a principal with uid 7
        When its uid is assigned
        Then the assignment raises a FrozenInstanceError
        """
        p = dc.Principal(uid=7, memberships={1: dc.CURATOR})

        with pytest.raises(dataclasses.FrozenInstanceError):
            p.uid = 8  # pyrefly: ignore[read-only]

    def test_assignment_leaves_the_uid_unchanged(self) -> None:
        """Given a principal with uid 7 whose uid assignment was refused
        When its uid is read
        Then it is still 7
        """
        p = dc.Principal(uid=7, memberships={1: dc.CURATOR})
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.uid = 8  # pyrefly: ignore[read-only]

        assert p.uid == 7

    def test_default_memberships_are_empty(self) -> None:
        """Given a principal built from a uid alone
        When its memberships are read
        Then there are none
        """
        anonymous = dc.Principal(uid=0)

        assert anonymous.memberships == {}

    def test_equal_data_means_equal_principals(self) -> None:
        """Given two principals with the same uid and memberships
        When they are compared
        Then they are equal
        """
        assert dc.Principal(2, {1: dc.STAFF}) == dc.Principal(2, {1: dc.STAFF})

    def test_different_memberships_mean_different_principals(self) -> None:
        """Given two principals with the same uid but different membership levels
        When they are compared
        Then they differ
        """
        assert dc.Principal(2, {1: dc.STAFF}) != dc.Principal(2, {1: dc.CURATOR})


class TestActorRegistry:
    """Actor rows are ordinary protected rows of the registry."""

    def test_human_actor_roundtrips_as_human(self, registered_actors: dc.Store) -> None:
        """Given Anna committed as a human actor and the store reopened
        When she is fetched by uid
        Then she is a human
        """
        with registered_actors.acting_as(BOOT):  # BOOT owns these rows (ADR-008 read fence)
            anna = registered_actors.get(dc.Actor, uid=2)

        assert anna is not None
        assert anna.human is True

    def test_actor_display_roundtrips(self, registered_actors: dc.Store) -> None:
        """Given Anna committed as an actor and the store reopened
        When she is fetched by uid
        Then her display name is Anna
        """
        with registered_actors.acting_as(BOOT):
            anna = registered_actors.get(dc.Actor, uid=2)

        assert anna is not None
        assert anna.display == "Anna"

    def test_actor_memberships_roundtrip(self, registered_actors: dc.Store) -> None:
        """Given Anna committed with two memberships and the store reopened
        When she is fetched by uid
        Then both memberships are there
        """
        with registered_actors.acting_as(BOOT):
            anna = registered_actors.get(dc.Actor, uid=2)

        assert anna is not None
        assert anna.memberships == {1: dc.STAFF, 2: dc.CURATOR}

    def test_technical_actor_roundtrips_as_not_human(self, registered_actors: dc.Store) -> None:
        """Given a swarm committed as a technical actor and the store reopened
        When it is fetched by uid
        Then it is not a human
        """
        with registered_actors.acting_as(BOOT):
            swarm = registered_actors.get(dc.Actor, uid=900)

        assert swarm is not None
        assert swarm.human is False

    def test_technical_actor_sponsor_roundtrips(self, registered_actors: dc.Store) -> None:
        """Given a swarm committed with Anna as sponsor and the store reopened
        When it is fetched by uid
        Then its sponsor is uid 2
        """
        with registered_actors.acting_as(BOOT):
            swarm = registered_actors.get(dc.Actor, uid=900)

        assert swarm is not None
        assert swarm.sponsor == 2

    def test_a_second_actor_with_the_same_uid_is_refused(self, store: dc.Store) -> None:
        """Given Anna and an impostor staged with the same uid
        When the store commits
        Then a UniqueViolationError is raised
        """
        with store.acting_as(BOOT):
            store.store(dc.Actor(uid=2, display="Anna", human=True))
            store.store(dc.Actor(uid=2, display="impostor", human=True))

            with pytest.raises(dc.UniqueViolationError):
                store.commit()

    def test_membership_change_survives_a_reopen(
        self, store_factory: Callable[[], dc.Store]
    ) -> None:
        """Given Anna committed with one membership
        When a second membership is added in place and committed
        Then the reopened store shows both
        """
        store = store_factory()
        with store.acting_as(BOOT):
            store.store(dc.Actor(uid=2, display="Anna", human=True, memberships={1: dc.STAFF}))
            store.commit()
            anna = store.get(dc.Actor, uid=2)
            assert anna is not None
            anna.memberships[2] = dc.CURATOR  # in-place container mutation marks dirty
            store.commit()
        store.close()

        reopened = store_factory()
        with reopened.acting_as(BOOT):  # BOOT owns this row (ADR-008 read fence)
            again = reopened.get(dc.Actor, uid=2)

        assert again is not None
        assert again.memberships == {1: dc.STAFF, 2: dc.CURATOR}
        reopened.close()

    def test_a_user_class_named_actor_does_not_collide_with_the_registry(
        self, store: dc.Store
    ) -> None:
        """Given a registry actor and a user's own Actor entity stored side by side
        When the registry actor is fetched by uid
        Then it is the registry row (typenames are module-qualified)
        """
        with store.acting_as(BOOT):
            store.store(dc.Actor(uid=5, display="registry row", human=True))
            store.store(Actor(name="stage play lead"))
            store.commit()

            registry_row = store.get(dc.Actor, uid=5)

        assert registry_row is not None
        assert registry_row.display == "registry row"

    def test_a_user_class_named_actor_keeps_its_own_extent(self, store: dc.Store) -> None:
        """Given a registry actor and a user's own Actor entity stored side by side
        When the user's Actor entities are counted
        Then there is exactly one
        """
        with store.acting_as(BOOT):
            store.store(dc.Actor(uid=5, display="registry row", human=True))
            store.store(Actor(name="stage play lead"))
            store.commit()

        assert store.count(Actor) == 1
