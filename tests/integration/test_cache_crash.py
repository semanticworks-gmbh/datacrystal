"""SIGKILL with an opt-in index cache: a stale/partial sidecar never lies (#63).

The store's own crash gate (test_crash.py) proves acked commits survive kill -9.
This proves that turning on ``cache_index=True`` cannot weaken that: after a
SIGKILL leaves the on-disk cache trailing the committed records (the sidecar is
written only on a clean close — atomic temp+rename, so a kill mid-write leaves the
prior valid sidecar or none, never a torn one), a reopen with the cache enabled
returns answers identical to a reopen without it. The cache is never authoritative
(invariant 11): a watermark/marker mismatch silently rebuilds from the records.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

APP = pathlib.Path(__file__).with_name("_cache_crash_app.py")


def test_sigkill_leaves_a_stale_cache_that_never_lies(tmp_path):
    work = str(tmp_path / "cache-crash")
    writer = subprocess.Popen(
        [sys.executable, str(APP), "write", work],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.8)  # let it ack a few dozen commits past the seeded cache
    finally:
        writer.kill()  # SIGKILL — no clean close, so the sidecar stays stale
    out, err = writer.communicate(timeout=10)
    acked = [int(line) for line in out.split()]
    assert acked, f"writer acked no commits before the kill (stderr: {err})"
    assert max(acked) >= 1, "the writer never got past the seeded cache batch"

    time.sleep(0.3)  # let the dead writer's lease go stale for the verifier
    verify = subprocess.run(
        [sys.executable, str(APP), "verify", work, str(max(acked))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verify.returncode == 0, f"verify failed:\n{verify.stdout}\n{verify.stderr}"
    assert "VERIFY-OK" in verify.stdout
