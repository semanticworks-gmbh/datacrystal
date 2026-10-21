"""Persistent containers: in-place list/dict mutation is never lost.

The M2 deliverable for KICKOFF risk 1 (silent dirty-tracking loss): every
list/dict entering an entity field is wrapped in an owner-bound
PersistentList/PersistentDict; mutators flip the owner to DIRTY before
mutating; frozen owners raise.
"""

from __future__ import annotations

import gc
from collections.abc import Callable

import pytest

import datacrystal as dc
from datacrystal._errors import FrozenEntityError
from tests.conftest import LogEntry, Mineral

StoreFactory = Callable[[], dc.Store]


@dc.entity(frozen=True)
class FrozenWithList:
    """A frozen entity holding a list, whose container must raise on mutation."""

    items: list


def test_inplace_list_append_on_clean_entity_commits(store_factory: StoreFactory) -> None:
    """THE footgun: mutate a list inside a committed entity in place, no reassignment.

    Given a committed root list holding a mineral with no tags
    When "collected" is appended to the mineral's tags and the store commits
    Then a TID is returned
    """
    store = store_factory()
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()
    store.root[0].tags.append("collected")

    tid = store.commit()

    assert tid is not None
    store.close()


def test_inplace_list_append_on_clean_entity_survives_reopen(store_factory: StoreFactory) -> None:
    """Given a committed root list holding a mineral with no tags
    When "collected" is appended in place, committed, and the store reopened
    Then the mineral's tags are ["collected"]
    """
    store = store_factory()
    store.root = [Mineral(qid="Q1", name="quartz")]
    store.commit()
    store.root[0].tags.append("collected")
    store.commit()
    store.close()

    reopened = store_factory()

    assert reopened.root[0].tags == ["collected"]
    reopened.close()


def test_inplace_mutation_after_reopen_survives_another_reopen(
    store_factory: StoreFactory,
) -> None:
    """Given a mineral tagged ["a"], committed and reopened
    When "b" is appended in place, committed, and the store reopened again
    Then the mineral's tags are ["a", "b"]
    """
    store = store_factory()
    store.root = [Mineral(qid="Q1", name="quartz", tags=["a"])]
    store.commit()
    store.close()
    reopened = store_factory()
    reopened.root[0].tags.append("b")
    reopened.commit()
    reopened.close()

    third = store_factory()

    assert third.root[0].tags == ["a", "b"]
    third.close()


@pytest.fixture
def nested_root_edited(store_factory: StoreFactory) -> None:
    """Commit a nested root, then append a mineral and add an empty list in place, and reopen."""
    store = store_factory()
    store.root = {"by_system": {"trigonal": [Mineral(qid="Q1", name="quartz")]}}
    store.commit()
    store.root["by_system"]["trigonal"].append(Mineral(qid="Q2", name="calcite"))
    store.root["by_system"]["hexagonal"] = []
    store.commit()
    store.close()


@pytest.mark.usefixtures("nested_root_edited")
def test_nested_list_append_survives_reopen(store_factory: StoreFactory) -> None:
    """Given a committed nested root with an appended calcite under "trigonal"
    When the store is reopened
    Then "trigonal" holds quartz and calcite
    """
    reopened = store_factory()

    trigonal = reopened.root["by_system"]["trigonal"]

    assert [m.name for m in trigonal] == ["quartz", "calcite"]
    reopened.close()


@pytest.mark.usefixtures("nested_root_edited")
def test_nested_dict_insert_survives_reopen(store_factory: StoreFactory) -> None:
    """Given a committed nested root with an empty list added under "hexagonal"
    When the store is reopened
    Then "hexagonal" is an empty list
    """
    reopened = store_factory()

    hexagonal = reopened.root["by_system"]["hexagonal"]

    assert hexagonal == []
    reopened.close()


def test_empty_root_list_grows_transparently(store_factory: StoreFactory) -> None:
    """The natural first-run flow: start from an empty root, append later.

    Given an empty list committed as the root
    When a mineral is appended to it, committed, and the store reopened
    Then the root holds quartz only
    """
    store = store_factory()
    store.root = []
    store.commit()
    store.root.append(Mineral(qid="Q1", name="quartz"))
    store.commit()
    store.close()

    reopened = store_factory()

    assert [m.name for m in reopened.root] == ["quartz"]
    reopened.close()


def test_root_dict_is_a_persistent_dict(store: dc.Store) -> None:
    """Given a store
    When a dict is assigned as the root
    Then store.root is a PersistentDict
    """
    store.root = {"minerals": []}

    assert isinstance(store.root, dc.PersistentDict)


def test_root_dict_values_are_persistent_lists(store: dc.Store) -> None:
    """Given a store
    When a dict holding a list is assigned as the root
    Then the list under "minerals" is a PersistentList
    """
    store.root = {"minerals": []}

    assert isinstance(store.root["minerals"], dc.PersistentList)


@pytest.fixture
def tagged_store(store: dc.Store) -> dc.Store:
    """Commit a mineral tagged ["c", "a", "b"] as the root."""
    store.root = Mineral(qid="Q1", name="quartz", tags=["c", "a", "b"])
    store.commit()
    return store


@pytest.fixture
def counted_store(store: dc.Store) -> dc.Store:
    """Commit the dict {"n": 1} as the root."""
    store.root = {"n": 1}
    store.commit()
    return store


def test_list_sort_marks_the_owner_dirty(tagged_store: dc.Store) -> None:
    """Given a committed mineral tagged ["c", "a", "b"]
    When its tags are sorted in place and the store commits
    Then a TID is returned
    """
    tagged_store.root.tags.sort()

    tid = tagged_store.commit()

    assert tid is not None


def test_list_reverse_marks_the_owner_dirty(tagged_store: dc.Store) -> None:
    """Given a committed mineral tagged ["c", "a", "b"]
    When its tags are reversed in place and the store commits
    Then a TID is returned
    """
    tagged_store.root.tags.reverse()

    tid = tagged_store.commit()

    assert tid is not None


def test_list_insert_marks_the_owner_dirty(tagged_store: dc.Store) -> None:
    """Given a committed mineral tagged ["c", "a", "b"]
    When "x" is inserted at 0 and the store commits
    Then a TID is returned
    """
    tagged_store.root.tags.insert(0, "x")

    tid = tagged_store.commit()

    assert tid is not None


def test_list_remove_marks_the_owner_dirty(tagged_store: dc.Store) -> None:
    """Given a committed mineral tagged ["c", "a", "b"]
    When "a" is removed and the store commits
    Then a TID is returned
    """
    tagged_store.root.tags.remove("a")

    tid = tagged_store.commit()

    assert tid is not None


def test_list_pop_marks_the_owner_dirty(tagged_store: dc.Store) -> None:
    """Given a committed mineral tagged ["c", "a", "b"]
    When the last tag is popped and the store commits
    Then a TID is returned
    """
    tagged_store.root.tags.pop()

    tid = tagged_store.commit()

    assert tid is not None


def test_list_setitem_marks_the_owner_dirty(tagged_store: dc.Store) -> None:
    """Given a committed mineral tagged ["c", "a", "b"]
    When tag 0 is set to "y" and the store commits
    Then a TID is returned
    """
    tagged_store.root.tags[0] = "y"

    tid = tagged_store.commit()

    assert tid is not None


def test_list_extend_marks_the_owner_dirty(tagged_store: dc.Store) -> None:
    """Given a committed mineral tagged ["c", "a", "b"]
    When ["z"] extends its tags and the store commits
    Then a TID is returned
    """
    tagged_store.root.tags.extend(["z"])

    tid = tagged_store.commit()

    assert tid is not None


def test_list_delitem_marks_the_owner_dirty(tagged_store: dc.Store) -> None:
    """Given a committed mineral tagged ["c", "a", "b"]
    When tag 0 is deleted and the store commits
    Then a TID is returned
    """
    del tagged_store.root.tags[0]

    tid = tagged_store.commit()

    assert tid is not None


def test_dict_setitem_marks_the_owner_dirty(counted_store: dc.Store) -> None:
    """Given a committed root {"n": 1}
    When "m" is set to 2 and the store commits
    Then a TID is returned
    """
    counted_store.root["m"] = 2

    tid = counted_store.commit()

    assert tid is not None


def test_dict_update_marks_the_owner_dirty(counted_store: dc.Store) -> None:
    """Given a committed root {"n": 1}
    When it is updated with k=3 and the store commits
    Then a TID is returned
    """
    counted_store.root.update(k=3)

    tid = counted_store.commit()

    assert tid is not None


def test_dict_update_stores_positional_and_keyword_pairs(counted_store: dc.Store) -> None:
    """Given a committed root {"n": 1}
    When it is updated with {"a": 1} and k=3
    Then the root holds n, a and k with their values
    """
    counted_store.root.update({"a": 1}, k=3)

    assert dict(counted_store.root) == {"n": 1, "a": 1, "k": 3}


def test_list_insert_puts_the_item_at_the_index(tagged_store: dc.Store) -> None:
    """Given a committed mineral tagged ["c", "a", "b"]
    When "x" is inserted at index 1
    Then the tags read ["c", "x", "a", "b"]
    """
    tagged_store.root.tags.insert(1, "x")

    assert list(tagged_store.root.tags) == ["c", "x", "a", "b"]


def test_list_insert_of_a_container_wraps_it_for_the_owner(tagged_store: dc.Store) -> None:
    """Given a committed mineral tagged ["c", "a", "b"]
    When a plain list is inserted at index 0
    Then the inserted item is a PersistentList holding its values
    """
    tagged_store.root.tags.insert(0, ["n"])

    assert isinstance(tagged_store.root.tags[0], dc.PersistentList)


def test_dict_setdefault_of_a_new_key_marks_the_owner_dirty(counted_store: dc.Store) -> None:
    """Given a committed root {"n": 1}
    When setdefault adds "fresh" and the store commits
    Then a TID is returned
    """
    counted_store.root.setdefault("fresh", 4)

    tid = counted_store.commit()

    assert tid is not None


def test_dict_pop_marks_the_owner_dirty(counted_store: dc.Store) -> None:
    """Given a committed root {"n": 1}
    When "n" is popped and the store commits
    Then a TID is returned
    """
    counted_store.root.pop("n")

    tid = counted_store.commit()

    assert tid is not None


def test_dict_delitem_marks_the_owner_dirty(counted_store: dc.Store) -> None:
    """Given a committed root {"n": 1}
    When "n" is deleted and the store commits
    Then a TID is returned
    """
    del counted_store.root["n"]

    tid = counted_store.commit()

    assert tid is not None


def test_dict_clear_marks_the_owner_dirty(counted_store: dc.Store) -> None:
    """Given a committed root {"n": 1}
    When it is cleared and the store commits
    Then a TID is returned
    """
    counted_store.root.clear()

    tid = counted_store.commit()

    assert tid is not None


def test_setdefault_on_an_existing_key_returns_the_existing_value(
    counted_store: dc.Store,
) -> None:
    """Given a committed root {"n": 1}
    When setdefault("n", 99) is called
    Then it returns 1
    """
    value = counted_store.root.setdefault("n", 99)

    assert value == 1


def test_setdefault_on_an_existing_key_leaves_the_store_clean(counted_store: dc.Store) -> None:
    """Given a committed root {"n": 1}
    When setdefault("n", 99) is called and the store commits
    Then nothing is committed
    """
    counted_store.root.setdefault("n", 99)

    tid = counted_store.commit()

    assert tid is None


def test_assignment_keeps_the_container_values() -> None:
    """Given a list ["a"]
    When a mineral is built with it as tags
    Then the mineral's tags are ["a"]
    """
    tags = ["a"]

    mineral = Mineral(qid="Q1", name="quartz", tags=tags)

    assert mineral.tags == ["a"]


def test_assignment_snapshots_the_container() -> None:
    """Containers are by-value parts of their owner: assignment copies.

    Given a mineral built with the list ["a"] as tags
    When the original list gets "rogue" appended
    Then the mineral's tags are still ["a"]
    """
    tags = ["a"]
    mineral = Mineral(qid="Q1", name="quartz", tags=tags)

    tags.append("rogue")

    assert mineral.tags == ["a"]


def test_frozen_entity_rejects_edits_after_commit(store: dc.Store) -> None:
    """Given a frozen LogEntry committed inside a root list
    When its note is assigned
    Then FrozenEntityError is raised
    """
    store.root = {"log": [LogEntry(note="acquired", kind="acquisition")]}
    store.commit()

    with pytest.raises(FrozenEntityError):
        store.root["log"][0].note = "edited"  # pyrefly: ignore[read-only]


def test_frozen_container_rejects_mutation_in_the_same_process(store: dc.Store) -> None:
    """Given a frozen entity holding the list ["a"], committed
    When "b" is appended to its list
    Then FrozenEntityError is raised
    """
    store.root = FrozenWithList(items=["a"])
    store.commit()

    with pytest.raises(FrozenEntityError):
        store.root.items.append("b")


def test_frozen_container_rejects_mutation_on_a_hydrated_copy(
    store_factory: StoreFactory,
) -> None:
    """Given a frozen entity holding the list ["a"], committed and reopened
    When "b" is appended to its list
    Then FrozenEntityError is raised
    """
    store = store_factory()
    store.root = FrozenWithList(items=["a"])
    store.commit()
    store.close()
    reopened = store_factory()

    with pytest.raises(FrozenEntityError):
        reopened.root.items.append("b")

    reopened.close()


def test_rejected_append_leaves_the_hydrated_frozen_list_unchanged(
    store_factory: StoreFactory,
) -> None:
    """Given a frozen entity holding ["a"], reopened, whose append of "b" was rejected
    When its list is read
    Then it is still ["a"]
    """
    store = store_factory()
    store.root = FrozenWithList(items=["a"])
    store.commit()
    store.close()
    reopened = store_factory()
    with pytest.raises(FrozenEntityError):
        reopened.root.items.append("b")

    items = reopened.root.items

    assert items == ["a"]
    reopened.close()


def test_container_alone_keeps_its_owner_alive_for_commit(store: dc.Store) -> None:
    """Holding only the container must be enough: it pins its owner.

    Given a committed mineral of which only its tags list is still referenced
    When "still-tracked" is appended to the tags and the store commits
    Then a TID is returned
    """
    orphan = Mineral(qid="QSOLO", name="solo", tags=[])
    store.store(orphan)
    store.commit()
    tags = orphan.tags
    del orphan
    gc.collect()
    tags.append("still-tracked")

    tid = store.commit()

    assert tid is not None


def test_container_mutation_without_the_entity_reference_survives_reopen(
    store_factory: StoreFactory,
) -> None:
    """Given a committed mineral of which only its tags list is still referenced
    When "still-tracked" is appended to the tags, committed, and the store reopened
    Then the mineral's tags are ["still-tracked"]
    """
    store = store_factory()
    orphan = Mineral(qid="QSOLO", name="solo", tags=[])
    store.store(orphan)
    store.commit()
    tags = orphan.tags
    del orphan
    gc.collect()
    tags.append("still-tracked")
    store.commit()
    store.close()

    reopened = store_factory()
    found = reopened.get(Mineral, qid="QSOLO")

    assert found is not None
    assert found.tags == ["still-tracked"]
    reopened.close()
