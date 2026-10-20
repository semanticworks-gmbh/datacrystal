"""SIGKILL mid-commit: reopen is always an exact commit prefix (KICKOFF M1).

The full deterministic fault-injection torture harness is an M2 deliverable;
this is the tracer-bullet version with a real SIGKILL — enough to gate the
walking skeleton's durability claim in CI.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from tests.integration._crash_harness import ROOT, Writer, app_command, newest_acked, report_of

APP = "tests.integration._crash_app"


def test_sigkill_mid_commit_preserves_every_acked_commit(
    start_writer: Callable[..., Writer],
) -> None:
    """Given a writer that acked 20 batches of 10 rows, each after its commit returned
    When the writer is SIGKILLed mid-commit and the store is reopened
    Then every acked batch survived whole
    """
    writer = start_writer(APP)

    writer.process.kill()
    remaining_stdout, _ = writer.process.communicate(timeout=10)
    verifier = subprocess.run(
        app_command(APP, "verify", writer.work_dir),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    report = report_of(verifier)
    assert verifier.returncode == 0, f"verify failed:\n{verifier.stdout}\n{verifier.stderr}"
    assert report["torn_batches"] == []
    assert report["batch_sequence_has_holes"] is False
    assert report["newest_batch"] >= newest_acked(writer, remaining_stdout)
    assert report["rows_in_newest_batch"] == 10
