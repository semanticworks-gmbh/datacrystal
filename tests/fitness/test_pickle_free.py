"""Fitness function #2 (M0 five): NO pickle anywhere in the engine.

An AST walk over src/datacrystal forbids importing any module that can
deserialize arbitrary code. This gate exists from the first commit because
the promise is unrecoverable once a format byte ships.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

FORBIDDEN = {"pickle", "dill", "cloudpickle", "shelve", "joblib", "marshal", "copyreg"}

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "datacrystal"


def _imports(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module.split(".")[0]


def _pickle_family_imports() -> list[str]:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        offenders.extend(
            f"{path.name}: imports {module}" for module in _imports(tree) if module in FORBIDDEN
        )
    return offenders


def test_no_pickle_family_imports() -> None:
    """Given every module of src/datacrystal
    When their imports are collected
    Then none is a module that deserializes arbitrary code
    """
    offenders = _pickle_family_imports()

    assert offenders == [], "pickle-family import found:\n" + "\n".join(offenders)
