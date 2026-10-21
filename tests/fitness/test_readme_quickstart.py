"""Fitness gate (#119): the README quickstart runs verbatim, twice, and persists.

CLAUDE.md's testing conventions promise "the README quickstart must run verbatim, twice,
from a clean directory." Until now ci.yml only ran the examples/ demos, not the README
block — so the README could rot silently. This gate puts that promise under CI:

* extract the first ```python fenced block from README.md verbatim,
* write it into a fresh temp dir and run it TWICE via a subprocess,
* assert returncode 0 both runs (it runs verbatim), AND
* assert persistence is proven — the README's run-count line increments across runs
  (the second run reopens the first run's store rather than starting over).

The quickstart prints, as its final stdout line, ``store.root["runs"]`` — 1 on the first
run, 2 on the second. We parse that to prove the store survived between processes.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"


def _first_python_block(readme: str) -> str | None:
    match = re.search(r"```python\n(.*?)```", readme, re.DOTALL)
    return match.group(1) if match else None


def _last_line(stdout: str) -> str:
    """Return the last non-blank stdout line, the run counter (store.root['runs'])."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def test_readme_quickstart_runs_twice_and_persists(tmp_path: Path) -> None:
    """Given the README's first python block saved as a script in a clean directory
    When the script runs twice in that directory
    Then the second run reopens the first run's store, so the run counter reads 2
    """
    block = _first_python_block(README.read_text())
    assert block is not None, "README.md must contain a ```python fenced quickstart block"
    script = tmp_path / "quickstart.py"
    script.write_text(block)

    first_run = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    second_run = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert first_run.returncode == 0, f"first run failed:\n{first_run.stderr}"
    assert second_run.returncode == 0, f"second run failed:\n{second_run.stderr}"
    assert _last_line(first_run.stdout) == "1"
    assert _last_line(second_run.stdout) == "2", "the run counter did not persist across runs"
