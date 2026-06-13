"""SIGKILL mid-commit: the reopened delta log is always an exact commit prefix.

The store's own crash gate (test_crash.py) proves acked commits survive; this
proves the attached DeltaLog survives the same kill -9 — its manifest watermark
never lies, partial appends and orphan segments left by the killed commit are
truncated/swept on reopen, and replay reconstructs every durable commit.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

APP = pathlib.Path(__file__).with_name("_deltalog_crash_app.py")


def test_sigkill_mid_commit_leaves_a_replayable_delta_log(tmp_path):
    work = str(tmp_path / "crash")
    writer = subprocess.Popen(
        [sys.executable, str(APP), "write", work],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.8)  # let it ack a few dozen commits
    finally:
        writer.kill()  # SIGKILL — no cleanup, no flushing, no goodbye
    out, err = writer.communicate(timeout=10)
    acked = [int(line) for line in out.split()]
    assert acked, f"writer acked no commits before the kill (stderr: {err})"

    time.sleep(0.3)  # let the dead writer's lease go stale for the verifier
    verify = subprocess.run(
        [sys.executable, str(APP), "verify", work, str(max(acked))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verify.returncode == 0, f"verify failed:\n{verify.stdout}\n{verify.stderr}"
    assert "VERIFY-OK" in verify.stdout
