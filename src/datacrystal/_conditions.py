"""The Condition AST (ROADMAP item 4).

Class-level field access on an ``@entity`` class yields a :class:`FieldExpr`, comparing it
builds a :class:`Pred`, and predicates compose with ``&``, ``|`` and ``~``::

    (Specimen.quality == "museum") & (Specimen.mass_g >= 100.0)

Conditions are single-class by design; mixing two entity classes raises :class:`QueryError`.
The planner resolves indexed predicates to bitmaps and runs the rest as a residual predicate.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Iterable
from itertools import islice
from typing import TYPE_CHECKING, Any, cast

from datacrystal._errors import QueryError

if TYPE_CHECKING:
    from _typeshed import SupportsRichComparison

    from datacrystal._entity import TypeInfo

_ORDER_BY_PAIR_SIZE = 2  # (field, direction)
_ORDERINGS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


class Condition:
    """Base class for all query conditions."""

    __slots__ = ()

    def __and__(self, other: Condition) -> Condition:
        _require_condition(other)
        return And(_flatten(And, self) + _flatten(And, other))

    def __or__(self, other: Condition) -> Condition:
        _require_condition(other)
        return Or(_flatten(Or, self) + _flatten(Or, other))

    def __invert__(self) -> Condition:
        return Not(self)

    # Reflected operands catch the classic mistake
    # ``Cls.field == "x" & (other)``: `&` binds tighter than `==`, so Python
    # evaluates `"x" & condition` and lands here.
    def __rand__(self, other: object) -> Condition:
        _require_condition(other)
        raise AssertionError("unreachable")

    def __ror__(self, other: object) -> Condition:
        _require_condition(other)
        raise AssertionError("unreachable")

    def entity_class(self) -> type:
        classes: set[type] = set()
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

    def evaluate(self, obj: object) -> bool:
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
        return cond.parts
    return (cond,)


def _refuse_float_against_decimal(cls: type, field: str, value: object) -> None:
    """Raise ``TypeError`` for a ``float`` literal against a ``Decimal`` field (#184).

    ``Decimal`` against ``float`` compares the float's binary expansion, so ``== 0.1`` is False
    and a range edge lands on the wrong side; ``Decimal`` and ``int`` literals are exact.
    """
    literals = cast("tuple[object, ...]", value) if isinstance(value, tuple) else (value,)
    if not any(isinstance(v, float) for v in literals):
        return
    from datacrystal._entity import type_info  # lazy: _entity imports us

    spec = type_info(cls).spec(field)
    if spec is not None and spec.decimal:
        raise TypeError(
            f"{cls.__name__}.{field} is a Decimal field; compare it with a Decimal "
            "or int, not a float (a float literal is inexact: "
            "Decimal('0.1') != 0.1)"
        )


class Pred(Condition):
    """A leaf predicate: ``field <op> value``."""

    __slots__ = ("cls", "field", "op", "value")

    # Any: _indexes, _snapshot and _store iterate, hash and order it per `op`.
    value: Any

    _OPS = frozenset({"==", "!=", "<", "<=", ">", ">=", "in",
                      "contains", "startswith"})

    def __init__(self, cls: type, field: str, op: str, value: object) -> None:
        assert op in self._OPS
        _refuse_float_against_decimal(cls, field, value)
        self.cls = cls
        self.field = field
        self.op = op
        self.value = value

    def _collect_classes(self, into: set[type]) -> None:
        into.add(self.cls)

    def evaluate(self, obj: object) -> bool:
        actual = getattr(obj, self.field)
        op = self.op
        if op == "==":
            return actual == self.value
        if op == "!=":
            return actual != self.value
        if op == "in":
            return actual in self.value
        if op == "contains":
            return self._contains(actual)
        if op == "startswith":
            return isinstance(actual, str) and actual.startswith(self.value)
        return self._ordered(actual)

    def _contains(self, actual: object) -> bool:
        """Match a needle against a multi-valued (list) field or a scalar string field.

        String matching is exact and case-sensitive (linguistic matching is the datacrystal[fts]
        extra's job); a non-matching type never matches, like the NULL-like ordering below.
        """
        if isinstance(actual, (list, tuple)):
            return self.value in actual
        return isinstance(actual, str) and self.value in actual

    def _ordered(self, actual: SupportsRichComparison | None) -> bool:
        # Ordering comparisons: None never matches (SQL-NULL-like semantics).
        if actual is None or self.value is None:
            return False
        return _ORDERINGS[self.op](actual, self.value)

    def __repr__(self) -> str:
        return f"({self.cls.__name__}.{self.field} {self.op} {self.value!r})"


class And(Condition):
    __slots__ = ("parts",)

    def __init__(self, parts: tuple[Condition, ...]) -> None:
        self.parts = parts

    def _collect_classes(self, into: set[type]) -> None:
        for p in self.parts:
            p._collect_classes(into)

    def evaluate(self, obj: object) -> bool:
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

    def evaluate(self, obj: object) -> bool:
        return any(p.evaluate(obj) for p in self.parts)

    def __repr__(self) -> str:
        return "(" + " | ".join(map(repr, self.parts)) + ")"


class Not(Condition):
    __slots__ = ("part",)

    def __init__(self, part: Condition) -> None:
        self.part = part

    def _collect_classes(self, into: set[type]) -> None:
        self.part._collect_classes(into)

    def evaluate(self, obj: object) -> bool:
        return not self.part.evaluate(obj)

    def __repr__(self) -> str:
        return f"~{self.part!r}"


def validate_window(limit: int | None, offset: int) -> None:
    """Validate the ``limit=``/``offset=`` window for query()/pluck() and the
    snapshot reads (#14). ``type(...) is not int`` rejects ``bool`` too.
    """
    if type(offset) is not int:
        raise TypeError(f"offset= must be an int, got {type(offset).__name__}")
    if offset < 0:
        raise ValueError(f"offset= must be >= 0, got {offset}")
    if limit is not None:
        if type(limit) is not int:
            raise TypeError(f"limit= must be an int, got {type(limit).__name__}")
        if limit < 0:
            raise ValueError(f"limit= must be >= 0, got {limit}")


def apply_window[T](seq: list[T], limit: int | None, offset: int) -> list[T]:
    """Slice a result list to its ``(offset, limit)`` window. Result order is
    deterministic (ascending OID, preserved by ``get_many``), so a windowed read
    equals the unwindowed read sliced the same way (#14).
    """
    if offset:
        seq = seq[offset:]
    if limit is not None:
        seq = seq[:limit]
    return seq


def window_iter[T](candidate: Iterable[T], limit: int | None, offset: int) -> list[T]:
    """Take the ``(offset, limit)`` window lazily from a sorted candidate iterable (#51).

    Costs O(offset + limit) and equals ``apply_window(list(candidate), limit, offset)``, because
    the roaring candidate iterates in ascending OID.
    """
    stop = None if limit is None else offset + limit
    return list(islice(candidate, offset, stop))


def order_by_values(
    matched: Iterable[int], value_of: Callable[[int], SupportsRichComparison], descending: bool
) -> list[int]:
    """Sort ``matched`` OIDs by ``value_of(oid)`` for an order_by on a non-indexed field (#25).

    NULLs go last. ``matched`` MUST already be ascending-OID so the stable sort keeps OID order
    within equal values (deterministic paging).
    """
    present: list[int] = []
    absent: list[int] = []
    for oid in matched:
        (absent if value_of(oid) is None else present).append(oid)
    present.sort(key=value_of, reverse=descending)
    return present + absent


def parse_order_by(order_by: object, ti: TypeInfo) -> tuple[str, bool]:
    """Resolve the frozen ``order_by`` contract (#25) to ``(field_name, descending)``.

    Accept ``(field, direction)`` or a bare ``field`` (ascending); ``field`` is a
    :class:`FieldExpr` or a name, and ``direction`` is ``'asc'`` or ``'desc'``.
    """
    field_ref: object
    direction: object
    if isinstance(order_by, tuple):
        items = cast("tuple[object, ...]", order_by)
        if len(items) != _ORDER_BY_PAIR_SIZE:
            raise QueryError(
                "order_by=(field, direction) takes a (field, 'asc'|'desc') pair"
            )
        field_ref, direction = items
    else:
        field_ref, direction = order_by, "asc"
    name = field_ref.name if isinstance(field_ref, FieldExpr) else field_ref
    if not isinstance(name, str):
        raise QueryError(
            "order_by field must be a field name or EntityClass.field, "
            f"got {type(field_ref).__name__}"
        )
    field_name: str = name
    if field_name not in ti.field_names:
        raise QueryError(
            f"{ti.cls.__name__} has no persisted field {field_name!r} to order by"
        )
    if direction not in ("asc", "desc"):
        raise QueryError(f"order_by direction must be 'asc' or 'desc', got {direction!r}")
    spec = ti.spec(field_name)
    if spec is not None and spec.multivalued:
        raise QueryError(
            f"{ti.cls.__name__}.{field_name} is a multi-valued (list) field; "
            "order_by needs a single orderable value"
        )
    return field_name, direction == "desc"


def query_target(target: object, method: str) -> tuple[type, Condition | None]:
    """``count()``/``pluck()``/snapshot reads accept an @entity class (the
    whole extent) or a Condition — shared validation for both surfaces.
    """
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

    def __getattr__(self, name: str) -> FieldExpr:
        cls = self._cls
        fieldset = type.__getattribute__(cls, "__dc_fieldset__")
        if name not in fieldset:
            raise AttributeError(f"{cls.__name__} has no persisted field {name!r}")
        return FieldExpr(cls, name)

    def __repr__(self) -> str:
        return f"<fields of {self._cls.__name__}>"


def fields(entity_class: type) -> FieldProxy:
    """Return a statically-typed handle for building query conditions.

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

    def __eq__(self, value: object) -> Pred:  # pyrefly: ignore[bad-override] - builds a predicate, not a bool
        return Pred(self.cls, self.name, "==", value)

    def __ne__(self, value: object) -> Pred:  # pyrefly: ignore[bad-override] - builds a predicate, not a bool
        return Pred(self.cls, self.name, "!=", value)

    def __lt__(self, value: object) -> Pred:
        return Pred(self.cls, self.name, "<", value)

    def __le__(self, value: object) -> Pred:
        return Pred(self.cls, self.name, "<=", value)

    def __gt__(self, value: object) -> Pred:
        return Pred(self.cls, self.name, ">", value)

    def __ge__(self, value: object) -> Pred:
        return Pred(self.cls, self.name, ">=", value)

    def in_(self, values: Iterable[object]) -> Pred:
        return Pred(self.cls, self.name, "in", tuple(values))

    def contains(self, substring: str) -> Pred:
        """Substring match (exact, case-sensitive). On an indexed field the
        planner iterates the index's distinct keys — never entities.
        """
        _require_str(self.cls, self.name, "contains", substring)
        return Pred(self.cls, self.name, "contains", substring)

    def startswith(self, prefix: str) -> Pred:
        """Prefix match (exact, case-sensitive). On an indexed field the
        planner iterates the index's distinct keys — never entities.
        """
        _require_str(self.cls, self.name, "startswith", prefix)
        return Pred(self.cls, self.name, "startswith", prefix)

    __hash__ = None

    def __repr__(self) -> str:
        return f"<field {self.cls.__name__}.{self.name}>"
