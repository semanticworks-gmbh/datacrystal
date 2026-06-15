"""Streamed blob access — read (#84) and write (#85), ADR-007 §3/§4.

The lazy-whole slice (#81-#83, ``test_blob.py``) proved a ``dc.Blob`` field stays
out-of-line and a scan/query never touches the bytes. This module proves the two
*streaming* halves on top of it:

* **Read** — ``store.open_blob(entity, field)`` returns a file-like
  ``io.BufferedReader``: ``seek``/``read(n)``/``tell`` pull only the spanned
  bytes off disk, so peak RSS is the buffer, never the blob (the byte-count gate
  below, invariant 12). It rides a private read view, so it is any-thread-safe
  and tear-free against a concurrent commit. The memory fake has no ``blobopen``,
  so it falls back to a whole ``BytesIO`` (the one backend difference).
* **Write** — assigning a ``dc.BlobSource(size, open_chunks)`` fills a
  ``zeroblob(size)`` cell chunk-by-chunk inside the commit transaction; the bytes
  are never whole in RAM. A wrong size rejects the commit *gaplessly* (the TID
  sequence is untouched, invariant 5).

The domain stays the mineral cabinet: a ``Specimen`` carries the scanned PDF/image
of its label as a streamed blob (the SOR-archive shape from ADR-007's context).
Same-typename fabrication for the gapless-TID test needs the magic-query pragmas.
"""
# pyright: reportCallIssue=false, reportArgumentType=false, reportAttributeAccessIssue=false
# pyright: reportFunctionMemberAccess=false, reportOptionalMemberAccess=false

from __future__ import annotations

import hashlib
import io
import threading
import tracemalloc
from pathlib import Path
from typing import Annotated, Iterator

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend


@dc.entity
class Specimen:
    qid: Annotated[str, dc.Unique]
    label: Annotated[str, dc.Index]
    scan: Annotated[bytes, dc.Blob] = b""          # the label's scanned PDF/image
    thumb: Annotated[bytes | None, dc.Blob] = None


# A ~4 MiB byte-pattern blob, allocated ONCE at import (before any
# tracemalloc.start()), so the RSS gates measure only what streaming allocates,
# not this constant. The pattern makes every range read independently checkable.
BIG = (b"%PDF-1.7\n" + bytes(range(256)) * 16384)
HALF = len(BIG) // 2


def _chunks(data: bytes, size: int = 64 * 1024):
    """A FRESH chunk iterator each call — the re-readable shape BlobSource needs."""
    def factory() -> Iterator[bytes]:
        for i in range(0, len(data), size):
            yield data[i:i + size]
    return factory


def _sqlite_store(tmp_path: Path) -> dc.Store:
    return dc.Store.open(tmp_path / "store", lock_ttl=0.5)


class _SpyBackend(MemoryBackend):
    """Counts whole-value ``load_blob`` calls — the op-count gate for
    'open_blob() never takes the whole-load path' (invariant 12)."""

    def __init__(self) -> None:
        super().__init__()
        self.load_blob_calls = 0

    def load_blob(self, oid: int):
        self.load_blob_calls += 1
        return super().load_blob(oid)


# -- streamed READ (#84) -----------------------------------------------------


def test_open_blob_range_and_whole_read(store_factory):
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=BIG)]
    store.commit()
    store.close()

    reopened = store_factory()
    spec = reopened.root[0]
    with reopened.open_blob(spec, "scan") as fh:
        assert fh.read(9) == b"%PDF-1.7\n"      # head
        assert fh.tell() == 9
        fh.seek(HALF)
        assert fh.read(100) == BIG[HALF:HALF + 100]  # arbitrary range
        fh.seek(-5, io.SEEK_END)
        assert fh.read() == BIG[-5:]            # tail via SEEK_END
    with reopened.open_blob(spec, "scan") as fh:
        assert fh.read() == BIG                 # whole stream == content
    reopened.close()


def test_open_blob_never_takes_the_whole_load_path():
    """open_blob + a range read must NOT call load_blob (the whole fetch);
    .bytes() is the one that does. The contrast is the op-count gate."""
    backend = _SpyBackend()
    store = dc.Store._from_backend(backend)
    store.root = [Specimen(qid="Q1", label="a", scan=BIG)]
    store.commit()
    store.close()

    reopened = dc.Store._from_backend(backend)
    spec = reopened.root[0]
    with reopened.open_blob(spec, "scan") as fh:
        fh.seek(HALF)
        assert fh.read(100) == BIG[HALF:HALF + 100]
    assert backend.load_blob_calls == 0, "open_blob fetched the whole blob"
    assert spec.scan.bytes() == BIG             # .bytes() DOES load whole
    assert backend.load_blob_calls == 1
    reopened.close()


def test_open_blob_peak_rss_bounded_sqlite(tmp_path):
    """sqlite range read peaks far below the blob size — the RSS/byte gate
    (invariant 12). Memory materializes (BytesIO), so this is sqlite-only."""
    store = _sqlite_store(tmp_path)
    store.root = [Specimen(qid="Q1", label="a", scan=BIG)]
    store.commit()
    store.close()

    reopened = _sqlite_store(tmp_path)
    spec = reopened.root[0]
    tracemalloc.start()
    with reopened.open_blob(spec, "scan") as fh:
        fh.seek(HALF)
        chunk = fh.read(100)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert chunk == BIG[HALF:HALF + 100]
    assert peak < len(BIG) // 4, (
        f"a 100-byte range read peaked at {peak} bytes for a {len(BIG)}-byte blob"
    )
    reopened.close()


def test_open_blob_is_safe_from_another_thread(tmp_path):
    """The stream rides a private read view, so reading it off the owner thread
    is allowed (unlike touching the live graph). sqlite-backed."""
    store = _sqlite_store(tmp_path)
    store.root = [Specimen(qid="Q1", label="a", scan=BIG)]
    store.commit()
    spec = store.root[0]
    fh = store.open_blob(spec, "scan")  # resolved on the owner

    out: dict[str, bytes] = {}

    def reader() -> None:
        with fh:
            fh.seek(HALF)
            out["data"] = fh.read(100)

    t = threading.Thread(target=reader)
    t.start()
    t.join()
    assert out["data"] == BIG[HALF:HALF + 100]
    store.close()


def test_open_blob_none_and_non_blob_and_uncommitted(store_factory):
    store = store_factory()
    spec = Specimen(qid="Q1", label="azurite", scan=b"committed", thumb=None)
    store.root = [spec]
    store.commit()

    with pytest.raises(ValueError, match="is None"):
        store.open_blob(store.root[0], "thumb")        # None blob
    with pytest.raises(TypeError, match="not a dc.Blob field"):
        store.open_blob(store.root[0], "label")        # not a blob field

    # An uncommitted raw-bytes assignment streams from an in-memory BytesIO.
    fresh = Specimen(qid="Q2", label="beryl", scan=b"not-yet-saved")
    store.root = [store.root[0], fresh]
    with store.open_blob(fresh, "scan") as fh:
        assert fh.read() == b"not-yet-saved"
    store.close()


def test_open_blob_on_uncommitted_source_demands_commit(store_factory):
    store = store_factory()
    spec = Specimen(qid="Q1", label="a", scan=dc.BlobSource(3, lambda: iter([b"abc"])))
    store.root = [spec]
    with pytest.raises(ValueError, match="commit"):
        store.open_blob(spec, "scan")                  # streamed source, not yet committed
    store.close()


def test_snapshot_open_blob_off_owner(store_factory):
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=BIG)]
    store.commit()

    snap = store.snapshot()
    ev = snap.all(Specimen)[0]
    with snap.open_blob(ev, "scan") as fh:
        fh.seek(HALF)
        assert fh.read(100) == BIG[HALF:HALF + 100]
    with snap.open_blob(ev, "scan") as fh:
        assert fh.read() == BIG
    snap.close()
    store.close()


def test_backend_open_blob_stream_missing_oid_raises(store_factory):
    """A read view streaming a non-existent blob OID raises DanglingRefError
    (the contract both backends share)."""
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="a", scan=b"x")]
    store.commit()
    view = store._backend.read_view()
    try:
        with pytest.raises(dc.DanglingRefError):
            view.open_blob_stream(2**40)               # never-allocated OID
    finally:
        view.close()
    store.close()


# -- streamed WRITE (#85) ----------------------------------------------------


def test_streamed_write_roundtrip_and_swap(store_factory):
    store = store_factory()
    spec = Specimen(qid="Q1", label="azurite", scan=dc.BlobSource(len(BIG), _chunks(BIG)))
    store.root = [spec]
    store.commit()

    # After commit (NO reopen) the consumed source is swapped to a readable
    # handle, so .bytes()/open_blob work immediately (ADR-007 §4).
    assert isinstance(spec.scan, dc.BlobHandle)
    assert spec.scan.size == len(BIG)
    assert spec.scan.hash == hashlib.sha256(BIG).digest()
    assert spec.scan.bytes() == BIG
    with store.open_blob(spec, "scan") as fh:
        assert fh.read() == BIG
    store.close()

    reopened = store_factory()
    assert reopened.root[0].scan.bytes() == BIG        # durable + identical
    reopened.close()


def test_streamed_write_keeps_the_object_record_tiny():
    backend = MemoryBackend()
    store = dc.Store._from_backend(backend)
    store.root = [Specimen(qid="Q1", label="big", scan=dc.BlobSource(len(BIG), _chunks(BIG)))]
    store.commit()
    store.close()

    biggest = max(len(rec.payload) for rec in backend._objects.values())
    assert biggest < 200, f"a streamed-blob record is not tiny: {biggest} bytes"
    assert len(backend._blobs) == 1
    (stored,) = backend._blobs.values()
    assert stored.size == len(BIG)
    assert stored.data == BIG                          # materialized whole in the fake


def test_streamed_write_peak_rss_bounded_sqlite(tmp_path):
    """Committing a multi-MiB BlobSource never holds it whole — peak heap stays
    far below the blob size (the write-side byte gate, sqlite zeroblob fill)."""
    store = _sqlite_store(tmp_path)
    src = dc.BlobSource(len(BIG), _chunks(BIG, size=64 * 1024))
    store.root = [Specimen(qid="Q1", label="a", scan=src)]
    tracemalloc.start()
    store.commit()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < len(BIG) // 4, (
        f"streamed commit peaked at {peak} bytes for a {len(BIG)}-byte blob"
    )
    assert store.root[0].scan.bytes() == BIG           # and it is correct
    store.close()


def test_streamed_write_size_mismatch_rejects_gaplessly(store_factory):
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=b"seed")]
    first = store.commit()

    # A source that lies about its size is rejected BEFORE the TID is taken.
    bad = Specimen(qid="Q2", label="liar",
                   scan=dc.BlobSource(999, lambda: iter([b"only-a-few-bytes"])))
    store.root = [store.root[0], bad]
    with pytest.raises(ValueError, match="declared size"):
        store.commit()

    # The rejected commit consumed NO TID; the entity stays buffered for a
    # fixed-up retry (invariant 5). Fix the source and commit: the next TID is
    # the very next one — the sequence is gapless.
    bad.scan = b"now-correct"
    second = store.commit()
    assert second == first + 1, f"TID gap after a rejected streamed write: {first}->{second}"
    assert {s.qid for s in store.root} == {"Q1", "Q2"}
    store.close()


def test_streamed_write_atomic_on_source_error(store_factory):
    """A source that raises mid-fill rolls the whole commit back: no blob, no
    record, the entity stays buffered, and a retry succeeds (gapless TID)."""
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=b"seed")]
    base = store.commit()

    calls = {"n": 0}

    def flaky() -> Iterator[bytes]:
        calls["n"] += 1
        yield BIG[:1000]
        if calls["n"] == 2:        # pass 1 (hash) ok; pass 2 (fill) explodes
            raise RuntimeError("disk gremlin")
        yield BIG[1000:]

    boom = Specimen(qid="Q2", label="boom", scan=dc.BlobSource(len(BIG), flaky))
    store.root = [store.root[0], boom]
    with pytest.raises(RuntimeError, match="gremlin"):
        store.commit()

    # Nothing from the failed commit is durable; the entity is still buffered.
    store.close()
    reopened = store_factory()
    assert {s.qid for s in reopened.root} == {"Q1"}, "a failed streamed write leaked"
    # The TID sequence is intact: the next commit is base+1.
    reopened.root[0].label = "moved"
    assert reopened.commit() == base + 1
    reopened.close()


def test_streamed_write_past_declared_size_is_caught(store_factory):
    """A non-deterministic source (hashes short, fills long) is caught at the
    fill and rolled back — the cell can never overflow its zeroblob."""
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=b"seed")]
    base = store.commit()

    state = {"calls": 0}

    def growing() -> Iterator[bytes]:
        state["calls"] += 1
        if state["calls"] == 1:
            yield b"x" * 100                 # pass 1: exactly the declared size
        else:
            yield b"x" * 250                 # pass 2: lies, writes past the cell

    store.root = [store.root[0],
                  Specimen(qid="Q2", label="grow", scan=dc.BlobSource(100, growing))]
    with pytest.raises(ValueError, match="declared size|more than"):
        store.commit()
    store.close()

    reopened = store_factory()
    assert {s.qid for s in reopened.root} == {"Q1"}
    reopened.root[0].label = "moved"
    assert reopened.commit() == base + 1     # gapless
    reopened.close()


def test_one_shot_source_fails_loudly(store_factory):
    """A captured single iterator (not a factory) is empty on the second pass —
    caught loudly at the fill, documenting the re-readable requirement."""
    store = store_factory()
    one_shot = iter([b"abc", b"def"])
    store.root = [Specimen(qid="Q1", label="a", scan=dc.BlobSource(6, lambda: one_shot))]
    with pytest.raises(ValueError):
        store.commit()
    store.close()


def test_blob_from_path_roundtrip(store_factory, tmp_path):
    p = tmp_path / "label.pdf"
    p.write_bytes(BIG)
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=dc.blob_from_path(p))]
    store.commit()
    assert store.root[0].scan.bytes() == BIG
    assert store.root[0].scan.size == len(BIG)
    store.close()


def test_negative_size_rejected():
    with pytest.raises(ValueError, match=">= 0"):
        dc.BlobSource(-1, lambda: iter([b""]))


# -- review regressions (adversarial pass, 2026-06-15) -----------------------


def test_content_change_between_passes_rejected_gaplessly(store_factory):
    """A source whose two passes have the SAME size but DIFFERENT bytes would
    store a descriptor hash that lies about the content — the fill pass re-hashes
    and rejects it, gaplessly (review finding A)."""
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="seed", scan=b"seed")]
    base = store.commit()

    state = {"calls": 0}

    def shifty() -> Iterator[bytes]:
        state["calls"] += 1
        yield (b"A" if state["calls"] == 1 else b"B") * 100   # same size, different bytes

    store.root = [store.root[0],
                  Specimen(qid="Q2", label="x", scan=dc.BlobSource(100, shifty))]
    with pytest.raises(ValueError, match="non-deterministic|changed between"):
        store.commit()
    store.close()

    reopened = store_factory()
    assert {s.qid for s in reopened.root} == {"Q1"}, "a lying streamed blob leaked"
    reopened.root[0].label = "moved"
    assert reopened.commit() == base + 1                       # gapless
    reopened.close()


def test_one_source_in_two_fields_gets_distinct_blobs(store_factory):
    """The SAME BlobSource assigned to two dc.Blob fields must give each field
    its OWN blob row + live handle — not alias both to the last OID (finding B)."""
    data = b"shared-scan-bytes" * 256
    store = store_factory()
    src = dc.BlobSource(len(data), _chunks(data))
    spec = Specimen(qid="Q1", label="a", scan=src, thumb=src)   # one source, two fields
    store.root = [spec]
    store.commit()

    assert isinstance(spec.scan, dc.BlobHandle) and isinstance(spec.thumb, dc.BlobHandle)
    assert spec.scan.blob_oid != spec.thumb.blob_oid, "live handles aliased to one OID"
    assert spec.scan.bytes() == data and spec.thumb.bytes() == data
    store.close()

    reopened = store_factory()
    r = reopened.root[0]
    assert r.scan.blob_oid != r.thumb.blob_oid                 # persisted descriptors distinct
    assert r.scan.bytes() == data and r.thumb.bytes() == data
    reopened.close()


def test_seek_past_eof_is_file_like_on_both_backends(store_factory):
    """seek past EOF parks at end and reads empty (standard file semantics) —
    identical on sqlite (clamped) and memory (BytesIO), no backend divergence
    and no raw sqlite error (finding C)."""
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="a", scan=b"0123456789")]
    store.commit()
    with store.open_blob(store.root[0], "scan") as fh:
        assert fh.seek(100) == 100          # past EOF: allowed
        assert fh.read() == b""             # reads nothing
        assert fh.seek(5) == 5              # back in range
        assert fh.read() == b"56789"
    store.close()


def test_stream_outliving_snapshot_raises_store_closed(tmp_path):
    """A blob stream read after its snapshot closed surfaces a DataCrystalError
    (StoreClosedError), never a raw sqlite3 error; close() stays quiet (finding
    D). sqlite-only — memory streams a detached BytesIO copy."""
    store = _sqlite_store(tmp_path)
    store.root = [Specimen(qid="Q1", label="a", scan=BIG)]
    store.commit()
    snap = store.snapshot()
    ev = snap.all(Specimen)[0]
    fh = snap.open_blob(ev, "scan")
    fh.read(4)
    snap.close()                            # close the snapshot out from under the stream
    with pytest.raises(dc.StoreClosedError):
        fh.read()                           # draining past the buffer hits the closed view
    fh.close()                              # must not raise / must not warn
    store.close()


def test_upsert_identical_bytearray_blob_is_a_noop(store_factory):
    """An identical blob re-supplied as a bytearray (not bytes) must still be a
    no-op — the write path accepts bytearray, so equivalence must too (finding
    E); otherwise it spuriously re-stores."""
    data = b"some-image-bytes" * 64
    store = store_factory()
    store.root = []
    store.upsert(Specimen(qid="Q1", label="v1", scan=bytearray(data)))
    store.commit()
    store.close()

    reopened = store_factory()
    oid_before = reopened.get(Specimen, qid="Q1").scan.blob_oid   # cur = hydrated BlobHandle
    reopened.upsert(Specimen(qid="Q1", label="v2", scan=bytearray(data)))  # new = bytearray
    reopened.commit()
    handle = reopened.get(Specimen, qid="Q1").scan
    assert isinstance(handle, dc.BlobHandle)
    assert handle.blob_oid == oid_before, "identical bytearray upsert re-stored the blob"
    reopened.close()
