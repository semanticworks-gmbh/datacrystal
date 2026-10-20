"""Fixtures of the kill -9 crash tests."""

from __future__ import annotations

import subprocess
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


@pytest.fixture
def start_writer(tmp_path: Path) -> Iterator[Callable[..., Writer]]:
    """Yield a function that starts a writer app and returns once it acked 20 commits.

    It waits on the acks the writer prints after each commit returned, never on time, so the
    test kills a writer that is busy committing. Every started writer is killed at teardown.
    """
    started: list[subprocess.Popen[str]] = []

    def start(app: str, *extra_args: str) -> Writer:
        work_dir = str(tmp_path / "crash-store")
        process = subprocess.Popen(
            app_command(app, "write", work_dir, *extra_args),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        started.append(process)
        stdout = cast(IO[str], process.stdout)
        lines = [stdout.readline() for _ in range(ACKS_BEFORE_KILL)]
        if "" in lines:
            process.kill()
            _, stderr = process.communicate(timeout=10)
            pytest.fail(f"writer died before acking {ACKS_BEFORE_KILL} commits: {stderr}")
        return Writer(process, work_dir, [int(line) for line in lines])

    yield start
    for process in started:
        process.kill()
        process.communicate(timeout=10)
