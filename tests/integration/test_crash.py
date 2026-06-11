"""SIGKILL mid-commit: reopen is always an exact commit prefix (KICKOFF M1).

The full deterministic fault-injection torture harness is an M2 deliverable;
this is the tracer-bullet version with a real SIGKILL — enough to gate the
walking skeleton's durability claim in CI.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

APP = pathlib.Path(__file__).with_name("_crash_app.py")


def test_sigkill_mid_commit_preserves_every_acked_commit(tmp_path):
    store_dir = str(tmp_path / "crash-store")
    writer = subprocess.Popen(
        [sys.executable, str(APP), "write", store_dir],
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
        [sys.executable, str(APP), "verify", store_dir, str(max(acked))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verify.returncode == 0, f"verify failed:\n{verify.stdout}\n{verify.stderr}"
    assert "VERIFY-OK" in verify.stdout
