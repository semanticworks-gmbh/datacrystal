"""W3-5: the unified live-store read-fence sieve — one record, every public
read surface, in a loop (ADR-008, epic #168, issue #173).

W3-1..W3-4 already proved each surface's own filtering in isolation
(``test_read_fence_discovery.py``, ``test_read_fence_query.py``,
``test_redacted_twin.py``, the bitmap-vs-row-filter oracle in
``tests/property/test_readable_oracle.py``). Correct-in-isolation is not the
same claim as "the concept holds together": a fence built one call site at a
time is only as strong as its LEAST-covered site — a single forgotten
``readable_bitmap()``/``committed_labels()``/``_load_oid_deref()`` call on
some future surface is a silent leak that none of those per-surface files
would ever notice (each only asserts about the surface it already knows to
test). This file is the closing proof: take ONE protected, owner-only
(unshared) record and run it through EVERY live-store public read surface in
a single loop, keyed by one dict literal — so a future surface that forgets
the filter fails HERE, naming itself in the assertion message, rather than
three PRs later when someone notices data leaking.

Discovery surfaces looped (R12 — denied is silently ABSENT, never an error):
``get(cls, key)``, ``get_many(cls, key=[...])``, ``query(cls)``,
``query(cond)``, ``query_iter(cls)``, ``count(cond)``, ``pluck(cls, field)``,
``explain(cls).extent``, ``explain(cond).candidates``, ``incoming(target)``.

Deref surfaces looped (R14 — denied is a redacted TWIN, since you already
hold the reference): ``get_many([oid])`` and a ``Lazy[T]`` handle's
``.get()``/``.peek()``.

Both storage backends ride along for free via the ``store``/``store_factory``
fixtures (``tests/conftest.py``). Mineral-cabinet domain only (no second
domain): a protected, self-referential ``Specimen`` (so one Specimen can be
an ``incoming()`` REFERRER of another) plus an unprotected ``Cabinet``
holding a direct ``Lazy`` handle onto the record under test.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._entity import oid_of

TEAM = 5
OTHER_TEAM = 6
LABEL = "the-record"

OWNER = dc.Principal(uid=2, memberships={TEAM: dc.CURATOR})
OUTSIDER = dc.Principal(uid=4, memberships={OTHER_TEAM: dc.CURATOR})
ANON = dc.Principal(uid=0)
ROOT = dc.Principal(uid=99, memberships={dc.WORLD: dc.EXECUTIVE})

# The ONE place a future principal category is added to this file's sweep.
PRINCIPALS = {"outsider": OUTSIDER, "anonymous": ANON, "owner": OWNER, "root": ROOT}

StoreFactory = Callable[[], dc.Store]


@dc.entity(protected=True)
class Specimen:
    """The one protected record every surface below is asked about.

    Self-referential ``linked`` lets a Specimen be an ``incoming()``
    REFERRER of another Specimen — no second domain needed for that surface.
    """

    label: Annotated[str, dc.Unique]
    note: str = ""
    linked: dc.Lazy[Specimen] | None = None


@dc.entity
class Cabinet:
    """An unprotected referrer holding a direct ``Lazy`` handle onto the record under test.

    The seam ``get_many(iterable)``/``Lazy.get()`` exercise (R14: a deref surface).
    """

    tag: Annotated[str, dc.Unique]
    holds: dc.Lazy[Specimen] | None = None


F = dc.fields(Specimen)


def _seed(store: dc.Store) -> tuple[Specimen, Specimen, Cabinet]:
    """Store the hidden record, a WORLD anchor it links to and a cabinet holding a handle on it.

    ``hidden`` has owner-only, unshared birth labels (ADR-008 R6): readable by nobody but
    OWNER/ROOT. ``anchor`` is WORLD-readable, so ``incoming(anchor)`` can include or exclude
    ``hidden`` as a referrer.
    """
    with store.acting_as(OWNER):
        anchor = Specimen(label="anchor")
        store.store(anchor)
        dc.share(anchor, dc.WORLD, read=dc.VIEWER, write=dc.VIEWER)

        hidden = Specimen(label=LABEL, note="classified", linked=dc.Lazy.of(anchor))
        store.store(hidden)

        cabinet = Cabinet(tag="drawer-1", holds=dc.Lazy.of(hidden))
        store.store(cabinet)
        store.commit()
    return hidden, anchor, cabinet


def _commit_hidden_record(store_factory: StoreFactory) -> int:
    """Commit the hidden record and a cabinet holding it, close the store and return its OID."""
    store = store_factory()
    with store.acting_as(OWNER):
        hidden = Specimen(label=LABEL, note="classified")
        hidden_oid = store.store(hidden)
        store.store(Cabinet(tag="drawer-1", holds=dc.Lazy.of(hidden)))
        store.commit()
    store.close()
    return hidden_oid


@pytest.mark.parametrize("principal_name", ["outsider", "anonymous"])
def test_denied_principal_finds_the_record_on_no_discovery_surface(
    store: dc.Store, principal_name: str
) -> None:
    """Given an owner-only record and a WORLD-readable anchor that it links to
    When an outsider or anonymous principal runs every discovery surface
    Then the record is silently absent from each of the ten
    """
    _hidden, anchor, _cabinet = _seed(store)
    with store.acting_as(PRINCIPALS[principal_name]):
        via_get = store.get(Specimen, label=LABEL)
        via_get_many = store.get_many(Specimen, label=[LABEL])
        via_query_cls = store.query(Specimen)
        via_query_cond = store.query(F.label == LABEL)
        via_query_iter = list(store.query_iter(Specimen))
        via_count = store.count(F.label == LABEL)
        via_pluck = store.pluck(Specimen, "label")
        extent = store.explain(Specimen).extent
        candidates = store.explain(F.label == LABEL).candidates
        via_incoming = store.incoming(anchor)
    assert via_get is None
    assert via_get_many == [None]
    assert LABEL not in [found.label for found in via_query_cls]
    assert via_query_cond == []
    assert LABEL not in [found.label for found in via_query_iter]
    assert via_count == 0
    assert LABEL not in via_pluck
    assert extent == 1
    assert candidates == 0
    assert via_incoming == []


@pytest.mark.parametrize("principal_name", ["owner", "root"])
def test_readable_principal_finds_the_record_on_every_discovery_surface(
    store: dc.Store, principal_name: str
) -> None:
    """Given an owner-only record and a WORLD-readable anchor that it links to
    When the owner or root runs every discovery surface
    Then the record is present on each of the ten
    """
    _hidden, anchor, _cabinet = _seed(store)
    with store.acting_as(PRINCIPALS[principal_name]):
        via_get = store.get(Specimen, label=LABEL)
        via_get_many = store.get_many(Specimen, label=[LABEL])
        via_query_cls = store.query(Specimen)
        via_query_cond = store.query(F.label == LABEL)
        via_query_iter = list(store.query_iter(Specimen))
        via_count = store.count(F.label == LABEL)
        via_pluck = store.pluck(Specimen, "label")
        extent = store.explain(Specimen).extent
        candidates = store.explain(F.label == LABEL).candidates
        via_incoming = store.incoming(anchor)
    assert via_get is not None
    assert via_get_many[0] is not None
    assert LABEL in [found.label for found in via_query_cls]
    assert LABEL in [found.label for found in via_query_cond]
    assert LABEL in [found.label for found in via_query_iter]
    assert via_count == 1
    assert LABEL in via_pluck
    assert extent == 2
    assert candidates == 1
    assert LABEL in [getattr(referrer, "label", None) for referrer in via_incoming]


@pytest.mark.parametrize("principal_name", ["outsider", "anonymous"])
def test_denied_principal_derefing_get_many_gets_a_twin(
    store: dc.Store, principal_name: str
) -> None:
    """Given an owner-only record whose OID a denied principal already holds
    When the denied principal derefs it through get_many
    Then it gets a redacted twin of the record's type
    And reading the twin's label raises ReadDeniedError
    """
    hidden, _anchor, _cabinet = _seed(store)
    with store.acting_as(PRINCIPALS[principal_name]):
        [via_get_many] = store.get_many([oid_of(hidden)])
    assert isinstance(via_get_many, dc.Redacted)
    assert isinstance(via_get_many, Specimen)
    with pytest.raises(dc.ReadDeniedError):
        _ = via_get_many.label


@pytest.mark.parametrize("principal_name", ["outsider", "anonymous"])
def test_denied_principal_derefing_a_lazy_handle_gets_a_twin(
    store: dc.Store, principal_name: str
) -> None:
    """Given a cabinet whose Lazy handle points at an owner-only record
    When a denied principal calls get on the handle
    Then it gets a redacted twin of the record's type
    And reading the twin's label raises ReadDeniedError
    """
    _hidden, _anchor, cabinet = _seed(store)
    handle = cabinet.holds
    assert handle is not None
    with store.acting_as(PRINCIPALS[principal_name]):
        via_lazy = handle.get()
    assert isinstance(via_lazy, dc.Redacted)
    assert isinstance(via_lazy, Specimen)
    with pytest.raises(dc.ReadDeniedError):
        _ = via_lazy.label


@pytest.mark.parametrize("principal_name", ["outsider", "anonymous"])
def test_denied_principal_peeking_a_lazy_handle_finds_nothing_cached(
    store: dc.Store, principal_name: str
) -> None:
    """Given a cabinet whose Lazy handle points at an owner-only record
    When a denied principal derefs and then peeks at the handle
    Then peek returns None
    """
    _hidden, _anchor, cabinet = _seed(store)
    handle = cabinet.holds
    assert handle is not None
    with store.acting_as(PRINCIPALS[principal_name]):
        handle.get()
        peeked = handle.peek()
    assert peeked is None


@pytest.mark.parametrize("principal_name", ["owner", "root"])
def test_readable_principal_derefing_get_many_gets_the_real_record(
    store: dc.Store, principal_name: str
) -> None:
    """Given an owner-only record whose OID is held
    When the owner or root derefs it through get_many
    Then it gets the real record, not a twin
    And the record carries its label
    """
    hidden, _anchor, _cabinet = _seed(store)
    with store.acting_as(PRINCIPALS[principal_name]):
        [via_get_many] = store.get_many([oid_of(hidden)])
    assert not isinstance(via_get_many, dc.Redacted)
    assert via_get_many.label == LABEL


@pytest.mark.parametrize("principal_name", ["owner", "root"])
def test_readable_principal_derefing_a_lazy_handle_gets_the_real_record(
    store: dc.Store, principal_name: str
) -> None:
    """Given a cabinet whose Lazy handle points at an owner-only record
    When the owner or root calls get on the handle
    Then it gets the real record, not a twin
    And the record carries its label
    """
    _hidden, _anchor, cabinet = _seed(store)
    handle = cabinet.holds
    assert handle is not None
    with store.acting_as(PRINCIPALS[principal_name]):
        via_lazy = handle.get()
    assert not isinstance(via_lazy, dc.Redacted)
    assert via_lazy.label == LABEL


def test_denied_deref_before_anyone_loaded_the_record_registers_nothing(
    store_factory: StoreFactory,
) -> None:
    """Given a reopened store where nobody has loaded the owner-only record
    When an outsider calls get on a Lazy handle onto it
    Then the outsider gets a twin
    And the identity registry holds nothing for the record
    """
    hidden_oid = _commit_hidden_record(store_factory)
    reopened = store_factory()
    with reopened.acting_as(OUTSIDER):
        cabinet = reopened.query(Cabinet)[0]
        handle = cabinet.holds
        assert handle is not None
        first_twin = handle.get()
    assert isinstance(first_twin, dc.Redacted)
    assert reopened._registry.get(hidden_oid) is None
    reopened.close()


def test_owner_deref_after_a_denied_deref_loads_the_real_registered_record(
    store_factory: StoreFactory,
) -> None:
    """Given a reopened store where an outsider already derefed the record into a twin
    When the owner calls get on the same Lazy handle
    Then the owner gets the real record
    And the identity registry holds that instance
    """
    hidden_oid = _commit_hidden_record(store_factory)
    reopened = store_factory()
    with reopened.acting_as(OUTSIDER):
        handle = reopened.query(Cabinet)[0].holds
        assert handle is not None
        handle.get()
    with reopened.acting_as(OWNER):
        real = handle.get()
    assert not isinstance(real, dc.Redacted)
    assert real.label == LABEL
    assert reopened._registry.get(hidden_oid) is real
    reopened.close()


def test_denied_deref_after_the_owner_loaded_the_record_leaves_the_registry_alone(
    store_factory: StoreFactory,
) -> None:
    """Given a reopened store where the owner already loaded the real record
    When an outsider calls get on the same Lazy handle
    Then the outsider gets a twin
    And the identity registry still holds the real instance
    """
    hidden_oid = _commit_hidden_record(store_factory)
    reopened = store_factory()
    with reopened.acting_as(OWNER):
        handle = reopened.query(Cabinet)[0].holds
        assert handle is not None
        real = handle.get()
    with reopened.acting_as(OUTSIDER):
        second_twin = handle.get()
    assert isinstance(second_twin, dc.Redacted)
    assert reopened._registry.get(hidden_oid) is real
    reopened.close()


def test_owner_deref_after_a_denied_deref_returns_the_same_instance(
    store_factory: StoreFactory,
) -> None:
    """Given a reopened store where the owner loaded the record and an outsider then derefed it
    When the owner calls get on the same Lazy handle again
    Then the owner gets the identical instance, not a re-materialized one
    """
    _commit_hidden_record(store_factory)
    reopened = store_factory()
    with reopened.acting_as(OWNER):
        handle = reopened.query(Cabinet)[0].holds
        assert handle is not None
        real = handle.get()
    with reopened.acting_as(OUTSIDER):
        handle.get()
    with reopened.acting_as(OWNER):
        real_again = handle.get()
    assert real_again is real
    reopened.close()
