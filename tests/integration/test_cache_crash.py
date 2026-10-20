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

import subprocess
from collections.abc import Callable

from tests.integration._crash_harness import ROOT, Writer, app_command, newest_acked, report_of

APP = "tests.integration._cache_crash_app"


def test_sigkill_leaves_a_stale_cache_that_never_lies(
    start_writer: Callable[..., Writer],
) -> None:
    """Given a writer that seeded an index cache at batch 0 and then acked 19 more batches
    When the writer is SIGKILLed, leaving the cache stale, and the store is reopened with the cache
    Then its count and keys equal those of a reopen without the cache
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
    assert report["cache_count"] == report["records_count"]
    assert report["cache_keys_equal_record_keys"] is True
