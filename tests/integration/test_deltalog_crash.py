"""SIGKILL mid-commit: the reopened delta log is always an exact commit prefix.

The store's own crash gate (test_crash.py) proves acked commits survive; this
proves the attached DeltaLog survives the same kill -9 — its manifest watermark
never lies, partial appends and orphan segments left by the killed commit are
truncated/swept on reopen, and replay reconstructs every durable commit.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from tests.integration._crash_harness import ROOT, Writer, app_command, newest_acked, report_of

APP = "tests.integration._deltalog_crash_app"


def test_sigkill_mid_commit_leaves_a_replayable_delta_log(
    start_writer: Callable[..., Writer],
) -> None:
    """Given a writer that acked 20 commits, each fsynced to the delta log before its TID printed
    When the writer is SIGKILLed mid-commit and the log and store are reopened
    Then the log is a gapless commit prefix that replays cleanly and never leads the store
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
    assert report["durable_watermark"] >= newest_acked(writer, remaining_stdout)
    assert report["replay_is_gapless_prefix"] is True
    assert report["applier_watermark"] == report["durable_watermark"]
    assert report["store_last_tid"] >= report["durable_watermark"]
