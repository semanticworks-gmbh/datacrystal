"""Helper app for the kill -9 blob crash test (ADR-007 atomicity).

    python _crash_blob_app.py write  <store-dir>
    python _crash_blob_app.py verify <store-dir> <minimum-batch>

Each batch commits one Scan whose image is an out-of-line dc.Blob, with the
blob bytes deterministically derived from the batch number. The writer prints
each batch AFTER its commit() returned — every printed batch is acked and must
survive the SIGKILL with BOTH halves intact: the referrer record AND its blob
row (the referrer's handle.bytes() must equal the expected bytes). This is the
ADR-007 claim that a record and its blob land in one atomic transaction.
"""

from __future__ import annotations

import hashlib
import sys
from typing import Annotated

import datacrystal as dc


@dc.entity
class Scan:
    batch: Annotated[int, dc.Index]
    image: Annotated[bytes, dc.Blob] = b""


def _bytes_for(batch: int) -> bytes:
    # A few KB so the bytes genuinely live out-of-line, deterministic per batch.
    return (f"batch-{batch}-".encode() * 256)


def write(path: str) -> None:
    store = dc.Store.open(path, lock_ttl=0.5, durability="commit")
    batch = 0
    while True:
        store.store(Scan(batch=batch, image=_bytes_for(batch)))
        store.commit()
        print(batch, flush=True)
        batch += 1


def verify(path: str, minimum_batch: int) -> None:
    store = dc.Store.open(path, lock_ttl=0.1)
    scans = store.query(dc.fields(Scan).batch >= 0)
    by_batch = {s.batch: s for s in scans}
    max_present = max(by_batch, default=-1)
    assert set(by_batch) == set(range(max_present + 1)), "batch sequence has holes"
    assert max_present >= minimum_batch, (
        f"acked batch {minimum_batch} lost; only {max_present} survived"
    )
    # Atomicity: every surviving record's blob survives too, byte-for-byte —
    # no torn commit left a referrer pointing at a missing/half-written blob.
    for batch, scan in by_batch.items():
        expected = _bytes_for(batch)
        assert scan.image.size == len(expected), f"batch {batch} blob size wrong"
        assert scan.image.hash == hashlib.sha256(expected).digest()
        assert scan.image.bytes() == expected, f"batch {batch} blob bytes torn"
    store.close()
    print(f"VERIFY-OK max_batch={max_present}", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "write":
        write(sys.argv[2])
    else:
        verify(sys.argv[2], int(sys.argv[3]))
