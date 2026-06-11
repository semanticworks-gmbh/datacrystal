"""Fitness function #2 (M0 five): NO pickle anywhere in the engine.

An AST walk over src/datacrystal forbids importing any module that can
deserialize arbitrary code. This gate exists from the first commit because
the promise is unrecoverable once a format byte ships.
"""

from __future__ import annotations

import ast
import pathlib

FORBIDDEN = {"pickle", "dill", "cloudpickle", "shelve", "joblib", "marshal", "copyreg"}

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "datacrystal"


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module.split(".")[0]


def test_no_pickle_family_imports():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for module in _imports(tree):
            if module in FORBIDDEN:
                offenders.append(f"{path.name}: imports {module}")
    assert not offenders, "pickle-family import found:\n" + "\n".join(offenders)
