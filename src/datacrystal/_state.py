"""Entity lifecycle states + the shared dirty-flip helper.

Lives in its own leaf module so both the ``@entity`` ``__setattr__`` hook
(:mod:`datacrystal._entity`) and the persistent containers
(:mod:`datacrystal._containers`) can flip an owner to DIRTY without an
import cycle between them.
"""

from __future__ import annotations

from typing import Any

# Entity lifecycle states (kept as small ints in the __dc_state__ slot).
STATE_NEW = 0      # constructed in this process, not yet committed
STATE_CLEAN = 1    # in sync with the store; write hook armed
STATE_DIRTY = 2    # changed since the last commit; buffered for the next one
STATE_DELETED = 3  # store.delete()d (ADR-003); reads work, writes raise


def touch(obj: Any) -> None:
    """Flip a CLEAN entity to DIRTY via its store, BEFORE the mutation lands.

    The store call enforces the ADR-001 owner-thread check (raising leaves
    the entity unmodified). NEW and DIRTY entities are already buffered, so
    touching them is free. CLEAN implies stamped — the ``__dc_store__`` slot
    is guaranteed to exist. DELETED entities are write-barred here, the one
    choke point both the entity hook and the containers pass through
    (ADR-003: pre-mutation, like every write barrier).
    """
    state = object.__getattribute__(obj, "__dc_state__")
    if state == STATE_DELETED:
        from datacrystal._errors import DeletedEntityError

        raise DeletedEntityError(
            f"this {type(obj).__name__} was deleted via store.delete(); it is "
            "a detached object now — reads work, writes don't; create a new "
            "entity instead"
        )
    if state == STATE_CLEAN:
        store = object.__getattribute__(obj, "__dc_store__")()
        if store is not None:
            store._on_first_write(obj)
        else:
            object.__setattr__(obj, "__dc_state__", STATE_DIRTY)
