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


def _first_python_block(readme: str) -> str:
    m = re.search(r"```python\n(.*?)```", readme, re.DOTALL)
    assert m, "README.md must contain a ```python fenced quickstart block"
    return m.group(1)


def _final_int(stdout: str) -> int:
    """The last non-blank stdout line is the run counter (store.root['runs'])."""
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    assert lines, f"quickstart produced no stdout to prove persistence: {stdout!r}"
    last = lines[-1]
    assert last.isdigit(), f"expected the final quickstart line to be the run count, got {last!r}"
    return int(last)


def test_readme_quickstart_runs_twice_and_persists(tmp_path: Path):
    block = _first_python_block(README.read_text())
    script = tmp_path / "quickstart.py"
    script.write_text(block)

    counts: list[int] = []
    for run in (1, 2):
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"README quickstart failed on run {run} (rc={result.returncode}):\n{result.stderr}"
        )
        counts.append(_final_int(result.stdout))

    # Persistence: the second run reopened the first run's store, so the counter advanced.
    assert counts[1] > counts[0], (
        f"README quickstart did not persist across runs — run-count did not increment: {counts}"
    )
