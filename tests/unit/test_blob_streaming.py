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
"""

from __future__ import annotations

import hashlib
import io
import threading
import tracemalloc
import warnings
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Annotated, cast

import pytest

import datacrystal as dc
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import StoredBlob

StoreFactory = Callable[[], dc.Store]
ChunkFactory = Callable[[], Iterator[bytes]]


@dc.entity
class Specimen:
    """A specimen whose label is scanned to a PDF or image, kept as a streamed blob."""

    qid: Annotated[str, dc.Unique]
    label: Annotated[str, dc.Index]
    scan: Annotated[bytes, dc.Blob] = b""  # the label's scanned PDF/image
    thumb: Annotated[bytes | None, dc.Blob] = None


# A ~4 MiB byte-pattern blob, allocated ONCE at import (before any
# tracemalloc.start()), so the RSS gates measure only what streaming allocates,
# not this constant. The pattern makes every range read independently checkable.
BIG = b"%PDF-1.7\n" + bytes(range(256)) * 16384
HALF = len(BIG) // 2


def _chunks(data: bytes, size: int = 64 * 1024) -> ChunkFactory:
    """Return a factory of FRESH chunk iterators, the re-readable shape BlobSource needs."""

    def factory() -> Iterator[bytes]:
        for i in range(0, len(data), size):
            yield data[i : i + size]

    return factory


def _source(size: int, open_chunks: ChunkFactory) -> bytes:
    """Return a BlobSource typed as the bytes field it is assigned to."""
    return cast("bytes", dc.BlobSource(size, open_chunks))


def _handle(blob_field: object) -> dc.BlobHandle:
    """Return a blob field as the handle a stored specimen hydrates to (typed bytes)."""
    return cast("dc.BlobHandle", blob_field)


def _abc_chunks() -> Iterator[bytes]:
    return iter([b"abc"])


def _few_bytes_chunks() -> Iterator[bytes]:
    return iter([b"only-a-few-bytes"])


def _no_chunks() -> Iterator[bytes]:
    return iter([b""])


def _chunks_failing_on_the_fill_pass() -> ChunkFactory:
    """Return a source that hashes fine on pass 1 and raises on pass 2, the fill."""
    calls = {"n": 0}

    def flaky() -> Iterator[bytes]:
        calls["n"] += 1
        yield BIG[:1000]
        if calls["n"] == 2:
            raise RuntimeError("disk gremlin")
        yield BIG[1000:]

    return flaky


def _chunks_growing_on_the_fill_pass() -> ChunkFactory:
    """Return a source that is exactly 100 bytes on pass 1 and 250 bytes on pass 2."""
    calls = {"n": 0}

    def growing() -> Iterator[bytes]:
        calls["n"] += 1
        yield b"x" * (100 if calls["n"] == 1 else 250)

    return growing


def _chunks_changing_content_between_passes() -> ChunkFactory:
    """Return a 100-byte source of A on pass 1 and B on pass 2: same size, other bytes."""
    calls = {"n": 0}

    def shifty() -> Iterator[bytes]:
        calls["n"] += 1
        yield (b"A" if calls["n"] == 1 else b"B") * 100

    return shifty


def _one_shot_chunks() -> ChunkFactory:
    """Return a 'factory' that hands out the same single iterator, empty on the 2nd pass."""
    one_shot = iter([b"abc", b"def"])

    def factory() -> Iterator[bytes]:
        return one_shot

    return factory


class _SpyBackend(MemoryBackend):
    """Counts whole-value ``load_blob`` calls.

    It is the op-count gate for 'open_blob() never takes the whole-load path' (invariant 12).
    """

    def __init__(self) -> None:
        super().__init__()
        self.load_blob_calls = 0

    def load_blob(self, oid: int) -> StoredBlob | None:
        self.load_blob_calls += 1
        return super().load_blob(oid)


@pytest.fixture
def reopened_big(store_factory: StoreFactory) -> dc.Store:
    """Return a reopened store holding one azurite specimen with the 4 MiB scan."""
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=BIG)]
    store.commit()
    store.close()
    return store_factory()


@pytest.fixture
def sqlite_big(tmp_path: Path) -> dc.Store:
    """Return a reopened sqlite store holding one specimen with the 4 MiB scan."""
    store = dc.Store.open(tmp_path / "store", lock_ttl=0.5)
    store.root = [Specimen(qid="Q1", label="a", scan=BIG)]
    store.commit()
    store.close()
    return dc.Store.open(tmp_path / "store", lock_ttl=0.5)


@pytest.fixture
def seeded(store_factory: StoreFactory) -> tuple[dc.Store, int]:
    """Return a store with specimen Q1 committed, and the TID of that commit."""
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=b"seed")]
    tid = store.commit()
    if tid is None:
        pytest.fail("the seed commit returned no transaction id")
    return store, tid


# -- streamed READ (#84) -----------------------------------------------------


def test_open_blob_reads_the_head(reopened_big: dc.Store) -> None:
    """Given a reopened store with a 4 MiB scan
    When the first 9 bytes of the scan are read through open_blob
    Then they are the PDF header
    And the stream position is 9
    """
    with reopened_big.open_blob(reopened_big.root[0], "scan") as fh:
        head = fh.read(9)

        assert head == b"%PDF-1.7\n"
        assert fh.tell() == 9
    reopened_big.close()


def test_open_blob_reads_an_arbitrary_range(reopened_big: dc.Store) -> None:
    """Given a reopened store with a 4 MiB scan
    When 100 bytes are read from the middle of the scan through open_blob
    Then they equal that range of the original
    """
    with reopened_big.open_blob(reopened_big.root[0], "scan") as fh:
        fh.seek(HALF)

        chunk = fh.read(100)

        assert chunk == BIG[HALF : HALF + 100]
    reopened_big.close()


def test_open_blob_reads_the_tail_from_seek_end(reopened_big: dc.Store) -> None:
    """Given a reopened store with a 4 MiB scan
    When the stream seeks 5 bytes before the end and reads on
    Then it reads the last 5 bytes of the original
    """
    with reopened_big.open_blob(reopened_big.root[0], "scan") as fh:
        fh.seek(-5, io.SEEK_END)

        tail = fh.read()

        assert tail == BIG[-5:]
    reopened_big.close()


def test_open_blob_whole_stream_equals_the_content(reopened_big: dc.Store) -> None:
    """Given a reopened store with a 4 MiB scan
    When the whole scan is read through open_blob
    Then it equals the original
    """
    with reopened_big.open_blob(reopened_big.root[0], "scan") as fh:
        whole = fh.read()

        assert whole == BIG
    reopened_big.close()


@pytest.fixture
def spied_big() -> tuple[dc.Store, _SpyBackend]:
    """Return a reopened store over a spy backend holding one specimen with the 4 MiB scan."""
    backend = _SpyBackend()
    store = dc.Store._from_backend(backend)
    store.root = [Specimen(qid="Q1", label="a", scan=BIG)]
    store.commit()
    store.close()
    return dc.Store._from_backend(backend), backend


def test_open_blob_range_read_never_takes_the_whole_load_path(
    spied_big: tuple[dc.Store, _SpyBackend],
) -> None:
    """Given a reopened store over a spy backend with a 4 MiB scan
    When a 100-byte range is read through open_blob
    Then the backend served no whole blob load
    """
    reopened, backend = spied_big
    with reopened.open_blob(reopened.root[0], "scan") as fh:
        fh.seek(HALF)

        fh.read(100)

    assert backend.load_blob_calls == 0
    reopened.close()


def test_bytes_does_take_the_whole_load_path(spied_big: tuple[dc.Store, _SpyBackend]) -> None:
    """Given a reopened store over a spy backend with a 4 MiB scan
    When the scan's bytes are read through the handle
    Then they are the original
    And the backend served one whole blob load
    """
    reopened, backend = spied_big

    whole = _handle(reopened.root[0].scan).bytes()

    assert whole == BIG
    assert backend.load_blob_calls == 1
    reopened.close()


def test_open_blob_range_read_peaks_far_below_the_blob_size_on_sqlite(sqlite_big: dc.Store) -> None:
    """Given a reopened sqlite store with a 4 MiB scan
    When a 100-byte range is read through open_blob under tracemalloc
    Then the chunk equals that range of the original
    And the peak heap is under a quarter of the blob size
    """
    spec = sqlite_big.root[0]
    tracemalloc.start()
    with sqlite_big.open_blob(spec, "scan") as fh:
        fh.seek(HALF)
        chunk = fh.read(100)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert chunk == BIG[HALF : HALF + 100]
    assert peak < len(BIG) // 4
    sqlite_big.close()


def test_open_blob_is_readable_from_another_thread(sqlite_big: dc.Store) -> None:
    """Given a sqlite store with a 4 MiB scan and a stream opened on the owner thread
    When another thread seeks into the stream and reads 100 bytes
    Then it reads that range of the original
    """
    fh = sqlite_big.open_blob(sqlite_big.root[0], "scan")
    out: list[bytes] = []

    def reader() -> None:
        with fh:
            fh.seek(HALF)
            out.append(fh.read(100))

    thread = threading.Thread(target=reader)
    thread.start()
    thread.join(timeout=30)

    assert out == [BIG[HALF : HALF + 100]]
    sqlite_big.close()


def test_open_blob_on_a_none_blob_raises(store_factory: StoreFactory) -> None:
    """Given a committed specimen whose thumb is None
    When open_blob is asked for the thumb
    Then ValueError says the blob "is None"
    """
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=b"committed", thumb=None)]
    store.commit()

    with pytest.raises(ValueError, match=r"is None"):
        store.open_blob(store.root[0], "thumb")
    store.close()


def test_open_blob_on_a_non_blob_field_raises(store_factory: StoreFactory) -> None:
    """Given a committed specimen
    When open_blob is asked for its label
    Then TypeError says it is "not a dc.Blob field"
    """
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=b"committed", thumb=None)]
    store.commit()

    with pytest.raises(TypeError, match=r"not a dc.Blob field"):
        store.open_blob(store.root[0], "label")
    store.close()


def test_open_blob_streams_an_uncommitted_raw_bytes_assignment(
    store_factory: StoreFactory,
) -> None:
    """Given a store with a committed specimen and a new uncommitted specimen with raw bytes
    When the new specimen's scan is read through open_blob
    Then the stream yields those bytes from memory
    """
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=b"committed")]
    store.commit()
    fresh = Specimen(qid="Q2", label="beryl", scan=b"not-yet-saved")
    store.root = [store.root[0], fresh]

    with store.open_blob(fresh, "scan") as fh:
        streamed = fh.read()

    assert streamed == b"not-yet-saved"
    store.close()


def test_open_blob_on_an_uncommitted_source_demands_a_commit(store_factory: StoreFactory) -> None:
    """Given a specimen whose scan is a BlobSource that was not committed yet
    When open_blob is asked for the scan
    Then ValueError tells to commit first
    """
    store = store_factory()
    spec = Specimen(qid="Q1", label="a", scan=_source(3, _abc_chunks))
    store.root = [spec]

    with pytest.raises(ValueError, match=r"commit"):
        store.open_blob(spec, "scan")
    store.close()


def test_snapshot_open_blob_reads_a_range(store_factory: StoreFactory) -> None:
    """Given a committed 4 MiB scan and a snapshot of the store
    When 100 bytes from the middle are read through the snapshot's open_blob
    Then they equal that range of the original
    """
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=BIG)]
    store.commit()
    snap = store.snapshot()
    view = snap.all(Specimen)[0]
    with snap.open_blob(view, "scan") as fh:
        fh.seek(HALF)

        chunk = fh.read(100)

        assert chunk == BIG[HALF : HALF + 100]
    snap.close()
    store.close()


def test_snapshot_open_blob_reads_the_whole_value(store_factory: StoreFactory) -> None:
    """Given a committed 4 MiB scan and a snapshot of the store
    When the whole scan is read through the snapshot's open_blob
    Then it equals the original
    """
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=BIG)]
    store.commit()
    snap = store.snapshot()
    view = snap.all(Specimen)[0]
    with snap.open_blob(view, "scan") as fh:
        whole = fh.read()

        assert whole == BIG
    snap.close()
    store.close()


def test_backend_open_blob_stream_missing_oid_raises(store_factory: StoreFactory) -> None:
    """Given a store and a read view of its backend
    When the view streams a blob oid that was never allocated
    Then DanglingRefError is raised
    """
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="a", scan=b"x")]
    store.commit()

    with closing(store._backend.read_view()) as view, pytest.raises(dc.DanglingRefError):
        view.open_blob_stream(2**40)
    store.close()


# -- streamed WRITE (#85) ----------------------------------------------------


@pytest.fixture
def streamed_big(store_factory: StoreFactory) -> tuple[dc.Store, Specimen]:
    """Return a store, and a specimen whose 4 MiB scan was committed from a BlobSource."""
    store = store_factory()
    spec = Specimen(qid="Q1", label="azurite", scan=_source(len(BIG), _chunks(BIG)))
    store.root = [spec]
    store.commit()
    return store, spec


def test_streamed_write_swaps_the_source_to_a_handle_without_reopen(
    streamed_big: tuple[dc.Store, Specimen],
) -> None:
    """Given a specimen committed with a BlobSource scan, the store not reopened
    When the scan is read
    Then it is a BlobHandle
    And its size is the blob size
    And its hash is the sha256 of the blob
    """
    store, spec = streamed_big

    handle = spec.scan

    assert isinstance(handle, dc.BlobHandle)
    assert handle.size == len(BIG)
    assert handle.hash == hashlib.sha256(BIG).digest()
    store.close()


def test_streamed_write_is_readable_whole_without_reopen(
    streamed_big: tuple[dc.Store, Specimen],
) -> None:
    """Given a specimen committed with a BlobSource scan, the store not reopened
    When the handle's bytes are read
    Then they equal the original
    """
    store, spec = streamed_big

    whole = _handle(spec.scan).bytes()

    assert whole == BIG
    store.close()


def test_streamed_write_is_readable_through_open_blob_without_reopen(
    streamed_big: tuple[dc.Store, Specimen],
) -> None:
    """Given a specimen committed with a BlobSource scan, the store not reopened
    When the scan is read through open_blob
    Then it equals the original
    """
    store, spec = streamed_big

    with store.open_blob(spec, "scan") as fh:
        whole = fh.read()

    assert whole == BIG
    store.close()


def test_streamed_write_is_durable_and_identical_after_reopen(
    store_factory: StoreFactory, streamed_big: tuple[dc.Store, Specimen]
) -> None:
    """Given a specimen committed with a BlobSource scan and the store closed
    When the store is reopened and the scan's bytes are read
    Then they equal the original
    """
    store, _ = streamed_big
    store.close()
    reopened = store_factory()

    whole = _handle(reopened.root[0].scan).bytes()

    assert whole == BIG
    reopened.close()


@pytest.fixture
def streamed_big_on_memory_backend() -> tuple[MemoryBackend, dc.Store]:
    """Return a memory backend and its closed store, committed from a BlobSource 4 MiB scan."""
    backend = MemoryBackend()
    store = dc.Store._from_backend(backend)
    store.root = [Specimen(qid="Q1", label="big", scan=_source(len(BIG), _chunks(BIG)))]
    store.commit()
    store.close()
    return backend, store


def test_streamed_write_keeps_the_object_record_tiny(
    streamed_big_on_memory_backend: tuple[MemoryBackend, dc.Store],
) -> None:
    """Given a 4 MiB scan committed from a BlobSource
    When the stored object records are measured
    Then the largest record is under 200 bytes
    """
    backend, _ = streamed_big_on_memory_backend

    assert max(len(rec.payload) for rec in backend._objects.values()) < 200


def test_streamed_write_stores_one_whole_blob_row(
    streamed_big_on_memory_backend: tuple[MemoryBackend, dc.Store],
) -> None:
    """Given a 4 MiB scan committed from a BlobSource
    When the blobs table is read
    Then it holds exactly one row
    And that row has the blob size
    And the memory fake materialized it whole
    """
    backend, _ = streamed_big_on_memory_backend

    (stored,) = backend._blobs.values()

    assert len(backend._blobs) == 1
    assert stored.size == len(BIG)
    assert stored.data == BIG


def test_streamed_commit_peaks_far_below_the_blob_size_on_sqlite(tmp_path: Path) -> None:
    """Given a store with a specimen whose 4 MiB scan is a BlobSource
    When it is committed under tracemalloc
    Then the peak heap is under a quarter of the blob size
    """
    store = dc.Store.open(tmp_path / "store", lock_ttl=0.5)
    src = _source(len(BIG), _chunks(BIG, size=64 * 1024))
    store.root = [Specimen(qid="Q1", label="a", scan=src)]
    tracemalloc.start()
    store.commit()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < len(BIG) // 4
    store.close()


def test_streamed_commit_on_sqlite_stores_the_correct_bytes(tmp_path: Path) -> None:
    """Given a store with a specimen whose 4 MiB scan is a BlobSource, committed
    When the scan's bytes are read
    Then they equal the original
    """
    store = dc.Store.open(tmp_path / "store", lock_ttl=0.5)
    store.root = [Specimen(qid="Q1", label="a", scan=_source(len(BIG), _chunks(BIG)))]
    store.commit()

    whole = _handle(store.root[0].scan).bytes()

    assert whole == BIG
    store.close()


def test_a_source_lying_about_its_size_is_rejected(seeded: tuple[dc.Store, int]) -> None:
    """Given a committed specimen Q1
    When a specimen whose BlobSource declares 999 bytes but yields fewer is committed
    Then ValueError names the declared size
    """
    store, _ = seeded
    liar = Specimen(qid="Q2", label="liar", scan=_source(999, _few_bytes_chunks))
    store.root = [store.root[0], liar]

    with pytest.raises(ValueError, match=r"declared size"):
        store.commit()
    store.close()


def test_a_rejected_size_mismatch_burns_no_tid(seeded: tuple[dc.Store, int]) -> None:
    """Given a committed specimen Q1 and a rejected commit of a size-lying BlobSource
    When the source is replaced by correct bytes and committed again
    Then the transaction id is the very next one, with no gap
    """
    store, first = seeded
    liar = Specimen(qid="Q2", label="liar", scan=_source(999, _few_bytes_chunks))
    store.root = [store.root[0], liar]
    with pytest.raises(ValueError, match=r"declared size"):
        store.commit()
    liar.scan = b"now-correct"

    second = store.commit()

    assert second == first + 1
    store.close()


def test_a_rejected_size_mismatch_keeps_the_entity_buffered_for_retry(
    seeded: tuple[dc.Store, int],
) -> None:
    """Given a committed specimen Q1 and a rejected commit of a size-lying BlobSource
    When the source is replaced by correct bytes and committed again
    Then the root holds Q1 and Q2
    """
    store, _ = seeded
    liar = Specimen(qid="Q2", label="liar", scan=_source(999, _few_bytes_chunks))
    store.root = [store.root[0], liar]
    with pytest.raises(ValueError, match=r"declared size"):
        store.commit()
    liar.scan = b"now-correct"
    store.commit()

    assert {s.qid for s in store.root} == {"Q1", "Q2"}
    store.close()


def test_a_source_raising_mid_fill_fails_the_commit(seeded: tuple[dc.Store, int]) -> None:
    """Given a committed specimen Q1
    When a specimen whose BlobSource raises on the fill pass is committed
    Then the commit raises the source's error
    """
    store, _ = seeded
    boom = Specimen(qid="Q2", label="boom", scan=_source(len(BIG), _chunks_failing_on_the_fill_pass()))
    store.root = [store.root[0], boom]

    with pytest.raises(RuntimeError, match=r"gremlin"):
        store.commit()
    store.close()


def test_a_source_raising_mid_fill_leaks_nothing_and_burns_no_tid(
    store_factory: StoreFactory, seeded: tuple[dc.Store, int]
) -> None:
    """Given a committed specimen Q1 and a commit whose BlobSource raised mid-fill
    When the store is reopened and Q1 is edited and committed
    Then only Q1 is stored
    And the transaction id is the very next one
    """
    store, base = seeded
    boom = Specimen(qid="Q2", label="boom", scan=_source(len(BIG), _chunks_failing_on_the_fill_pass()))
    store.root = [store.root[0], boom]
    with pytest.raises(RuntimeError, match=r"gremlin"):
        store.commit()
    store.close()
    reopened = store_factory()
    assert {s.qid for s in reopened.root} == {"Q1"}
    reopened.root[0].label = "moved"

    next_tid = reopened.commit()

    assert next_tid == base + 1
    reopened.close()


def test_a_source_writing_past_its_declared_size_is_caught(seeded: tuple[dc.Store, int]) -> None:
    """Given a committed specimen Q1
    When a BlobSource hashing 100 bytes but filling 250 is committed
    Then ValueError names the declared size
    """
    store, _ = seeded
    grower = Specimen(qid="Q2", label="grow", scan=_source(100, _chunks_growing_on_the_fill_pass()))
    store.root = [store.root[0], grower]

    with pytest.raises(ValueError, match=r"declared size|more than"):
        store.commit()
    store.close()


def test_a_source_writing_past_its_declared_size_leaks_nothing_and_burns_no_tid(
    store_factory: StoreFactory, seeded: tuple[dc.Store, int]
) -> None:
    """Given a committed specimen Q1 and a rejected commit of an overflowing BlobSource
    When the store is reopened and Q1 is edited and committed
    Then only Q1 is stored
    And the transaction id is the very next one
    """
    store, base = seeded
    grower = Specimen(qid="Q2", label="grow", scan=_source(100, _chunks_growing_on_the_fill_pass()))
    store.root = [store.root[0], grower]
    with pytest.raises(ValueError, match=r"declared size|more than"):
        store.commit()
    store.close()
    reopened = store_factory()
    assert {s.qid for s in reopened.root} == {"Q1"}
    reopened.root[0].label = "moved"

    next_tid = reopened.commit()

    assert next_tid == base + 1
    reopened.close()


def test_a_one_shot_source_fails_loudly(store_factory: StoreFactory) -> None:
    """Given a BlobSource whose factory hands out one single iterator, empty on the second pass
    When a specimen with that source is committed
    Then ValueError is raised
    """
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="a", scan=_source(6, _one_shot_chunks()))]

    with pytest.raises(ValueError):  # noqa: PT011  # any message: the requirement is that it fails
        store.commit()
    store.close()


def test_blob_from_path_stores_the_file_bytes(store_factory: StoreFactory, tmp_path: Path) -> None:
    """Given a file holding the 4 MiB scan
    When a specimen is committed with blob_from_path on that file
    Then the scan's bytes equal the file's
    """
    path = tmp_path / "label.pdf"
    path.write_bytes(BIG)
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=cast("bytes", dc.blob_from_path(path)))]
    store.commit()

    whole = _handle(store.root[0].scan).bytes()

    assert whole == BIG
    store.close()


def test_blob_from_path_records_the_file_size(store_factory: StoreFactory, tmp_path: Path) -> None:
    """Given a file holding the 4 MiB scan
    When a specimen is committed with blob_from_path on that file
    Then the scan's size is the file's size
    """
    path = tmp_path / "label.pdf"
    path.write_bytes(BIG)
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="azurite", scan=cast("bytes", dc.blob_from_path(path)))]
    store.commit()

    size = _handle(store.root[0].scan).size

    assert size == len(BIG)
    store.close()


def test_negative_size_is_rejected() -> None:
    """Given a BlobSource size of -1
    When the source is created
    Then ValueError says the size must be ">= 0"
    """
    with pytest.raises(ValueError, match=r">= 0"):
        dc.BlobSource(-1, _no_chunks)


# -- review regressions (adversarial pass, 2026-06-15) -----------------------


def test_content_change_between_passes_is_rejected(seeded: tuple[dc.Store, int]) -> None:
    """Given a BlobSource whose two passes have the same size but different bytes
    When a specimen with that source is committed
    Then ValueError says the content is non-deterministic or changed between passes
    """
    store, _ = seeded
    shifty = Specimen(
        qid="Q2", label="x", scan=_source(100, _chunks_changing_content_between_passes())
    )
    store.root = [store.root[0], shifty]

    with pytest.raises(ValueError, match=r"non-deterministic|changed between"):
        store.commit()
    store.close()


def test_content_change_between_passes_leaks_nothing_and_burns_no_tid(
    store_factory: StoreFactory, seeded: tuple[dc.Store, int]
) -> None:
    """Given a committed specimen Q1 and a rejected commit of a content-changing BlobSource
    When the store is reopened and Q1 is edited and committed
    Then only Q1 is stored
    And the transaction id is the very next one
    """
    store, base = seeded
    shifty = Specimen(
        qid="Q2", label="x", scan=_source(100, _chunks_changing_content_between_passes())
    )
    store.root = [store.root[0], shifty]
    with pytest.raises(ValueError, match=r"non-deterministic|changed between"):
        store.commit()
    store.close()
    reopened = store_factory()
    assert {s.qid for s in reopened.root} == {"Q1"}
    reopened.root[0].label = "moved"

    next_tid = reopened.commit()

    assert next_tid == base + 1
    reopened.close()


SHARED = b"shared-scan-bytes" * 256


@pytest.fixture
def one_source_in_two_fields(store_factory: StoreFactory) -> tuple[dc.Store, Specimen]:
    """Return a store and a committed specimen whose scan and thumb share one BlobSource."""
    store = store_factory()
    src = _source(len(SHARED), _chunks(SHARED))
    spec = Specimen(qid="Q1", label="a", scan=src, thumb=src)
    store.root = [spec]
    store.commit()
    return store, spec


def test_one_source_in_two_fields_gets_distinct_live_blob_oids(
    one_source_in_two_fields: tuple[dc.Store, Specimen],
) -> None:
    """Given one BlobSource assigned to both dc.Blob fields of a specimen, committed
    When the live handles are read
    Then both are BlobHandles
    And their blob oids differ
    """
    store, spec = one_source_in_two_fields

    scan, thumb = spec.scan, spec.thumb

    assert isinstance(scan, dc.BlobHandle)
    assert isinstance(thumb, dc.BlobHandle)
    assert scan.blob_oid != thumb.blob_oid
    store.close()


def test_one_source_in_two_fields_gives_both_the_full_bytes(
    one_source_in_two_fields: tuple[dc.Store, Specimen],
) -> None:
    """Given one BlobSource assigned to both dc.Blob fields of a specimen, committed
    When both handles' bytes are read
    Then each equals the source bytes
    """
    store, spec = one_source_in_two_fields

    scan_bytes = _handle(spec.scan).bytes()
    thumb_bytes = _handle(spec.thumb).bytes()

    assert scan_bytes == SHARED
    assert thumb_bytes == SHARED
    store.close()


def test_one_source_in_two_fields_persists_distinct_descriptors(
    store_factory: StoreFactory, one_source_in_two_fields: tuple[dc.Store, Specimen]
) -> None:
    """Given one BlobSource assigned to both dc.Blob fields of a specimen, committed and closed
    When the store is reopened
    Then the two handles have different blob oids
    """
    store, _ = one_source_in_two_fields
    store.close()
    reopened = store_factory()

    scan, thumb = _handle(reopened.root[0].scan), _handle(reopened.root[0].thumb)

    assert scan.blob_oid != thumb.blob_oid
    reopened.close()


def test_one_source_in_two_fields_persists_the_full_bytes_of_both(
    store_factory: StoreFactory, one_source_in_two_fields: tuple[dc.Store, Specimen]
) -> None:
    """Given one BlobSource assigned to both dc.Blob fields of a specimen, committed and closed
    When the store is reopened and both handles' bytes are read
    Then each equals the source bytes
    """
    store, _ = one_source_in_two_fields
    store.close()
    reopened = store_factory()

    scan_bytes = _handle(reopened.root[0].scan).bytes()
    thumb_bytes = _handle(reopened.root[0].thumb).bytes()

    assert scan_bytes == SHARED
    assert thumb_bytes == SHARED
    reopened.close()


@pytest.fixture
def ten_digits(store_factory: StoreFactory) -> dc.Store:
    """Return a store with one committed specimen whose scan is b"0123456789"."""
    store = store_factory()
    store.root = [Specimen(qid="Q1", label="a", scan=b"0123456789")]
    store.commit()
    return store


def test_seek_past_eof_is_allowed(ten_digits: dc.Store) -> None:
    """Given a committed 10-byte scan
    When the stream seeks to position 100
    Then the seek returns 100
    """
    with ten_digits.open_blob(ten_digits.root[0], "scan") as fh:
        position = fh.seek(100)

        assert position == 100
    ten_digits.close()


def test_read_past_eof_returns_nothing(ten_digits: dc.Store) -> None:
    """Given a committed 10-byte scan and a stream seeked to position 100
    When the stream is read
    Then it returns no bytes
    """
    with ten_digits.open_blob(ten_digits.root[0], "scan") as fh:
        fh.seek(100)

        rest = fh.read()

        assert rest == b""
    ten_digits.close()


def test_seek_back_in_range_after_eof_reads_the_tail(ten_digits: dc.Store) -> None:
    """Given a committed 10-byte scan and a stream that sought past EOF
    When the stream seeks back to position 5 and reads
    Then it reads b"56789"
    """
    with ten_digits.open_blob(ten_digits.root[0], "scan") as fh:
        fh.seek(100)
        fh.read()
        fh.seek(5)

        tail = fh.read()

        assert tail == b"56789"
    ten_digits.close()


def test_stream_outliving_its_snapshot_raises_store_closed(sqlite_big: dc.Store) -> None:
    """Given a sqlite blob stream opened on a snapshot, and the snapshot closed under it
    When the stream is drained past its buffer
    Then StoreClosedError is raised, not a raw sqlite3 error
    """
    snap = sqlite_big.snapshot()
    fh = snap.open_blob(snap.all(Specimen)[0], "scan")
    fh.read(4)
    snap.close()

    with pytest.raises(dc.StoreClosedError):
        fh.read()
    fh.close()
    sqlite_big.close()


def test_closing_a_stream_that_outlived_its_snapshot_is_quiet(sqlite_big: dc.Store) -> None:
    """Given a sqlite blob stream whose snapshot closed under it and failed a read
    When the stream is closed
    Then it closes without raising or warning
    And it reports closed
    """
    snap = sqlite_big.snapshot()
    fh = snap.open_blob(snap.all(Specimen)[0], "scan")
    fh.read(4)
    snap.close()
    with pytest.raises(dc.StoreClosedError):
        fh.read()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        fh.close()

    assert fh.closed
    sqlite_big.close()


def test_upsert_of_an_identical_bytearray_blob_keeps_the_blob_oid(
    store_factory: StoreFactory,
) -> None:
    """Given a stored specimen Q1 whose scan was supplied as a bytearray
    When the store is reopened and Q1 is upserted with an identical bytearray scan
    Then the scan keeps its blob oid, as the write path accepts bytearray too
    """
    data = b"some-image-bytes" * 64
    store = store_factory()
    store.root = []
    store.upsert(Specimen(qid="Q1", label="v1", scan=cast("bytes", bytearray(data))))
    store.commit()
    store.close()
    reopened = store_factory()
    before = reopened.get(Specimen, qid="Q1")
    assert before is not None
    oid_before = _handle(before.scan).blob_oid
    del before  # let the live entity go, so the next get rehydrates from the store
    reopened.upsert(Specimen(qid="Q1", label="v2", scan=cast("bytes", bytearray(data))))
    reopened.commit()

    after = reopened.get(Specimen, qid="Q1")

    assert after is not None
    assert _handle(after.scan).blob_oid == oid_before
    reopened.close()
