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


def _event(record: logging.LogRecord) -> Event:
    fields = {k: v for k, v in vars(record).items() if k not in _BARE_RECORD_ATTRIBUTES}
    return Event(record.levelno, fields)


def logged(caplog: pytest.LogCaptureFixture) -> dict[str, Event]:
    """Index the captured records by their message; the last of equal messages wins."""
    return {record.getMessage(): _event(record) for record in caplog.records}


def logged_all(caplog: pytest.LogCaptureFixture) -> dict[str, list[Event]]:
    """Index every captured record by its message, in order."""
    by_message: dict[str, list[Event]] = {}
    for record in caplog.records:
        by_message.setdefault(record.getMessage(), []).append(_event(record))
    return by_message
