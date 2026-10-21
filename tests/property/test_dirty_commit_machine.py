"""The M2 exit gate: a stateful dirty/commit machine vs a dict oracle.

The machine and its dict oracle live in ``_dirty_commit_machine.py``: Hypothesis reports a
broken rule or invariant by the exception it raises, so the machine asserts as it runs.
"""

from __future__ import annotations

from hypothesis import settings

from tests.property._dirty_commit_machine import DirtyCommitMachine

TestDirtyCommitMachine = DirtyCommitMachine.TestCase
TestDirtyCommitMachine.settings = settings(max_examples=25, stateful_step_count=40, deadline=None)
