"""The M2 exit gate: a stateful dirty/commit machine vs a dict oracle.

Hypothesis drives an interleaving of entity creation, scalar writes,
list/dict child mutations (including a nested level), commits and restarts
against one persistent MemoryBackend, while a plain-dict oracle tracks
what the store MUST contain. KICKOFF M2 names this machine — with the
container child-mutation operations — as the exit criterion for the
dirty-tracking deliverables, because silently lost writes were the #1 DX
killer in both ancestor systems (risk 1).

Semantics pinned here:
* buffer-until-commit — a restart discards uncommitted work, exactly;
* commit captures everything reachable+dirty, including child mutations
  through wrapped containers at any depth;
* reopened state equals the oracle's committed state, field by field.
"""

from __future__ import annotations

import copy
from dataclasses import field
from typing import Annotated, Any

from hypothesis import assume, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend


@dc.entity
class CabinetSpecimen:
    """Machine-local specimen (the canonical domain, its own typename so the
    machine never collides with other tests' extents)."""

    catalog_no: Annotated[str, dc.Unique]
    quality: str = "B"
    mass_g: float = 0.0
    tags: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)


_TAGS = ["smoky", "phantom", "rutile", "cluster", "twin"]
_NOTE_KEYS = ["locality", "donor", "drawer"]


class DirtyCommitMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.backend = MemoryBackend()
        self.store = dc.Store._from_backend(self.backend)
        self.committed: dict[str, dict[str, Any]] = {}  # the durable oracle
        self.live: dict[str, dict[str, Any]] = {}       # the in-memory oracle
        self.objs: dict[str, Any] = {}
        self.counter = 0

    keys = Bundle("keys")

    # -- creation -------------------------------------------------------------

    @rule(target=keys, quality=st.sampled_from("ABCD"))
    def create(self, quality: str) -> str:
        key = f"S{self.counter:04d}"
        self.counter += 1
        obj = CabinetSpecimen(catalog_no=key, quality=quality)
        self.store.store(obj)
        self.objs[key] = obj
        self.live[key] = {"quality": quality, "mass_g": 0.0, "tags": [], "notes": {}}
        return key

    # -- scalar writes (the one-shot hook path) --------------------------------

    @rule(key=keys, value=st.floats(allow_nan=False, allow_infinity=False))
    def set_mass(self, key: str, value: float) -> None:
        assume(key in self.live)
        self.objs[key].mass_g = value
        self.live[key]["mass_g"] = value

    @rule(key=keys, quality=st.sampled_from("ABCD"))
    def set_quality(self, key: str, quality: str) -> None:
        assume(key in self.live)
        self.objs[key].quality = quality
        self.live[key]["quality"] = quality

    # -- container child mutations (the owner-bound container path) -----------

    @rule(key=keys, tag=st.sampled_from(_TAGS))
    def append_tag(self, key: str, tag: str) -> None:
        assume(key in self.live)
        self.objs[key].tags.append(tag)
        self.live[key]["tags"].append(tag)

    @rule(key=keys, data=st.data())
    def set_tag_item(self, key: str, data: st.DataObject) -> None:
        assume(key in self.live and self.live[key]["tags"])
        i = data.draw(st.integers(0, len(self.live[key]["tags"]) - 1))
        tag = data.draw(st.sampled_from(_TAGS))
        self.objs[key].tags[i] = tag
        self.live[key]["tags"][i] = tag

    @rule(key=keys)
    def pop_tag(self, key: str) -> None:
        assume(key in self.live and self.live[key]["tags"])
        assert self.objs[key].tags.pop() == self.live[key]["tags"].pop()

    # msgpack ints are 64-bit; out-of-range values are a *rejected commit*
    # (tested in test_three_phase.py), not an oracle case — bound the draws.
    _INTS = st.integers(min_value=-(2**63), max_value=2**64 - 1)

    @rule(key=keys, note_key=st.sampled_from(_NOTE_KEYS), value=_INTS)
    def set_note(self, key: str, note_key: str, value: int) -> None:
        assume(key in self.live)
        self.objs[key].notes[note_key] = value
        self.live[key]["notes"][note_key] = value

    @rule(key=keys, note_key=st.sampled_from(_NOTE_KEYS))
    def del_note(self, key: str, note_key: str) -> None:
        assume(key in self.live and note_key in self.live[key]["notes"])
        del self.objs[key].notes[note_key]
        del self.live[key]["notes"][note_key]

    @rule(key=keys, value=_INTS)
    def mutate_nested_child(self, key: str, value: int) -> None:
        """Two levels down: a list inside a dict inside the entity — the
        recursive wrap must propagate the dirty flip from any depth."""
        assume(key in self.live)
        obj = self.objs[key]
        if "nest" not in self.live[key]["notes"]:
            obj.notes["nest"] = {"deep": []}
            self.live[key]["notes"]["nest"] = {"deep": []}
        obj.notes["nest"]["deep"].append(value)
        self.live[key]["notes"]["nest"]["deep"].append(value)

    # -- the lifecycle rules ----------------------------------------------------

    @rule()
    def commit(self) -> None:
        self.store.commit()
        self.committed = copy.deepcopy(self.live)

    @rule()
    def restart(self) -> None:
        """Close (discarding uncommitted work — pinned semantics) and reopen
        over the same backend; every committed entity must round-trip."""
        self.store.close()
        self.store = dc.Store._from_backend(self.backend)
        self.live = copy.deepcopy(self.committed)
        self.objs = {}
        for key in self.committed:
            obj = self.store.get(CabinetSpecimen, catalog_no=key)
            assert obj is not None, f"{key} was committed but is gone"
            self.objs[key] = obj

    # -- the oracle check (runs after every rule) -------------------------------

    @invariant()
    def live_objects_match_the_oracle(self) -> None:
        for key, expected in self.live.items():
            obj = self.objs[key]
            assert obj.quality == expected["quality"]
            assert obj.mass_g == expected["mass_g"]
            assert list(obj.tags) == expected["tags"]
            assert obj.notes == expected["notes"]

    def teardown(self) -> None:
        self.store.close()


TestDirtyCommitMachine = DirtyCommitMachine.TestCase
TestDirtyCommitMachine.settings = settings(
    max_examples=25, stateful_step_count=40, deadline=None
)
