"""dc.Blob fields — lazy-whole out-of-line raw bytes (ADR-007 / #81+#82+#83).

A ``Annotated[bytes, dc.Blob]`` field stores its bytes OUT-OF-LINE raw in a
sibling ``blobs`` table; the entity record keeps only a 48-byte BLOB_EXT
descriptor. Reading the field gives a :class:`dc.BlobHandle` whose ``.bytes()``
fetches the whole value (lazy, cached, demotable), while ``.size``/``.hash`` are
free from the descriptor. The promise this slice must keep: a scan/query/count
around a blob-bearing type NEVER touches the blobs table.

The test/demo domain stays the mineral cabinet: a ``Scan`` carries the scanned
image/PDF of a specimen label as a blob (the SOR-archive shape from ADR-007's
context).

The schema-evolution test fabricates same-typename classes (inline bytes →
dc.Blob) like the existing evolution tests.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, cast

import pytest

import datacrystal as dc
from datacrystal import UntrackedMutationWarning
from datacrystal._storage.memory import MemoryBackend
from datacrystal._storage.protocol import StoredBlob
from datacrystal._storage.sqlite import SqliteBackend

StoreFactory = Callable[[], dc.Store]


@dc.entity
class Scan:
    """The scanned image of a specimen label, with an optional thumbnail."""

    qid: Annotated[str, dc.Unique]
    label: Annotated[str, dc.Index]
    image: Annotated[bytes, dc.Blob] = b""
    thumb: Annotated[bytes | None, dc.Blob] = None


F = dc.fields(Scan)
IMG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 4096  # ~24 KB, not trivially small
BIG = b"M" * (1024 * 1024)  # 1 MB, the "stays tiny" probe


class _SpyBackend(MemoryBackend):
    """A memory backend that counts ``load_blob`` calls.

    It is the op-count gate (invariant 12) for 'a query never reads the blobs table'.
    """

    def __init__(self) -> None:
        super().__init__()
        self.load_blob_calls = 0

    def load_blob(self, oid: int) -> StoredBlob | None:
        self.load_blob_calls += 1
        return super().load_blob(oid)


class _Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _SpyCabinet:
    store: dc.Store
    backend: _SpyBackend
    clock: _Clock


def _handle(blob_field: object) -> dc.BlobHandle:
    """Return a blob field as the handle a stored scan hydrates to (the field is typed bytes)."""
    return cast("dc.BlobHandle", blob_field)


@pytest.fixture
def spy_cabinet() -> _SpyCabinet:
    """Return a 10 s lazy-timeout store, over a spy backend, holding one stored azurite scan."""
    clock = _Clock()
    backend = _SpyBackend()
    seed = dc.Store._from_backend(backend)
    seed.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    seed.commit()
    seed.close()
    store = dc.Store._from_backend(backend, lazy_timeout=10.0, lazy_clock=clock)
    return _SpyCabinet(store, backend, clock)


@pytest.fixture
def two_scans_on_a_spy_backend() -> tuple[dc.Store, _SpyBackend]:
    """Return a reopened store of the azurite and cuprite scans over a spy backend."""
    backend = _SpyBackend()
    seed = dc.Store._from_backend(backend)
    seed.root = [
        Scan(qid="Q1", label="azurite", image=IMG),
        Scan(qid="Q2", label="cuprite", image=IMG),
    ]
    seed.commit()
    seed.close()
    return dc.Store._from_backend(backend), backend


@pytest.fixture
def committed_original_scan() -> tuple[MemoryBackend, dc.Store, Scan, int]:
    """Return a memory backend, its store, a committed scan imaged b"original", and the blob oid."""
    backend = MemoryBackend()
    store = dc.Store._from_backend(backend)
    scan = Scan(qid="Q1", label="azurite", image=b"original")
    store.root = [scan]
    store.commit()
    (first_oid,) = list(backend._blobs)
    return backend, store, scan, first_oid


# -- round-trip --------------------------------------------------------------


def test_blob_field_reads_back_as_a_handle_after_reopen(store_factory: StoreFactory) -> None:
    """Given a stored scan with an image blob
    When the store is reopened and the scan is read
    Then the image is a BlobHandle
    And its size is the image length
    And its hash is the sha256 of the image
    And its bytes are the image
    """
    store = store_factory()
    store.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    store.commit()
    store.close()
    reopened = store_factory()

    handle = _handle(reopened.root[0].image)

    assert isinstance(handle, dc.BlobHandle)
    assert handle.size == len(IMG)
    assert handle.hash == hashlib.sha256(IMG).digest()
    assert handle.bytes() == IMG
    reopened.close()


def test_size_and_hash_need_no_fetch(store_factory: StoreFactory) -> None:
    """Given a stored scan with an image blob, reopened
    When the handle's size and hash are read
    Then they match the image
    And the handle is still not loaded
    """
    store = store_factory()
    store.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    store.commit()
    store.close()
    reopened = store_factory()
    handle = _handle(reopened.root[0].image)

    size, digest = handle.size, handle.hash

    assert size == len(IMG)
    assert digest == hashlib.sha256(IMG).digest()
    assert not handle.loaded
    reopened.close()


def test_first_bytes_call_fetches_the_blob_once(spy_cabinet: _SpyCabinet) -> None:
    """Given a reopened store whose scan blob was never fetched
    When the handle's bytes are read
    Then they are the image
    And the backend served one blob load
    """
    handle = _handle(spy_cabinet.store.root[0].image)

    fetched = handle.bytes()

    assert fetched == IMG
    assert spy_cabinet.backend.load_blob_calls == 1
    spy_cabinet.store.close()


def test_second_bytes_call_is_served_from_the_cache(spy_cabinet: _SpyCabinet) -> None:
    """Given a handle whose bytes were fetched once
    When its bytes are read again
    Then they are the image
    And the backend still served only one blob load
    """
    handle = _handle(spy_cabinet.store.root[0].image)
    handle.bytes()

    again = handle.bytes()

    assert again == IMG
    assert spy_cabinet.backend.load_blob_calls == 1
    spy_cabinet.store.close()


def test_idle_blob_is_demoted_at_the_next_boundary_sweep(spy_cabinet: _SpyCabinet) -> None:
    """Given a handle fetched at t=0 with a 10 s lazy timeout
    When the clock reads 11 s and the store's root is touched
    Then the handle is no longer loaded
    """
    handle = _handle(spy_cabinet.store.root[0].image)
    handle.bytes()
    spy_cabinet.clock.now = 11.0

    _ = spy_cabinet.store.root

    assert not handle.loaded
    spy_cabinet.store.close()


def test_reading_a_cached_blob_refreshes_its_idle_clock(spy_cabinet: _SpyCabinet) -> None:
    """Given a handle fetched at t=0 and read again at t=8 with a 10 s lazy timeout
    When the clock reads 16 s and the store's root is touched
    Then the handle is still loaded, idle only 8 s
    """
    handle = _handle(spy_cabinet.store.root[0].image)
    handle.bytes()
    spy_cabinet.clock.now = 8.0
    handle.bytes()
    spy_cabinet.clock.now = 16.0

    _ = spy_cabinet.store.root

    assert handle.loaded
    spy_cabinet.store.close()


def test_demoted_blob_reloads_transparently(spy_cabinet: _SpyCabinet) -> None:
    """Given a handle that was fetched, then demoted after 11 s idle
    When its bytes are read again
    Then they are the image
    And the backend served a second blob load
    """
    handle = _handle(spy_cabinet.store.root[0].image)
    handle.bytes()
    spy_cabinet.clock.now = 11.0
    _ = spy_cabinet.store.root

    reloaded = handle.bytes()

    assert reloaded == IMG
    assert spy_cabinet.backend.load_blob_calls == 2
    spy_cabinet.store.close()


# -- the promise: a scan/query/count never reads the blobs table -------------


def test_count_does_not_read_blobs(two_scans_on_a_spy_backend: tuple[dc.Store, _SpyBackend]) -> None:
    """Given a store of two scans with image blobs
    When the scans are counted
    Then the count is 2
    And the blobs table was never read
    """
    store, backend = two_scans_on_a_spy_backend

    counted = store.count(Scan)

    assert counted == 2
    assert backend.load_blob_calls == 0
    store.close()


def test_filtered_query_does_not_read_blobs(
    two_scans_on_a_spy_backend: tuple[dc.Store, _SpyBackend],
) -> None:
    """Given a store of two scans with image blobs
    When the scans labelled azurite are queried
    Then one scan is returned
    And the blobs table was never read
    """
    store, backend = two_scans_on_a_spy_backend

    azurite = store.query(F.label == "azurite")

    assert len(azurite) == 1
    assert backend.load_blob_calls == 0
    store.close()


def test_full_query_does_not_read_blobs(
    two_scans_on_a_spy_backend: tuple[dc.Store, _SpyBackend],
) -> None:
    """Given a store of two scans with image blobs
    When all scans are queried
    Then two scans are returned
    And the blobs table was never read
    """
    store, backend = two_scans_on_a_spy_backend

    scans = store.query(Scan)

    assert len(scans) == 2
    assert backend.load_blob_calls == 0
    store.close()


def test_pluck_returns_inert_descriptors_without_reading_blobs(
    two_scans_on_a_spy_backend: tuple[dc.Store, _SpyBackend],
) -> None:
    """Given a store of two scans with image blobs
    When label and image are plucked
    Then every image descriptor carries the image size
    And every image descriptor carries the image hash
    And the blobs table was never read
    """
    store, backend = two_scans_on_a_spy_backend

    plucked = store.pluck(Scan, "label", "image")

    assert all(image.size == len(IMG) for _label, image in plucked)
    assert all(image.hash == hashlib.sha256(IMG).digest() for _label, image in plucked)
    assert backend.load_blob_calls == 0
    store.close()


def test_bytes_load_only_when_explicitly_asked_for(
    two_scans_on_a_spy_backend: tuple[dc.Store, _SpyBackend],
) -> None:
    """Given a store of two scans with image blobs
    When the bytes of the azurite scan's image are read
    Then the blobs table was read exactly once
    """
    store, backend = two_scans_on_a_spy_backend
    azurite = store.query(F.label == "azurite")[0]

    _handle(azurite.image).bytes()

    assert backend.load_blob_calls == 1
    store.close()


def test_record_stays_tiny_for_a_megabyte_blob() -> None:
    """Given a committed scan with a 1 MB image blob
    When the stored object records are measured
    Then the largest record is under 200 bytes
    """
    backend = MemoryBackend()
    store = dc.Store._from_backend(backend)
    store.root = [Scan(qid="Q1", label="big", image=BIG)]
    store.commit()
    store.close()

    assert max(len(rec.payload) for rec in backend._objects.values()) < 200


def test_megabyte_blob_lives_in_one_raw_whole_blob_row() -> None:
    """Given a committed scan with a 1 MB image blob
    When the blobs table is read
    Then it holds exactly one row
    And that row is the 1 MB image, raw and whole
    """
    backend = MemoryBackend()
    store = dc.Store._from_backend(backend)
    store.root = [Scan(qid="Q1", label="big", image=BIG)]
    store.commit()
    store.close()

    (stored,) = backend._blobs.values()

    assert len(backend._blobs) == 1
    assert stored.size == len(BIG)
    assert stored.data == BIG


# -- None blob, immutability --------------------------------------------------


def test_none_blob_reads_back_as_none(store_factory: StoreFactory) -> None:
    """Given a stored scan with an image and a None thumb
    When the store is reopened and the scan is read
    Then the thumb is None
    """
    store = store_factory()
    store.root = [Scan(qid="Q1", label="azurite", image=IMG, thumb=None)]
    store.commit()
    store.close()
    reopened = store_factory()

    scan = reopened.root[0]

    assert scan.thumb is None
    reopened.close()


def test_non_none_blob_beside_a_none_one_reads_back_as_a_handle(
    store_factory: StoreFactory,
) -> None:
    """Given a stored scan with an image and a None thumb
    When the store is reopened and the scan is read
    Then the image is a BlobHandle
    """
    store = store_factory()
    store.root = [Scan(qid="Q1", label="azurite", image=IMG, thumb=None)]
    store.commit()
    store.close()
    reopened = store_factory()

    scan = reopened.root[0]

    assert isinstance(scan.image, dc.BlobHandle)
    reopened.close()


def test_none_blob_writes_no_row() -> None:
    """Given a scan with an image and a None thumb
    When it is committed
    Then the blobs table holds one row, for the image alone
    """
    backend = MemoryBackend()
    store = dc.Store._from_backend(backend)
    store.root = [Scan(qid="Q1", label="a", image=IMG, thumb=None)]

    store.commit()

    assert len(backend._blobs) == 1
    store.close()


def test_changing_a_blob_leaves_the_old_row_untouched(
    committed_original_scan: tuple[MemoryBackend, dc.Store, Scan, int],
) -> None:
    """Given a committed scan whose image is b"original"
    When the image is replaced with b"replacement" and committed
    Then the old blob row still holds b"original"
    """
    backend, store, scan, first_oid = committed_original_scan

    scan.image = b"replacement"
    store.commit()

    assert backend._blobs[first_oid].data == b"original"
    store.close()


def test_changing_a_blob_mints_one_new_row(
    committed_original_scan: tuple[MemoryBackend, dc.Store, Scan, int],
) -> None:
    """Given a committed scan whose image is b"original"
    When the image is replaced with b"replacement" and committed
    Then exactly one new blob row exists
    And it holds b"replacement"
    """
    backend, store, scan, first_oid = committed_original_scan

    scan.image = b"replacement"
    store.commit()

    new_oids = set(backend._blobs) - {first_oid}
    assert len(new_oids) == 1
    (new_oid,) = new_oids
    assert backend._blobs[new_oid].data == b"replacement"
    store.close()


def test_changed_blob_reads_back_after_reopen(
    committed_original_scan: tuple[MemoryBackend, dc.Store, Scan, int],
) -> None:
    """Given a committed scan whose image was replaced with b"replacement" and committed
    When the store is reopened and the image bytes are read
    Then they are b"replacement"
    """
    backend, store, scan, _ = committed_original_scan
    scan.image = b"replacement"
    store.commit()
    store.close()
    reopened = dc.Store._from_backend(backend)

    fetched = _handle(reopened.root[0].image).bytes()

    assert fetched == b"replacement"
    reopened.close()


# -- both backends agree (parametrized) --------------------------------------


def test_both_backends_return_the_whole_value(store_factory: StoreFactory) -> None:
    """Given a stored scan with an image and a thumb blob
    When the store is reopened and the scan is read
    Then the image bytes equal the image
    And the thumb bytes equal the thumb
    And the image size is the image length
    """
    store = store_factory()
    store.root = [Scan(qid="Q1", label="azurite", image=IMG, thumb=b"tiny-thumb")]
    store.commit()
    store.close()
    reopened = store_factory()

    scan = reopened.root[0]

    assert _handle(scan.image).bytes() == IMG
    assert _handle(scan.thumb).bytes() == b"tiny-thumb"
    assert _handle(scan.image).size == len(IMG)
    reopened.close()


# -- atomicity: a failed apply rolls back BOTH the record and the blob -------


class _FailOnApply(MemoryBackend):
    """A memory backend whose ``apply`` raises after the batch was handed over.

    It proves the record and its blob land together or not at all.
    """

    def apply(self, batch: object) -> None:
        raise RuntimeError("injected storage failure")


def test_failed_apply_raises_the_storage_error() -> None:
    """Given a backend whose apply fails
    When a scan with an image blob is committed
    Then the commit raises the injected storage error
    """
    store = dc.Store._from_backend(_FailOnApply())
    store.root = [Scan(qid="Q1", label="azurite", image=IMG)]

    with pytest.raises(RuntimeError, match="injected storage failure"):
        store.commit()
    store.close()


def test_failed_apply_persists_neither_record_nor_blob() -> None:
    """Given a backend whose apply fails
    When a scan with an image blob fails to commit
    Then the backend holds no records
    And it holds no blobs
    """
    backend = _FailOnApply()
    store = dc.Store._from_backend(backend)
    store.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    with pytest.raises(RuntimeError, match="injected storage failure"):
        store.commit()

    assert backend._objects == {}
    assert backend._blobs == {}
    store.close()


class _FlakyConn:
    """A proxy over a sqlite3 connection that blows up on the meta INSERT.

    That is AFTER apply() has put the objects AND blobs rows in the open BEGIN IMMEDIATE txn
    but BEFORE COMMIT. Everything else delegates, so the real ROLLBACK runs on the real connection.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def executemany(self, sql: str, seq: Iterable[Sequence[object]]) -> sqlite3.Cursor:
        if sql.startswith("INSERT OR REPLACE INTO meta"):
            raise RuntimeError("injected mid-txn failure")
        return self._conn.executemany(sql, seq)

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


@pytest.fixture
def sqlite_store_failing_at_the_meta_insert(
    tmp_path: Path,
) -> Iterator[tuple[dc.Store, sqlite3.Connection]]:
    """Yield a sqlite store whose apply() fails at the meta INSERT, and its real connection."""
    store = dc.Store.open(tmp_path / "store", lock_ttl=0.5)
    backend = cast("SqliteBackend", store._backend)
    real_conn = backend._conn
    backend._conn = cast("sqlite3.Connection", _FlakyConn(real_conn))
    yield store, real_conn
    backend._conn = real_conn
    store.close()


def test_sqlite_failed_txn_rolls_back_the_record(
    sqlite_store_failing_at_the_meta_insert: tuple[dc.Store, sqlite3.Connection],
) -> None:
    """Given a sqlite store whose connection fails at the meta INSERT inside apply()
    When a scan with an image blob fails to commit
    Then the objects table is empty
    """
    store, real_conn = sqlite_store_failing_at_the_meta_insert
    store.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    with pytest.raises(RuntimeError, match="injected mid-txn failure"):
        store.commit()

    (objects,) = real_conn.execute("SELECT count(*) FROM objects").fetchone()

    assert objects == 0


def test_sqlite_failed_txn_rolls_back_the_blob(
    sqlite_store_failing_at_the_meta_insert: tuple[dc.Store, sqlite3.Connection],
) -> None:
    """Given a sqlite store whose connection fails at the meta INSERT inside apply()
    When a scan with an image blob fails to commit
    Then the blobs table is empty
    """
    store, real_conn = sqlite_store_failing_at_the_meta_insert
    store.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    with pytest.raises(RuntimeError, match="injected mid-txn failure"):
        store.commit()

    (blobs,) = real_conn.execute("SELECT count(*) FROM blobs").fetchone()

    assert blobs == 0


# -- schema evolution: inline bytes -> dc.Blob -------------------------------


def _evolve_scan(image_annotation: object) -> type:
    """Redefine 'EvolvingScan' with the given annotation for `image`, under one typename.

    The same typename across runs simulates code changed between runs.
    """
    annotations = {
        "qid": Annotated[str, dc.Unique],
        "image": image_annotation,
    }
    namespace: dict = {
        "__module__": __name__,
        "__qualname__": "EvolvingScan",
        "__annotations__": annotations,
        "image": b"",
    }
    return dc.entity(type("EvolvingScan", (), namespace))


def test_an_old_inline_record_still_reads_as_a_scan_of_the_new_shape(
    store_factory: StoreFactory,
) -> None:
    """Given a stored scan written when image was plain inline bytes
    When the store is reopened after image became a dc.Blob
    Then the scan is an instance of the new shape
    """
    v1 = _evolve_scan(bytes)
    store = store_factory()
    store.root = [v1(qid="Q1", image=b"inline-bytes")]
    store.commit()
    store.close()
    v2 = _evolve_scan(Annotated[bytes, dc.Blob])
    reopened = store_factory()

    scan = reopened.root[0]

    assert isinstance(scan, v2)
    reopened.close()


def test_an_old_inline_record_reads_its_image_as_raw_bytes(store_factory: StoreFactory) -> None:
    """Given a stored scan written when image was plain inline bytes
    When the store is reopened after image became a dc.Blob
    Then the image is the raw bytes, decoded through its own shape
    And it is not a BlobHandle
    """
    v1 = _evolve_scan(bytes)
    store = store_factory()
    store.root = [v1(qid="Q1", image=b"inline-bytes")]
    store.commit()
    store.close()
    _evolve_scan(Annotated[bytes, dc.Blob])
    reopened = store_factory()

    scan = reopened.root[0]

    assert scan.image == b"inline-bytes"
    assert not isinstance(scan.image, dc.BlobHandle)
    reopened.close()


def test_a_new_record_under_the_blob_shape_reads_back_as_a_handle(
    store_factory: StoreFactory,
) -> None:
    """Given an inline-bytes scan stored before image became a dc.Blob
    When a second scan is stored under the dc.Blob shape and the store is reopened
    Then the second scan's image is a BlobHandle
    And its bytes are the stored bytes
    """
    v1 = _evolve_scan(bytes)
    store = store_factory()
    store.root = [v1(qid="Q1", image=b"inline-bytes")]
    store.commit()
    store.close()
    v2 = _evolve_scan(Annotated[bytes, dc.Blob])
    reopened = store_factory()
    reopened.store(v2(qid="Q2", image=b"out-of-line-bytes"))
    reopened.commit()
    reopened.close()
    third = store_factory()

    q2 = third.get(v2, qid="Q2")

    assert q2 is not None
    assert isinstance(q2.image, dc.BlobHandle)
    assert q2.image.bytes() == b"out-of-line-bytes"
    third.close()


def test_the_old_inline_record_stays_raw_bytes_beside_a_new_blob_record(
    store_factory: StoreFactory,
) -> None:
    """Given an inline-bytes scan stored before image became a dc.Blob, and a blob scan after
    When the store is reopened and the old scan is fetched
    Then its image is still the raw inline bytes
    """
    v1 = _evolve_scan(bytes)
    store = store_factory()
    store.root = [v1(qid="Q1", image=b"inline-bytes")]
    store.commit()
    store.close()
    v2 = _evolve_scan(Annotated[bytes, dc.Blob])
    reopened = store_factory()
    reopened.store(v2(qid="Q2", image=b"out-of-line-bytes"))
    reopened.commit()
    reopened.close()
    third = store_factory()

    q1 = third.get(v2, qid="Q1")

    assert q1 is not None
    assert q1.image == b"inline-bytes"
    third.close()


# -- debug fingerprint net (must not false-positive on blob fields) ----------


@pytest.fixture(params=["memory", "sqlite"])
def debug_store_factory(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[], dc.Store]:
    """Return a zero-arg callable that opens the *same* store each call with debug on."""
    if request.param == "memory":
        backend = MemoryBackend()

        def open_store() -> dc.Store:
            return dc.Store._from_backend(backend, debug=True)
    else:

        def open_store() -> dc.Store:
            return dc.Store.open(tmp_path / "store", lock_ttl=0.5, debug=True)

    return open_store


def test_debug_mode_does_not_flag_unchanged_blob_entity(
    debug_store_factory: StoreFactory, recwarn: pytest.WarningsRecorder
) -> None:
    """Given a debug store holding a clean scan whose blob was rehydrated as a handle
    When another scan is stored and committed, so the sweep walks the clean one
    Then no untracked-mutation warning is raised
    """
    seed = debug_store_factory()
    seed.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    seed.commit()
    seed.commit()
    seed.close()
    reopened = debug_store_factory()
    _handle(reopened.root[0].image).bytes()
    reopened.store(Scan(qid="Q2", label="other", image=b"small"))

    reopened.commit()

    assert not any(isinstance(w.message, UntrackedMutationWarning) for w in recwarn.list)
    reopened.close()


# -- marker guards ------------------------------------------------------------


def test_blob_on_non_bytes_field_raises() -> None:
    """Given an entity declaring a str field as dc.Blob
    When the entity class is created
    Then TypeError says the field must be bytes
    """
    with pytest.raises(TypeError, match="must be bytes"):

        @dc.entity
        class Bad:
            qid: Annotated[str, dc.Unique]
            oops: Annotated[str, dc.Blob] = ""


def test_blob_plus_index_raises() -> None:
    """Given an entity declaring a bytes field as both dc.Blob and dc.Index
    When the entity class is created
    Then TypeError says a blob cannot also be Index/Unique/SortedIndex
    """
    with pytest.raises(TypeError, match="cannot also be Index/Unique/SortedIndex"):

        @dc.entity
        class Bad:
            qid: Annotated[str, dc.Unique]
            oops: Annotated[bytes, dc.Blob, dc.Index] = b""


def test_blob_plus_unique_raises() -> None:
    """Given an entity declaring a bytes field as both dc.Blob and dc.Unique
    When the entity class is created
    Then TypeError says a blob cannot also be Index/Unique/SortedIndex
    """
    with pytest.raises(TypeError, match="cannot also be Index/Unique/SortedIndex"):

        @dc.entity
        class Bad:
            qid: Annotated[str, dc.Unique]
            oops: Annotated[bytes, dc.Blob, dc.Unique] = b""


def test_blob_plus_renamed_from_raises() -> None:
    """Given an entity declaring a bytes field as both dc.Blob and dc.RenamedFrom
    When the entity class is created
    Then TypeError says a blob cannot also be FullText/RenamedFrom/Glue
    """
    with pytest.raises(TypeError, match="cannot also be FullText/RenamedFrom/Glue"):

        @dc.entity
        class Bad:
            qid: Annotated[str, dc.Unique]
            oops: Annotated[bytes, dc.Blob, dc.RenamedFrom("old")] = b""


def test_optional_bytes_blob_is_allowed(store: dc.Store) -> None:
    """Given an entity declaring an optional bytes field as dc.Blob, holding bytes
    When the entity class is created, stored, committed and read back
    Then the field is a blob handle of the stored size
    """

    @dc.entity
    class Ok:
        qid: Annotated[str, dc.Unique]
        data: Annotated[bytes | None, dc.Blob] = None

    store.store(Ok(qid="Q1", data=b"abc"))
    store.commit()

    [found] = store.query(Ok)

    assert isinstance(found.data, dc.BlobHandle)
    assert found.data.size == 3


# -- regression: re-committing a REOPENED blob entity (adversarial review) ----
# The encode/upsert paths must understand a hydrated BlobHandle, or a reopened
# blob entity cannot be updated and upsert silently re-stores/wipes the blob.


def test_editing_a_sibling_field_of_a_reopened_blob_entity_commits(
    store_factory: StoreFactory,
) -> None:
    """Given a stored scan reopened, its image a handle
    When the scan's label is edited and committed
    Then the commit succeeds and the new label is stored
    """
    store = store_factory()
    store.root = [Scan(qid="S1", label="draft", image=IMG)]
    store.commit()
    store.close()
    reopened = store_factory()
    scan = reopened.get(Scan, qid="S1")
    assert scan is not None
    scan.label = "final"
    reopened.commit()
    reopened.close()
    third = store_factory()

    back = third.get(Scan, qid="S1")

    assert back is not None
    assert back.label == "final"
    third.close()


def test_editing_a_sibling_field_leaves_the_blob_untouched(store_factory: StoreFactory) -> None:
    """Given a stored scan reopened, its label edited and committed
    When the store is reopened again and the image is read
    Then its bytes are the original image
    And its hash is the sha256 of the original image
    """
    store = store_factory()
    store.root = [Scan(qid="S1", label="draft", image=IMG)]
    store.commit()
    store.close()
    reopened = store_factory()
    scan = reopened.get(Scan, qid="S1")
    assert scan is not None
    scan.label = "final"
    reopened.commit()
    reopened.close()
    third = store_factory()

    back = third.get(Scan, qid="S1")

    assert back is not None
    assert _handle(back.image).bytes() == IMG
    assert _handle(back.image).hash == hashlib.sha256(IMG).digest()
    third.close()


def test_upsert_with_an_identical_blob_updates_the_label(store_factory: StoreFactory) -> None:
    """Given a stored scan S1 labelled v1
    When S1 is upserted with the same image and the label v2
    Then the label is v2
    And the image bytes are unchanged
    """
    store = store_factory()
    store.root = []
    store.upsert(Scan(qid="S1", label="v1", image=IMG))
    store.commit()
    store.upsert(Scan(qid="S1", label="v2", image=IMG))
    store.commit()

    scan = store.get(Scan, qid="S1")

    assert scan is not None
    assert scan.label == "v2"
    assert _handle(scan.image).bytes() == IMG
    store.close()


def test_upsert_with_an_identical_blob_does_not_restore_it(store_factory: StoreFactory) -> None:
    """Given a stored scan S1 whose image has a blob oid
    When S1 is upserted with the same image and a new label
    Then the image keeps the same blob oid
    """
    store = store_factory()
    store.root = []
    store.upsert(Scan(qid="S1", label="v1", image=IMG))
    store.commit()
    first = store.get(Scan, qid="S1")
    assert first is not None
    oid1 = _handle(first.image).blob_oid
    store.upsert(Scan(qid="S1", label="v2", image=IMG))
    store.commit()

    scan = store.get(Scan, qid="S1")

    assert scan is not None
    assert _handle(scan.image).blob_oid == oid1
    store.close()


def test_upsert_with_a_changed_blob_stores_the_new_bytes(store_factory: StoreFactory) -> None:
    """Given a stored scan S1
    When S1 is upserted with a longer image
    Then the image bytes are the longer image
    """
    store = store_factory()
    store.root = []
    store.upsert(Scan(qid="S1", label="v1", image=IMG))
    store.commit()
    bigger = IMG + b"!"
    store.upsert(Scan(qid="S1", label="v1", image=bigger))
    store.commit()

    scan = store.get(Scan, qid="S1")

    assert scan is not None
    assert _handle(scan.image).bytes() == bigger
    store.close()


def test_upsert_with_a_changed_blob_mints_a_fresh_blob_oid(store_factory: StoreFactory) -> None:
    """Given a stored scan S1 whose image has a blob oid
    When S1 is upserted with a longer image
    Then the image has a different blob oid
    """
    store = store_factory()
    store.root = []
    store.upsert(Scan(qid="S1", label="v1", image=IMG))
    store.commit()
    first = store.get(Scan, qid="S1")
    assert first is not None
    oid1 = _handle(first.image).blob_oid
    store.upsert(Scan(qid="S1", label="v1", image=IMG + b"!"))
    store.commit()

    scan = store.get(Scan, qid="S1")

    assert scan is not None
    assert _handle(scan.image).blob_oid != oid1
    store.close()


def test_unmarking_a_blob_field_errors_clearly(store_factory: StoreFactory) -> None:
    """Given a stored scan whose image was a dc.Blob, now redeclared as plain bytes
    When the reopened scan is marked dirty and committed
    Then TypeError says the field is an un-marked dc.Blob
    """
    blobbed = _evolve_scan(Annotated[bytes, dc.Blob])
    store = store_factory()
    store.root = [blobbed(qid="Q1", image=b"x" * 100)]
    store.commit()
    store.close()
    plain = _evolve_scan(bytes)
    reopened = store_factory()
    scan = reopened.get(plain, qid="Q1")
    assert scan is not None
    reopened.mark_dirty(scan)

    with pytest.raises(TypeError, match=r"un-marked dc\.Blob"):
        reopened.commit()
    reopened.close()
