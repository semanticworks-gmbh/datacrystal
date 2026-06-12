"""The Condition AST (ROADMAP item 4).

Class-level field access on an ``@entity`` class yields a :class:`FieldExpr`
(via the entity metaclass — instance attribute access is untouched and pays
zero overhead). Comparing a ``FieldExpr`` builds a :class:`Pred`; predicates
compose with ``&``, ``|`` and ``~``::

    (Specimen.quality == "museum") & (Specimen.mass_g >= 100.0)

Conditions are **single-class by design** — mixing fields of two entity
classes raises :class:`QueryError` (cross-entity joins are v1, on Arrow
mirrors). Evaluation is split by the store's planner: ``==`` / ``in_`` on
indexed fields resolve to roaring bitmaps; ``contains()`` / ``startswith()``
on indexed string fields iterate the index's *distinct keys* and OR the
matching postings (KICKOFF M4 — O(distinct values), never entities);
everything else runs as a residual Python predicate over the candidate set.
"""

from __future__ import annotations

from typing import Any

from datacrystal._errors import QueryError


class Condition:
    """Base class for all query conditions."""

    __slots__ = ()

    def __and__(self, other: "Condition") -> "Condition":
        _require_condition(other)
        return And(_flatten(And, self) + _flatten(And, other))

    def __or__(self, other: "Condition") -> "Condition":
        _require_condition(other)
        return Or(_flatten(Or, self) + _flatten(Or, other))

    def __invert__(self) -> "Condition":
        return Not(self)

    # Reflected operands catch the classic mistake
    # ``Cls.field == "x" & (other)``: `&` binds tighter than `==`, so Python
    # evaluates `"x" & condition` and lands here.
    def __rand__(self, other: object) -> "Condition":
        _require_condition(other)
        raise AssertionError("unreachable")

    def __ror__(self, other: object) -> "Condition":
        _require_condition(other)
        raise AssertionError("unreachable")

    def entity_class(self) -> type:
        classes = set()
        self._collect_classes(classes)
        if len(classes) != 1:
            names = sorted(c.__name__ for c in classes)
            raise QueryError(
                f"a condition must use fields of exactly one entity class, got {names}; "
                "cross-entity joins are a v1 feature (Arrow mirrors)"
            )
        return classes.pop()

    def _collect_classes(self, into: set[type]) -> None:
        raise NotImplementedError

    def evaluate(self, obj: Any) -> bool:
        raise NotImplementedError


def _require_str(cls: type, field: str, op: str, value: object) -> None:
    if not isinstance(value, str):
        raise QueryError(
            f"{cls.__name__}.{field}.{op}() takes a str, "
            f"got {type(value).__name__}"
        )


def _require_condition(other: object) -> None:
    if not isinstance(other, Condition):
        raise QueryError(
            f"cannot combine a condition with {type(other).__name__!r}; "
            "did you forget parentheses? `&` binds tighter than `==`"
        )


def _flatten(kind: type, cond: Condition) -> tuple[Condition, ...]:
    if type(cond) is kind:
        return cond.parts  # type: ignore[attr-defined]
    return (cond,)


class Pred(Condition):
    """A leaf predicate: ``field <op> value``."""

    __slots__ = ("cls", "field", "op", "value")

    _OPS = frozenset({"==", "!=", "<", "<=", ">", ">=", "in",
                      "contains", "startswith"})

    def __init__(self, cls: type, field: str, op: str, value: Any) -> None:
        assert op in self._OPS
        self.cls = cls
        self.field = field
        self.op = op
        self.value = value

    def _collect_classes(self, into: set[type]) -> None:
        into.add(self.cls)

    def evaluate(self, obj: Any) -> bool:
        actual = getattr(obj, self.field)
        op = self.op
        if op == "==":
            return actual == self.value
        if op == "!=":
            return actual != self.value
        if op == "in":
            return actual in self.value
        # String matching: exact and case-sensitive (linguistic matching is
        # the datacrystal[fts] extra's job); a non-str value never matches,
        # mirroring the SQL-NULL-like ordering semantics below.
        if op == "contains":
            return isinstance(actual, str) and self.value in actual
        if op == "startswith":
            return isinstance(actual, str) and actual.startswith(self.value)
        # Ordering comparisons: None never matches (SQL-NULL-like semantics).
        if actual is None or self.value is None:
            return False
        if op == "<":
            return actual < self.value
        if op == "<=":
            return actual <= self.value
        if op == ">":
            return actual > self.value
        return actual >= self.value

    def __repr__(self) -> str:
        return f"({self.cls.__name__}.{self.field} {self.op} {self.value!r})"


class And(Condition):
    __slots__ = ("parts",)

    def __init__(self, parts: tuple[Condition, ...]) -> None:
        self.parts = parts

    def _collect_classes(self, into: set[type]) -> None:
        for p in self.parts:
            p._collect_classes(into)

    def evaluate(self, obj: Any) -> bool:
        return all(p.evaluate(obj) for p in self.parts)

    def __repr__(self) -> str:
        return "(" + " & ".join(map(repr, self.parts)) + ")"


class Or(Condition):
    __slots__ = ("parts",)

    def __init__(self, parts: tuple[Condition, ...]) -> None:
        self.parts = parts

    def _collect_classes(self, into: set[type]) -> None:
        for p in self.parts:
            p._collect_classes(into)

    def evaluate(self, obj: Any) -> bool:
        return any(p.evaluate(obj) for p in self.parts)

    def __repr__(self) -> str:
        return "(" + " | ".join(map(repr, self.parts)) + ")"


class Not(Condition):
    __slots__ = ("part",)

    def __init__(self, part: Condition) -> None:
        self.part = part

    def _collect_classes(self, into: set[type]) -> None:
        self.part._collect_classes(into)

    def evaluate(self, obj: Any) -> bool:
        return not self.part.evaluate(obj)

    def __repr__(self) -> str:
        return f"~{self.part!r}"


def query_target(target: Any, method: str) -> tuple[type, Condition | None]:
    """``count()``/``pluck()``/snapshot reads accept an @entity class (the
    whole extent) or a Condition — shared validation for both surfaces."""
    if isinstance(target, Condition):
        return target.entity_class(), target
    if isinstance(target, type):
        from datacrystal._entity import type_info  # lazy: _entity imports us

        type_info(target)  # loud for non-entity classes
        return target, None
    raise TypeError(
        f"{method}() takes an @entity class or a Condition, "
        f"got {type(target).__name__}"
    )


class FieldProxy:
    """Typed query-field access for one entity class (see :func:`fields`)."""

    __slots__ = ("_cls",)

    def __init__(self, cls: type) -> None:
        self._cls = cls

    def __getattr__(self, name: str) -> "FieldExpr":
        cls = self._cls
        fieldset = type.__getattribute__(cls, "__dc_fieldset__")
        if name not in fieldset:
            raise AttributeError(f"{cls.__name__} has no persisted field {name!r}")
        return FieldExpr(cls, name)

    def __repr__(self) -> str:
        return f"<fields of {self._cls.__name__}>"


def fields(entity_class: type) -> FieldProxy:
    """A statically-typed handle for building query conditions.

    ``Mineral.mohs >= 6.0`` works at runtime but type checkers infer the
    field's *value* type for class-level access and flag the comparison.
    The proxy route is checker-clean and otherwise identical::

        M = dc.fields(Mineral)
        store.query((M.crystal_system == "cubic") & (M.mohs >= 6.0))
    """
    try:
        type.__getattribute__(entity_class, "__dc_fieldset__")
    except AttributeError:
        from datacrystal._errors import NotAnEntityError

        raise NotAnEntityError(
            f"{entity_class.__name__} is not an @entity class"
        ) from None
    return FieldProxy(entity_class)


class FieldExpr:
    """``EntityClass.field`` — the left-hand side of a predicate."""

    __slots__ = ("cls", "name")

    def __init__(self, cls: type, name: str) -> None:
        self.cls = cls
        self.name = name

    def __eq__(self, value: Any) -> Pred:  # type: ignore[override]
        return Pred(self.cls, self.name, "==", value)

    def __ne__(self, value: Any) -> Pred:  # type: ignore[override]
        return Pred(self.cls, self.name, "!=", value)

    def __lt__(self, value: Any) -> Pred:
        return Pred(self.cls, self.name, "<", value)

    def __le__(self, value: Any) -> Pred:
        return Pred(self.cls, self.name, "<=", value)

    def __gt__(self, value: Any) -> Pred:
        return Pred(self.cls, self.name, ">", value)

    def __ge__(self, value: Any) -> Pred:
        return Pred(self.cls, self.name, ">=", value)

    def in_(self, values: Any) -> Pred:
        return Pred(self.cls, self.name, "in", tuple(values))

    def contains(self, substring: str) -> Pred:
        """Substring match (exact, case-sensitive). On an indexed field the
        planner iterates the index's distinct keys — never entities."""
        _require_str(self.cls, self.name, "contains", substring)
        return Pred(self.cls, self.name, "contains", substring)

    def startswith(self, prefix: str) -> Pred:
        """Prefix match (exact, case-sensitive). On an indexed field the
        planner iterates the index's distinct keys — never entities."""
        _require_str(self.cls, self.name, "startswith", prefix)
        return Pred(self.cls, self.name, "startswith", prefix)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"<field {self.cls.__name__}.{self.name}>"
