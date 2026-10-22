"""Read captured log records as a message-indexed map of level and ``extra`` fields."""

from __future__ import annotations

import logging
from typing import NamedTuple

import pytest

_BARE_RECORD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None))
) | {"message", "asctime", "taskName"}


class Event(NamedTuple):
    """A captured record reduced to its level and its ``extra`` fields."""

    level: int
    fields: dict[str, object]


def logged(caplog: pytest.LogCaptureFixture) -> dict[str, Event]:
    """Index the captured records by their message."""
    return {
        record.getMessage(): Event(record.levelno, {
            key: value for key, value in vars(record).items()
            if key not in _BARE_RECORD_ATTRIBUTES
        })
        for record in caplog.records
    }
