"""What the kill -9 crash tests share: start a writer app, read its acks, read a report."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ACKS_BEFORE_KILL = 20


@dataclass
class Writer:
    """A running writer app: its process, its store directory and the acks read so far."""

    process: subprocess.Popen[str]
    work_dir: str
    acked: list[int]


def app_command(app: str, *args: str) -> list[str]:
    """Return the command that runs a crash app module from the repository root."""
    return [sys.executable, "-m", app, *args]


def newest_acked(writer: Writer, remaining_stdout: str) -> int:
    """Return the newest batch or TID the writer acked, before and after the kill."""
    return max(writer.acked + [int(line) for line in remaining_stdout.split()])


def report_of(verifier: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Return the JSON report on the verifier's last stdout line, or an empty report."""
    lines = verifier.stdout.strip().splitlines()
    return json.loads(lines[-1]) if lines else {}
