"""Pytest plugin for mutmut runs: run the tests of each file in a fixed order.

mutmut hands pytest a set of node ids in arbitrary order.
Some tests depend on the global entity registry that earlier tests fill (for example
``tests/extras/test_fts.py`` fails after ``tests/unit/test_r11_lazy_only.py``),
so an unordered run would kill mutants by test pollution instead of by behaviour.
"""


def pytest_collection_modifyitems(items: list) -> None:
    """Sort the collected items by test file, keeping the order inside a file."""
    items.sort(key=lambda item: str(item.path))
