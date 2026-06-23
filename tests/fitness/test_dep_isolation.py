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


def test_runtime_dependency_budget():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    deps = {d.split(">=")[0].split("==")[0].strip() for d in pyproject["project"]["dependencies"]}
    assert deps == {"msgspec", "pyroaring"}, f"core dependency budget violated: {deps}"


def test_import_isolation_and_lazy_sqlite():
    probe = (
        "import sys; import datacrystal; "
        f"loaded = {BANNED_AT_IMPORT!r} & set(sys.modules); "
        "assert not loaded, f'banned modules at import time: {loaded}'; "
        "print('ISOLATION-OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "ISOLATION-OK" in result.stdout


def test_follower_profile_reaches_contribute_serializer():
    """The ``datacrystal[follower]`` install (pydantic + httpx, NO fastapi /
    strawberry) must be able to import the contribute serializer.

    A follower's ``commit()`` reaches ``datacrystal.web._pydantic.to_pydantic``,
    which first runs the ``datacrystal.web`` package init — that barrel lazily
    imports its fastapi-/strawberry-backed submodules (PEP 562), so a follower-only
    install does not crash on the first contribute (#153 peer-review fix). Simulate
    the profile by blocking ``fastapi``/``strawberry`` at import via ``meta_path``.
    """
    probe = (
        "import sys, importlib.abc\n"
        "BLOCKED = {'fastapi', 'strawberry'}\n"
        "class _Blocker(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ModuleNotFoundError(f'blocked for follower-profile test: {name}')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Blocker())\n"
        "from datacrystal.web._pydantic import to_pydantic\n"
        "assert 'fastapi' not in sys.modules, 'fastapi leaked into the follower path'\n"
        "assert 'strawberry' not in sys.modules, 'strawberry leaked into the follower path'\n"
        "assert callable(to_pydantic)\n"
        "print('FOLLOWER-PROFILE-OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "FOLLOWER-PROFILE-OK" in result.stdout
