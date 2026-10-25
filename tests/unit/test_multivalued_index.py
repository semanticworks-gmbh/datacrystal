"""Multi-valued (inverted) index on list fields (#13).

A mineral specimen carries several tags (museum / fine / fluorescent / rare);
``query(Specimen.tags.contains("rare"))`` must answer from an element-keyed
bitmap with zero record reads — instead of exploding each tag into its own
entity. Parametrized over both backends: the storage protocol is the seam,
memory and sqlite must behave identically.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import field
from typing import Annotated

import pytest

import datacrystal as dc


@dc.entity
class Specimen:
    """A cabinet specimen with a list of tags and optional aliases."""

    specimen_no: Annotated[str, dc.Unique]
    tags: Annotated[list[str], dc.Index] = field(default_factory=list)
    aliases: Annotated[list[str] | None, dc.Index] = None


S = dc.fields(Specimen)
TAG_ANSWERS = pytest.mark.parametrize(
    ("tag", "expected"),
    [("rare", "S1, S3"), ("fluorescent", "S1, S2"), ("museum", "S1"), ("fine", "S2"),
     ("nonexistent", "")],
)


def _nos(hits: Iterable[Specimen]) -> str:
    return ", ".join(sorted(s.specimen_no for s in hits))


@pytest.fixture
def drawer(store: dc.Store) -> dc.Store:
    """Return a store with specimens S1..S4 committed."""
    store.root = [
        Specimen(specimen_no="S1", tags=["museum", "fluorescent", "rare"], aliases=["alt"]),
        Specimen(specimen_no="S2", tags=["fine", "fluorescent"]),
        Specimen(specimen_no="S3", tags=["rare"]),
        Specimen(specimen_no="S4", tags=[]),
    ]
    store.commit()
    return store


def test_a_list_index_field_answers_membership_from_the_index(drawer: dc.Store) -> None:
    """Given the Specimen entity with an indexed list field tags
    When the plan for tags containing "rare" is explained
    Then the plan is indexed
    """
    plan = drawer.explain(S.tags.contains("rare"))

    assert plan.indexed


def test_an_optional_list_field_answers_membership_from_the_index(drawer: dc.Store) -> None:
    """Given specimens where S1 has aliases ["alt"] and an indexed list[str] | None field aliases
    When aliases containing "alt" is queried and its plan explained
    Then S1 is found
    And the plan is indexed
    """
    hits = drawer.query(S.aliases.contains("alt"))
    plan = drawer.explain(S.aliases.contains("alt"))

    assert _nos(hits) == "S1"
    assert plan.indexed


def test_a_scalar_field_contains_is_a_substring_match_not_membership(drawer: dc.Store) -> None:
    """Given specimens S1..S4 and a scalar field specimen_no
    When specimen_no containing "S" is queried
    Then every specimen matches, as substring and not list membership
    """
    hits = drawer.query(S.specimen_no.contains("S"))

    assert _nos(hits) == "S1, S2, S3, S4"


@TAG_ANSWERS
def test_contains_matches_the_specimens_carrying_the_tag(
    drawer: dc.Store, tag: str, expected: str
) -> None:
    """Given specimens S1..S4 with tags
    When tags are queried for containing one tag
    Then exactly the specimens carrying it match
    """
    hits = drawer.query(S.tags.contains(tag))

    assert _nos(hits) == expected


def test_contains_is_membership_not_substring(drawer: dc.Store) -> None:
    """Given specimens S1..S4, S1 and S2 tagged "fluorescent"
    When tags are queried for containing "fluor"
    Then no specimen matches, because list membership is exact
    """
    hits = drawer.query(S.tags.contains("fluor"))

    assert _nos(hits) == ""


def test_contains_is_index_only_with_no_residual(drawer: dc.Store) -> None:
    """Given specimens S1..S4 with tags
    When the plan for tags containing "rare" is explained
    Then the plan is indexed
    And it has no residual filter
    """
    plan = drawer.explain(S.tags.contains("rare"))

    assert plan.indexed
    assert plan.residual is None


def test_assigning_a_new_list_drops_the_old_tag_postings(drawer: dc.Store) -> None:
    """Given specimens S1..S4, S2 tagged fine and fluorescent
    When S2's tags are reassigned to fine and rare and committed
    Then only S1 is still tagged fluorescent
    """
    s2 = drawer.get(Specimen, specimen_no="S2")
    assert s2 is not None
    s2.tags = ["fine", "rare"]
    drawer.commit()

    hits = drawer.query(S.tags.contains("fluorescent"))

    assert _nos(hits) == "S1"


def test_assigning_a_new_list_adds_the_new_tag_postings(drawer: dc.Store) -> None:
    """Given specimens S1..S4, S2 tagged fine and fluorescent
    When S2's tags are reassigned to fine and rare and committed
    Then S1, S2 and S3 are tagged rare
    """
    s2 = drawer.get(Specimen, specimen_no="S2")
    assert s2 is not None
    s2.tags = ["fine", "rare"]
    drawer.commit()

    hits = drawer.query(S.tags.contains("rare"))

    assert _nos(hits) == "S1, S2, S3"


def test_assigning_a_new_list_keeps_the_shared_tag_postings(drawer: dc.Store) -> None:
    """Given specimens S1..S4, S2 tagged fine and fluorescent
    When S2's tags are reassigned to fine and rare and committed
    Then S2 is still tagged fine
    """
    s2 = drawer.get(Specimen, specimen_no="S2")
    assert s2 is not None
    s2.tags = ["fine", "rare"]
    drawer.commit()

    hits = drawer.query(S.tags.contains("fine"))

    assert _nos(hits) == "S2"


def test_in_place_removal_un_indexes_from_the_prior_snapshot(drawer: dc.Store) -> None:
    """Given specimens S1..S4, S1 tagged museum, fluorescent and rare
    When "rare" is removed from S1's list in place and committed
    Then only S3 is tagged rare, because the index diffs against its own prior copy
    """
    s1 = drawer.get(Specimen, specimen_no="S1")
    assert s1 is not None
    s1.tags.remove("rare")
    drawer.commit()

    hits = drawer.query(S.tags.contains("rare"))

    assert _nos(hits) == "S3"


def test_in_place_removal_keeps_the_other_tags_indexed(drawer: dc.Store) -> None:
    """Given specimens S1..S4, S1 tagged museum, fluorescent and rare
    When "rare" is removed from S1's list in place and committed
    Then S1 is still tagged museum
    """
    s1 = drawer.get(Specimen, specimen_no="S1")
    assert s1 is not None
    s1.tags.remove("rare")
    drawer.commit()

    hits = drawer.query(S.tags.contains("museum"))

    assert _nos(hits) == "S1"


def test_whole_list_equality_is_not_answered_from_the_index(drawer: dc.Store) -> None:
    """Given specimens S1..S4 with tags
    When the plan for tags equal to ["rare"] is explained
    Then the plan is not indexed
    """
    plan = drawer.explain(S.tags == ["rare"])

    assert not plan.indexed


def test_whole_list_equality_compares_the_actual_list(drawer: dc.Store) -> None:
    """Given specimens S1..S4, only S3 tagged exactly ["rare"]
    When tags are queried equal to ["rare"]
    Then only S3 matches, without failing on an unhashable list
    """
    hits = drawer.query(S.tags == ["rare"])

    assert _nos(hits) == "S3"


def test_none_list_values_are_safe_to_query(drawer: dc.Store) -> None:
    """Given specimens S1..S4 where only S1 has aliases and the others hold None
    When aliases are queried for containing "alt"
    Then only S1 matches, and querying does not raise
    """
    hits = drawer.query(S.aliases.contains("alt"))

    assert _nos(hits) == "S1"


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("museum", ""), ("rare", "S3"), ("fluorescent", "S2"), ("fine", "S2")],
)
def test_deleting_a_specimen_clears_its_element_postings(
    drawer: dc.Store, tag: str, expected: str
) -> None:
    """Given specimens S1..S4 with tags
    When S1 is deleted and committed
    Then querying a tag lists only the remaining specimens carrying it
    """
    s1 = drawer.get(Specimen, specimen_no="S1")
    assert s1 is not None
    drawer.delete(s1)
    drawer.commit()

    hits = drawer.query(S.tags.contains(tag))

    assert _nos(hits) == expected


def _stock_and_edit_in_place(store: dc.Store) -> None:
    """Commit A (rare, blue) and B (fine), then turn A's tags into rare and fluorescent."""
    store.root = [
        Specimen(specimen_no="A", tags=["rare", "blue"]),
        Specimen(specimen_no="B", tags=["fine"]),
    ]
    store.commit()
    a = store.root[0]
    a.tags.remove("blue")
    a.tags.append("fluorescent")
    store.commit()


EDITED_ANSWERS = pytest.mark.parametrize(
    ("tag", "expected"),
    [("rare", "A"), ("blue", ""), ("fine", "B"), ("fluorescent", "A")],
)


@EDITED_ANSWERS
def test_incremental_index_follows_assignment_and_in_place_edits(
    store_factory: Callable[[], dc.Store], tag: str, expected: str
) -> None:
    """Given specimens A and B, with A's tags edited in place
    When tags are queried for containing one tag in the same session
    Then the incrementally maintained index answers for the edited lists
    """
    store = store_factory()
    _stock_and_edit_in_place(store)

    hits = store.query(S.tags.contains(tag))

    assert _nos(hits) == expected
    store.close()


@EDITED_ANSWERS
def test_rebuilt_index_equals_the_incremental_one(
    store_factory: Callable[[], dc.Store], tag: str, expected: str
) -> None:
    """Given specimens A and B, with A's tags edited in place, then the store reopened
    When tags are queried for containing one tag
    Then the index rebuilt from a backend scan gives the same answer as the incremental one
    """
    store = store_factory()
    _stock_and_edit_in_place(store)
    store.close()
    reopened = store_factory()

    hits = reopened.query(S.tags.contains(tag))

    assert _nos(hits) == expected
    reopened.close()
