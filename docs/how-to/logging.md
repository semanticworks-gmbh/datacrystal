# How-to: see datacrystal's operational events in your log

After this page you can render datacrystal's log records, with their data fields, in a structlog app.

```bash
pip install datacrystal structlog
```

datacrystal logs through the standard library and configures nothing, so the records go wherever your app sends the `datacrystal` logger.
structlog is not a dependency of datacrystal; your app installs it.

Save this as `cabinet_logging.py` and run it with `python cabinet_logging.py`.

```python
import json
import logging
import sys
import tempfile
from pathlib import Path

import structlog

import datacrystal as dc

shared = [
    structlog.stdlib.add_log_level,
    structlog.stdlib.ExtraAdder(),
    structlog.processors.TimeStamper(fmt="iso"),
]
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(
    structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
)
logging.getLogger().addHandler(handler)
logging.getLogger("datacrystal").setLevel(logging.INFO)

directory = Path(tempfile.mkdtemp()) / "cabinet.store"
directory.mkdir()
(directory / "used.lock").write_text(json.dumps({"pid": 1, "token": "gone", "ts": 1000.0}))

with dc.Store.open(directory, lock_ttl=0.5):
    pass
```

The script prints one JSON line with the event as `event`, the level as `level` and every field of the record beside them:

```json
{"event": "stale_lease_reclaimed", "level": "warning", "reason": "ttl_expired", "lease_ttl_seconds": 0.5, "timestamp": "2026-10-22T13:39:49.416241Z"}
```

`ExtraAdder` is what lifts the record's `extra` fields into the structlog event dict; without it only the message appears.
The `datacrystal` logger carries a `NullHandler` and no level, so an app that configures nothing sees no output, and one that sets a level on `datacrystal` sees exactly the events at or above it.
The logger name, every event and its fields are listed in the [reference](../reference.md#logging).
