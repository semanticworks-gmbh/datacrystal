"""Store.open(principal=), store.principal, and acting_as() (epic #168, W1 — #171).

Session identity only: resolution, the sponsor gate, scope semantics, and
owner confinement. The observable stamp lands with COMMIT-DELTA-v2 (W1-5);
here we pin the identity plumbing those stamps read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import cast

import pytest

import datacrystal as dc
from tests.conftest import Mineral

ORG, TEAM = 1, 2

# Actor is protected since W2 (ADR-008): registering actors needs a
# non-anonymous session. Since W5 (F1) an Actor's memberships are
# authority-ceiling-checked, so the config-trusted bootstrap that seeds the
# registry is ROOT — "whoever syncs the registry" is app-side trust
# (ADR-008:200); only root may mint authority it does not itself hold.
BOOT = dc.root_principal(1)

ANONYMOUS = dc.Principal(uid=0)


@pytest.fixture
def registry(store: dc.Store) -> dc.Store:
    """Return the store with Anna (human), a sponsored swarm and an unsponsored swarm committed."""
    with store.acting_as(BOOT):
        store.store(
            dc.Actor(
                uid=2,
                display="Anna",
                human=True,
                memberships={ORG: dc.STAFF, TEAM: dc.CURATOR},
            )
        )
        store.store(
            dc.Actor(
                uid=900,
                display="parser swarm",
                human=False,
                sponsor=2,
                memberships={TEAM: dc.AGENT},
            )
        )
        store.store(
            dc.Actor(uid=901, display="rogue swarm", human=False, memberships={TEAM: dc.AGENT})
        )
        store.commit()
    return store


def _act_as(store: dc.Store, who: int | dc.Principal) -> dc.Principal:
    """Enter and leave an acting_as scope, returning the principal it resolved."""
    with store.acting_as(who) as principal:
        return principal


def _raise_inside_scope(store: dc.Store) -> None:
    with store.acting_as(dc.Principal(uid=7)):
        raise RuntimeError("boom")


class TestAmbientPrincipal:
    """The identity a store carries when no acting_as scope is open."""

    def test_default_is_anonymous(self, store: dc.Store) -> None:
        """Given a store opened without a principal
        When its principal is read
        Then it is the anonymous uid 0
        """
        assert store.principal == ANONYMOUS

    def test_open_pins_the_ambient_identity(self, tmp_path: Path) -> None:
        """Given a store opened with a bootstrap principal
        When its principal is read
        Then it is that principal
        """
        boot = dc.Principal(uid=1, memberships={ORG: dc.ADMIN})

        with dc.Store.open(tmp_path / "store", principal=boot) as store:
            assert store.principal == boot


class TestActingAs:
    """acting_as switches the session identity for a scope and restores it after."""

    def test_scope_yields_the_principal(self, store: dc.Store) -> None:
        """Given claims for uid 41
        When a store enters acting_as with them
        Then the scope yields those claims
        """
        claims = dc.Principal(uid=41, memberships={TEAM: dc.STAFF})

        with store.acting_as(claims) as active:
            pass

        assert active == claims

    def test_store_principal_is_the_claims_inside_the_scope(self, store: dc.Store) -> None:
        """Given claims for uid 41
        When a store enters acting_as with them
        Then the store's principal inside the scope is those claims
        """
        claims = dc.Principal(uid=41, memberships={TEAM: dc.STAFF})

        with store.acting_as(claims):
            inside = store.principal

        assert inside == claims

    def test_scope_restores_the_ambient_principal(self, store: dc.Store) -> None:
        """Given claims for uid 41
        When the acting_as scope with them ends
        Then the store's principal is anonymous again
        """
        with store.acting_as(dc.Principal(uid=41, memberships={TEAM: dc.STAFF})):
            pass

        assert store.principal == ANONYMOUS

    def test_innermost_scope_wins(self, store: dc.Store) -> None:
        """Given an outer scope for uid 1 and an inner one for uid 2
        When the principal is read inside the inner scope
        Then it is the inner principal
        """
        inner = dc.Principal(uid=2)

        with store.acting_as(dc.Principal(uid=1)), store.acting_as(inner):
            inside = store.principal

        assert inside == inner

    def test_leaving_the_inner_scope_restores_the_outer(self, store: dc.Store) -> None:
        """Given an outer scope for uid 1 and an inner one for uid 2
        When the inner scope ends
        Then the principal is the outer one again
        """
        outer = dc.Principal(uid=1)

        with store.acting_as(outer):
            with store.acting_as(dc.Principal(uid=2)):
                pass
            after_inner = store.principal

        assert after_inner == outer

    def test_scope_restores_even_when_the_block_raises(self, store: dc.Store) -> None:
        """Given a scope for uid 7 whose block raises
        When the exception leaves the scope
        Then the store's principal is anonymous again
        """
        with pytest.raises(RuntimeError):
            _raise_inside_scope(store)

        assert store.principal == ANONYMOUS

    def test_uid_resolves_through_the_registry(self, registry: dc.Store) -> None:
        """Given Anna registered with two memberships
        When the store acts as uid 2
        Then the scope yields a principal with Anna's memberships
        """
        anna = _act_as(registry, 2)

        assert anna == dc.Principal(uid=2, memberships={ORG: dc.STAFF, TEAM: dc.CURATOR})

    def test_resolved_principal_is_decoupled_from_the_live_row(self, registry: dc.Store) -> None:
        """Given the store acting as Anna
        When the registry row grants her ADMIN in ORG in place
        Then the principal of the open scope keeps her old memberships
        """
        with registry.acting_as(2) as anna:
            with registry.acting_as(BOOT):  # BOOT owns the row (ADR-008 read fence)
                row = registry.get(dc.Actor, uid=2)  # anna's own row shares no group with her
                assert row is not None
                row.memberships[ORG] = dc.ADMIN  # a later grant …

            assert anna.memberships == {ORG: dc.STAFF, TEAM: dc.CURATOR}  # … not this scope

    def test_unknown_uid_refuses(self, registry: dc.Store) -> None:
        """Given a registry without uid 777
        When the store acts as uid 777
        Then an UnknownActorError is raised
        """
        with pytest.raises(dc.UnknownActorError):
            _act_as(registry, 777)

    def test_sponsor_gate_blocks_unsponsored_technical_user(self, registry: dc.Store) -> None:
        """Given a technical actor, uid 901, registered without a sponsor
        When the store acts as uid 901
        Then a SponsorRequiredError is raised
        """
        with pytest.raises(dc.SponsorRequiredError):
            _act_as(registry, 901)

    def test_sponsored_technical_user_may_act(self, registry: dc.Store) -> None:
        """Given a technical actor, uid 900, sponsored by the human Anna
        When the store acts as uid 900
        Then the scope yields the swarm's uid
        """
        swarm = _act_as(registry, 900)

        assert swarm.uid == 900

    def test_sponsored_technical_user_acts_with_its_own_memberships(
        self, registry: dc.Store
    ) -> None:
        """Given a technical actor, uid 900, sponsored by the human Anna
        When the store acts as uid 900
        Then the scope yields the swarm's own memberships
        """
        swarm = _act_as(registry, 900)

        assert swarm.memberships == {TEAM: dc.AGENT}

    def test_humans_need_no_sponsor(self, registry: dc.Store) -> None:
        """Given the human Anna registered without a sponsor
        When the store acts as uid 2
        Then the scope yields Anna's uid
        """
        anna = _act_as(registry, 2)

        assert anna.uid == 2


async def _principal_across_an_await(store: dc.Store) -> tuple[int, int]:
    """Return the uid seen inside an acting_as scope after an await, and the uid after the scope.

    The await hands control to the event loop for one tick and back, through an event the loop
    sets itself, so it waits on a condition and not on time.
    """
    astore = dc.AsyncStore(store)
    resumed = asyncio.Event()
    with astore.acting_as(dc.Principal(uid=7)):
        asyncio.get_running_loop().call_soon(resumed.set)
        await resumed.wait()
        uid_inside = astore.principal.uid
    return uid_inside, astore.principal.uid


async def _identities_of_interleaved_tasks(store: dc.Store) -> dict[str, int]:
    """Run task A, holding a uid-7 scope across an await, beside task B and report each uid seen."""
    astore = dc.AsyncStore(store)
    seen: dict[str, int] = {}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def task_a() -> None:
        with astore.acting_as(dc.Principal(uid=7)):
            entered.set()
            await release.wait()  # hold the scope across awaits
            seen["a"] = astore.principal.uid

    async def task_b() -> None:
        await entered.wait()  # A is inside its scope now
        seen["b"] = astore.principal.uid  # must NOT be 7
        with astore.acting_as(dc.Principal(uid=8)):
            seen["b_scoped"] = astore.principal.uid
        release.set()

    await asyncio.gather(task_a(), task_b())
    return seen


class TestTaskConfinement:
    """acting_as under aopen()/AsyncStore: contextvar-backed, task-confined.

    The load-bearing test is the interleave: task A holds acting_as across an
    await while task B runs — B must see its OWN identity (the ambient one),
    never A's. A plain stack would leak here; the ContextVar cannot.
    """

    def test_scope_survives_awaits_within_one_task(self, store: dc.Store) -> None:
        """Given a task inside an acting_as scope for uid 7
        When it awaits the event loop and resumes
        Then it is still uid 7
        """
        uid_inside, _ = asyncio.run(_principal_across_an_await(store))

        assert uid_inside == 7

    def test_scope_ends_for_the_task_after_the_await(self, store: dc.Store) -> None:
        """Given a task whose acting_as scope for uid 7 spanned an await
        When the scope ends
        Then the task is anonymous again
        """
        _, uid_after = asyncio.run(_principal_across_an_await(store))

        assert uid_after == 0

    def test_holder_task_keeps_its_own_identity(self, store: dc.Store) -> None:
        """Given task A holding a uid-7 scope across an await while task B runs
        When A resumes
        Then A still sees uid 7
        """
        seen = asyncio.run(_identities_of_interleaved_tasks(store))

        assert seen["a"] == 7

    def test_other_task_sees_the_ambient_identity_not_the_holders(self, store: dc.Store) -> None:
        """Given task A holding a uid-7 scope across an await while task B runs
        When B reads the principal outside any scope of its own
        Then B sees the ambient uid 0, never A's 7
        """
        seen = asyncio.run(_identities_of_interleaved_tasks(store))

        assert seen["b"] == 0

    def test_other_task_scopes_independently(self, store: dc.Store) -> None:
        """Given task A holding a uid-7 scope across an await while task B runs
        When B enters its own scope for uid 8
        Then B sees uid 8
        """
        seen = asyncio.run(_identities_of_interleaved_tasks(store))

        assert seen["b_scoped"] == 8


def _enter_scope(store: dc.Store, principal: dc.Principal) -> None:
    with store.acting_as(principal):
        pass


class TestOwnerConfinement:
    """acting_as belongs to the owner thread."""

    def test_foreign_thread_raises_before_any_switch(self, store: dc.Store) -> None:
        """Given a store owned by this thread
        When a foreign thread enters acting_as
        Then a WrongThreadError is raised
        """
        with ThreadPoolExecutor(max_workers=1) as pool:
            attempt = pool.submit(_enter_scope, store, dc.Principal(uid=5))

            with pytest.raises(dc.WrongThreadError):
                attempt.result(timeout=5)

    def test_foreign_thread_attempt_leaks_nothing(self, store: dc.Store) -> None:
        """Given a foreign thread whose acting_as attempt was refused
        When the owner reads the store's principal
        Then it is still anonymous
        """
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_enter_scope, store, dc.Principal(uid=5)).exception(timeout=5)

        assert store.principal == ANONYMOUS


class _StampCollector:
    """Minimal consumer recording every delta's (tid, actor)."""

    def __init__(self) -> None:
        self.watermark = 0
        self.stamps: list[tuple[int, int]] = []

    def apply(self, delta: dict[str, int]) -> bool:
        self.stamps.append((delta["tid"], delta["actor"]))
        self.watermark = delta["tid"]
        return True


def _commit_queued_mineral(store: dc.Store) -> None:
    # NB deliberately an UNPROTECTED entity: the closure runs as the
    # ambient (anonymous) principal, which since W2 cannot create
    # protected records (ADR-008 R6) — Actor is protected now.
    store.store(Mineral(qid="Q-queued", name="queued work"))
    store.commit()


class TestSubmittedWorkIdentity:
    """Work submitted from a foreign thread commits as the ambient principal."""

    def test_pumped_closures_run_ambient_never_the_owners_scope(self, store: dc.Store) -> None:
        """Given a commit closure queued from a foreign thread
        When the owner pumps the queue inside an acting_as(42) scope and commits its own actor
        Then the queued commit is stamped with the ambient uid 0 and the owner's with uid 42

        Review finding (HIGH): the pump piggybacks on owner API calls, which may sit INSIDE an
        acting_as scope. A queued foreign-thread closure must commit as the AMBIENT principal,
        never whatever scope the owner happened to be in when it pumped.
        """
        collector = _StampCollector()
        store.attach(collector)
        with ThreadPoolExecutor(max_workers=1) as pool:
            queued: Future[None] = pool.submit(
                store.submit, partial(_commit_queued_mineral, store)
            ).result(timeout=5)

        with store.acting_as(dc.Principal(uid=42)):
            store.count(dc.Actor)  # an owner API boundary → pumps the queue HERE
            store.store(dc.Actor(uid=42, display="op", human=True))  # scoped: allowed
            store.commit()  # the owner's OWN commit keeps the scoped stamp

        queued.result(timeout=5)
        assert [actor for _tid, actor in collector.stamps] == [0, 42]


class TestRegistryGates:
    """The registry must be durable and well-sponsored before an identity may act."""

    @pytest.fixture
    def sponsored_bots(self, store: dc.Store) -> dc.Store:
        """Return the store with Anna, her bot, a ghost-backed bot and a bot-backed bot."""
        with store.acting_as(BOOT):
            store.store(dc.Actor(uid=2, display="Anna", human=True))
            store.store(dc.Actor(uid=900, display="bot", human=False, sponsor=2))
            store.store(dc.Actor(uid=901, display="ghost-backed", human=False, sponsor=777))
            store.store(dc.Actor(uid=902, display="bot-backed", human=False, sponsor=900))
            store.commit()
        return store

    def test_bot_with_a_human_sponsor_may_act(self, sponsored_bots: dc.Store) -> None:
        """Given a bot sponsored by the registered human Anna
        When the store acts as the bot
        Then the scope yields the bot's uid
        """
        bot = _act_as(sponsored_bots, 900)

        assert bot.uid == 900

    def test_sponsor_that_was_never_registered_refuses(self, sponsored_bots: dc.Store) -> None:
        """Given a bot whose sponsor uid 777 was never registered
        When the store acts as the bot
        Then a SponsorRequiredError naming a human is raised
        """
        with pytest.raises(dc.SponsorRequiredError, match="human"):
            _act_as(sponsored_bots, 901)

    def test_sponsor_that_is_itself_a_bot_refuses(self, sponsored_bots: dc.Store) -> None:
        """Given a bot sponsored by another bot
        When the store acts as the sponsored bot
        Then a SponsorRequiredError naming a human is raised
        """
        with pytest.raises(dc.SponsorRequiredError, match="human"):
            _act_as(sponsored_bots, 902)

    @pytest.fixture
    def buffered_sponsor(self, store: dc.Store) -> Iterator[dc.Store]:
        """Yield the store, inside BOOT's scope, with a bot's sponsor set but not committed."""
        with store.acting_as(BOOT):
            store.store(dc.Actor(uid=900, display="bot", human=False))
            store.store(dc.Actor(uid=2, display="Anna", human=True))
            store.commit()
            row = cast(dc.Actor, store.get(dc.Actor, uid=900))
            row.sponsor = 2  # buffered, NOT committed
            yield store

    def test_uncommitted_actor_rows_refuse(self, buffered_sponsor: dc.Store) -> None:
        """Given a bot whose sponsor edit is buffered and not committed
        When the store acts as the bot
        Then an UncommittedActorError is raised

        Identity must be durable before it acts: buffered registry edits would authorize
        stamps the replayed history cannot explain.
        """
        with pytest.raises(dc.UncommittedActorError):
            _act_as(buffered_sponsor, 900)

    def test_committed_actor_rows_may_act(self, buffered_sponsor: dc.Store) -> None:
        """Given a bot whose buffered sponsor edit was then committed
        When the store acts as the bot
        Then the scope yields the bot's uid
        """
        buffered_sponsor.commit()  # now durable (BOOT owns the row — clears the gate)

        bot = _act_as(buffered_sponsor, 900)

        assert bot.uid == 900

    def test_buffered_new_actor_is_not_actable(self, store: dc.Store) -> None:
        """Given a new actor stored but not committed
        When the store acts as it
        Then an UnknownActorError is raised
        """
        with store.acting_as(BOOT):
            store.store(dc.Actor(uid=5, display="new hire", human=True))  # uncommitted

        with pytest.raises(dc.UnknownActorError):
            _act_as(store, 5)
