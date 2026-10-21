"""Helper app for the kill -9 blob crash test (ADR-007 atomicity).

    python _crash_blob_app.py write  <store-dir> [whole|stream]
    python _crash_blob_app.py verify <store-dir>

Each batch commits one Scan whose image is an out-of-line dc.Blob, with the
blob bytes deterministically derived from the batch number. The writer prints
each batch AFTER its commit() returned — every printed batch is acked and must
survive the SIGKILL with BOTH halves intact: the referrer record AND its blob
row (the referrer's handle.bytes() must equal the expected bytes). This is the
ADR-007 claim that a record and its blob land in one atomic transaction.
The verifier reports, as one JSON line, the batches whose blob has the wrong size,
hash or bytes.

``whole`` writes the bytes resident (StoredBlob); ``stream`` writes them via a
``dc.BlobSource`` (zeroblob + incremental blobopen fill, ADR-007 §4) — proving
the streamed fill rides the SAME atomic transaction, so a torn streamed commit
can never leave a half-filled zeroblob behind a surviving record.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Iterator
from typing import Annotated

import datacrystal as dc
from tests.integration._crash_support import ack, emit_report, open_store_when_lease_is_stale


@dc.entity
class Scan:
    """A scan whose image lives out of line."""

    batch: Annotated[int, dc.Index]
    image: Annotated[bytes, dc.Blob] = b""


def _bytes_for(batch: int) -> bytes:
    # A few KB so the bytes genuinely live out-of-line, deterministic per batch.
    return f"batch-{batch}-".encode() * 256


def _scan_of(batch: int, image: bytes | dc.BlobSource) -> Scan:
    # Assigning a dc.BlobSource to a bytes-typed dc.Blob field is the documented
    # write-asymmetry (ADR-007 §4) — untypeable like the magic-query syntax.
    return Scan(batch=batch, image=image)  # pyrefly: ignore[bad-argument-type]


def write(path: str, mode: str = "whole") -> None:
    """Commit one blob-carrying scan per batch forever, printing each acked batch number."""
    store = dc.Store.open(path, lock_ttl=0.5, durability="commit")
    batch = 0
    while True:
        data = _bytes_for(batch)
        if mode == "stream":

            def chunks(data: bytes = data) -> Iterator[bytes]:
                for i in range(0, len(data), 512):
                    yield data[i : i + 512]

            image: bytes | dc.BlobSource = dc.BlobSource(len(data), chunks)
        else:
            image = data
        store.store(_scan_of(batch, image))
        store.commit()
        ack(batch)
        batch += 1


def verify(path: str) -> None:
    """Reopen the killed store and report which surviving scans lost or tore their blob."""
    store = open_store_when_lease_is_stale(path)
    scans = store.query(dc.fields(Scan).batch >= 0)
    by_batch = {s.batch: s for s in scans}
    max_present = max(by_batch, default=-1)
    # Atomicity: every surviving record's blob survives too, byte-for-byte —
    # no torn commit left a referrer pointing at a missing/half-written blob.
    wrong_size, wrong_hash, torn_bytes = [], [], []
    for batch, scan in sorted(by_batch.items()):
        expected = _bytes_for(batch)
        if scan.image.size != len(expected):
            wrong_size.append(batch)
        if scan.image.hash != hashlib.sha256(expected).digest():
            wrong_hash.append(batch)
        if scan.image.bytes() != expected:
            torn_bytes.append(batch)
    store.close()
    emit_report(
        {
            "batch_sequence_has_holes": set(by_batch) != set(range(max_present + 1)),
            "newest_batch": max_present,
            "wrong_size_batches": wrong_size,
            "wrong_hash_batches": wrong_hash,
            "torn_bytes_batches": torn_bytes,
        }
    )


if __name__ == "__main__":
    if sys.argv[1] == "write":
        write(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "whole")
    else:
        verify(sys.argv[2])
