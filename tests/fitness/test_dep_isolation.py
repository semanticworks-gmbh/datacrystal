"""Fitness function #3 (M0 five): the dependency budget is a one-way door.

* ``[project.dependencies]`` must be exactly {msgspec, pyroaring}.
* ``import datacrystal`` must not drag in any extra's heavyweight modules,
  and must load sqlite3 lazily (only when a store is opened).
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]

BANNED_AT_IMPORT = {
    "pyarrow", "duckdb", "polars", "usearch", "pydantic", "numpy",
    "psutil", "fastapi", "strawberry", "pandas", "sqlite3",
    # the follower's HTTP transport (datacrystal[follower]) is imported lazily,
    # inside open_follower's fetch — never at `import datacrystal` (#151).
    "httpx", "httpcore", "h11", "requests", "urllib3", "aiohttp",
}


def test_runtime_dependency_budget_is_msgspec_and_pyroaring() -> None:
    """Given the project's pyproject.toml
    When its runtime dependencies are read
    Then they are exactly msgspec and pyroaring
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    dependencies = pyproject["project"]["dependencies"]

    assert {d.split(">=")[0].split("==")[0].strip() for d in dependencies} == {
        "msgspec",
        "pyroaring",
    }


def test_import_loads_no_extra_dependency_and_no_sqlite() -> None:
    """Given a fresh interpreter
    When it imports datacrystal
    Then none of the extras' heavyweight modules, nor sqlite3, is loaded
    """
    probe = (
        "import sys; import datacrystal; "
        f"loaded = {BANNED_AT_IMPORT!r} & set(sys.modules); "
        "assert not loaded, f'banned modules at import time: {loaded}'; "
        "print('ISOLATION-OK')"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr
    assert "ISOLATION-OK" in result.stdout


def test_follower_profile_contributes_without_fastapi_or_strawberry() -> None:
    """Given an interpreter that blocks fastapi and strawberry at import
    When a follower contributes a new entity through a fake coordinator client
    Then the contribution is sent and neither blocked module is loaded

    The ``datacrystal[follower]`` install (pydantic + httpx, NO fastapi / strawberry) must be
    able to commit on a follower. The first contribute reaches the pydantic serializer through
    the ``datacrystal.web`` package init, which lazily imports its fastapi-/strawberry-backed
    submodules (PEP 562), so a follower-only install does not crash on it (#153 peer-review fix).
    """
    probe = (
        "import sys, importlib.abc\n"
        "from typing import Annotated\n"
        "BLOCKED = {'fastapi', 'strawberry'}\n"
        "class _Blocker(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ModuleNotFoundError(f'blocked for follower-profile test: {name}')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Blocker())\n"
        "import datacrystal as dc\n"
        "@dc.entity\n"
        "class Mineral:\n"
        "    qid: Annotated[str, dc.Unique]\n"
        "    name: str\n"
        "class Reply:\n"
        "    content = b'{\"applied_tid\": 1}'\n"
        "    status_code = 200\n"
        "    def raise_for_status(self): return None\n"
        "    def json(self): return {'applied_tid': 1}\n"
        "class NoDeltas(Reply):\n"
        "    content = b''\n"
        "submitted = []\n"
        "class Coordinator:\n"
        "    def get(self, url, /, *, params): return NoDeltas()\n"
        "    def post(self, url, /, *, json):\n"
        "        submitted.append(json)\n"
        "        return Reply()\n"
        "follower = dc.Store.follower('http://coordinator', client=Coordinator())\n"
        "follower.store(Mineral(qid='Q1', name='quartz'))\n"
        "follower.commit()\n"
        "assert len(submitted) == 1, submitted\n"
        "assert 'fastapi' not in sys.modules, 'fastapi leaked into the follower path'\n"
        "assert 'strawberry' not in sys.modules, 'strawberry leaked into the follower path'\n"
        "print('FOLLOWER-PROFILE-OK')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr
    assert "FOLLOWER-PROFILE-OK" in result.stdout
