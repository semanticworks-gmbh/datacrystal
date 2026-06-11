"""The contract package must stay engine-free (COMMIT-DELTA-v1 spec §1).

KICKOFF M2 names an "engine-free contract/ reference applier": a consumer
must be able to take the applier into any project with msgspec and nothing
else. Like the pickle-free gate, this is an AST walk — mechanical, not
aspirational. The vectors generator is held to the same standard so the
contract can never quietly start depending on what it specifies.
"""

from __future__ import annotations

import ast
from pathlib import Path

CONTRACT = Path("src/datacrystal/contract")

# stdlib used by the contract, plus msgspec, plus the error taxonomy leaf
# (imported in a try/except so a copied-out file degrades to Exception).
_ALLOWED = {
    "__future__", "ast", "hashlib", "json", "pathlib", "struct", "typing",
    "msgspec",
    "datacrystal._errors", "datacrystal.contract", "datacrystal.contract.applier",
}


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_contract_package_imports_no_engine():
    offenders = []
    for path in CONTRACT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for name in _imports(tree):
            root_ok = name in _ALLOWED
            if not root_ok:
                offenders.append(f"{path.name}: import {name}")
    assert not offenders, (
        "the contract package must stay engine-free (msgspec + stdlib + "
        f"the error-taxonomy leaf only), found: {offenders}"
    )
