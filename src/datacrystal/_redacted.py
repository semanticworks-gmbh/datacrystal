"""Redacted twins (ADR-008 R14 variant (a)).

Dereferencing a denied-but-existing protected record returns a frozen, field-empty stand-in.
Traversal is graceful; using redacted data raises :class:`ReadDeniedError`.
Twins are per-principal ephemera and never enter the shared identity registry (R14).
Imports ``_entity`` and ``_errors`` only.
"""

from __future__ import annotations

from typing import Any, cast

from datacrystal._entity import EntityMeta, TypeInfo
from datacrystal._errors import ReadDeniedError


class Redacted:
    """Public marker base (``dc.Redacted``); ``isinstance(x, Redacted)`` tells callers to skip field
    access.

    ``isinstance(x, TheEntityClass)`` also holds, so typed call sites keep their ``T``.
    """

    __slots__ = ()


# real class -> twin class, cached forever (per-CLASS, like TYPES_BY_NAME).
# Instances are per-deref — see make_twin — never shared across derefs.
_TWIN_CLS: dict[type, type] = {}


def twin_class(ti: TypeInfo) -> type:
    """Return the per-class redacted-twin type for ``ti``'s entity class, built once and cached.

    The MRO is ``(RedactedX, Redacted, X, ...)``, so ``type_info(twin)`` is ``X``'s protected
    :class:`TypeInfo`.
    That keeps a twin out of any engine ``Lazy`` handle (:meth:`~datacrystal._lazy.Lazy.get` checks
    ``.protected``).
    ``__getattribute__`` intercepts every persisted name, labels and ``dc_permissions`` included.
    """
    cls = _TWIN_CLS.get(ti.cls)
    if cls is not None:
        return cls
    denied = frozenset(ti.field_names) | {"dc_permissions"}

    def _get(self: object, name: str) -> object:
        if name in denied:
            raise ReadDeniedError(
                f"this {ti.cls.__name__} is redacted for the current principal "
                "(ADR-008 R14): traversal is graceful, reading redacted data is "
                "loud — check isinstance(x, dc.Redacted) before using fields"
            )
        return object.__getattribute__(self, name)

    def _set(self: object, name: str, value: object) -> None:
        raise ReadDeniedError(
            f"a redacted {ti.cls.__name__} is frozen and never committable "
            "(ADR-008 R14)"
        )

    def _falsy(self: object) -> bool:
        return False  # the study's falsy-stub semantics

    def _typename(self: object) -> str:
        return ti.typename

    def _twin_repr(self: object) -> str:
        return f"<Redacted {ti.cls.__name__} oid={object.__getattribute__(self, '_dc_twin_oid')}>"

    namespace: dict[str, Any] = {
        "__slots__": ("_dc_twin_oid",),
        "__module__": ti.cls.__module__,
        "__qualname__": f"Redacted[{ti.cls.__qualname__}]",
        "__getattribute__": _get,
        "__setattr__": _set,
        "__bool__": _falsy,
        "typename": property(_typename),
        "__repr__": _twin_repr,
    }
    cls = EntityMeta(f"Redacted{ti.cls.__name__}", (Redacted, ti.cls), namespace)
    _TWIN_CLS[ti.cls] = cls
    return cls


def make_twin(ti: TypeInfo, oid: int) -> Redacted:
    """Build one redacted-twin instance for ``oid``; twins are never cached.

    ``object.__new__`` skips the entity ``__new__`` hook, so no engine slot is set and nothing can
    mistake a twin for a live record.
    Only ``_dc_twin_oid`` is stamped, because the twin's own ``__setattr__`` always raises.
    """
    twin = cast("Redacted", object.__new__(twin_class(ti)))
    object.__setattr__(twin, "_dc_twin_oid", oid)
    return twin
