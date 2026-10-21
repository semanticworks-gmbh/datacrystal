"""SIGKILL mid-commit with out-of-line blobs: a referrer record and its blob
row both survive an acked commit, or neither does (ADR-007 atomicity).

The tracer-bullet version of the durability gate, extended to prove the blobs
table rides the SAME atomic transaction as the objects table: a torn commit can
never leave a surviving Scan pointing at a missing or half-written blob.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from tests.integration._crash_harness import ROOT, Writer, app_command, newest_acked, report_of

APP = "tests.integration._crash_blob_app"


@pytest.mark.parametrize("mode", ["whole", "stream"])
def test_sigkill_mid_commit_keeps_record_and_blob_atomic(
    start_writer: Callable[..., Writer], mode: str
) -> None:
    """Given a writer that acked 20 scans with out-of-line blobs, written whole or streamed
    When the writer is SIGKILLed mid-commit and the store is reopened
    Then every surviving scan has its blob whole, byte for byte
    """
    writer = start_writer(APP, mode)

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
    assert report["batch_sequence_has_holes"] is False
    assert report["newest_batch"] >= newest_acked(writer, remaining_stdout)
    assert report["wrong_size_batches"] == []
    assert report["wrong_hash_batches"] == []
    assert report["torn_bytes_batches"] == []
