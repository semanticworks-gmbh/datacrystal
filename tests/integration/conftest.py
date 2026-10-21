"""Fixtures of the kill -9 crash tests."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, cast

import pytest

from tests.integration._crash_harness import (
    ACKS_BEFORE_KILL,
    ROOT,
    Writer,
    app_command,
)

ACK_DEADLINE_SECONDS = 60.0


def _read_acks(stdout: IO[str], count: int, lines: list[str]) -> None:
    """Append up to ``count`` ack lines to ``lines``; stop at the end of the stream."""
    for _ in range(count):
        line = stdout.readline()
        lines.append(line)
        if not line:
            return


@pytest.fixture
def start_writer(tmp_path: Path) -> Iterator[Callable[..., Writer]]:
    """Yield a function that starts a writer app and returns once it acked 20 commits.

    It waits on the acks the writer prints after each commit returned, never on time, so the
    test kills a writer that is busy committing. Every started writer is killed at teardown.
    """
    started: list[subprocess.Popen[str]] = []
    stderr_files: list[IO[str]] = []

    def start(app: str, *extra_args: str) -> Writer:
        work_dir = str(tmp_path / "crash-store")
        stderr_path = tmp_path / f"writer-{len(started)}.stderr"
        stderr_file = stderr_path.open("w+", encoding="utf-8")
        stderr_files.append(stderr_file)
        process = subprocess.Popen(
            app_command(app, "write", work_dir, *extra_args),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
        )
        started.append(process)
        lines: list[str] = []
        reader = threading.Thread(
            target=_read_acks, args=(cast(IO[str], process.stdout), ACKS_BEFORE_KILL, lines)
        )
        reader.daemon = True
        reader.start()
        reader.join(timeout=ACK_DEADLINE_SECONDS)
        if reader.is_alive() or "" in lines:
            process.kill()
            process.wait(timeout=10)
            reader.join(timeout=10)
            reason = "stalled" if len(lines) < ACKS_BEFORE_KILL else "died"
            pytest.fail(
                f"writer {reason} before acking {ACKS_BEFORE_KILL} commits: "
                f"{stderr_path.read_text(encoding='utf-8')}"
            )
        return Writer(process, work_dir, [int(line) for line in lines])

    yield start
    for process in started:
        process.kill()
        process.communicate(timeout=10)
    for stderr_file in stderr_files:
        stderr_file.close()
