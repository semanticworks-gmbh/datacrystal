"""SIGKILL mid-commit with out-of-line blobs: a referrer record and its blob
row both survive an acked commit, or neither does (ADR-007 atomicity).

The tracer-bullet version of the durability gate, extended to prove the blobs
table rides the SAME atomic transaction as the objects table: a torn commit can
never leave a surviving Scan pointing at a missing or half-written blob.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

APP = pathlib.Path(__file__).with_name("_crash_blob_app.py")


def test_sigkill_mid_commit_keeps_record_and_blob_atomic(tmp_path):
    store_dir = str(tmp_path / "crash-blob-store")
    writer = subprocess.Popen(
        [sys.executable, str(APP), "write", store_dir],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.8)  # let it ack a handful of blob commits
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
