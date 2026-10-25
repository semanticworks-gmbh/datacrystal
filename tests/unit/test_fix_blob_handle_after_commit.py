"""#197 — after a commit the caller's blob field is a BlobHandle, whatever was assigned.

A raw ``bytes`` assignment used to stay raw ``bytes`` on the caller's instance while a
``BlobSource`` became a ``BlobHandle``.
So ``.size``, ``.hash`` and ``.bytes()`` failed until a reopen.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from typing import Annotated, cast

import datacrystal as dc

StoreFactory = Callable[[], dc.Store]

LABEL_SCAN = b"\x89PNG\r\n\x1a\n" + b"azurite-label" * 64


@dc.entity
class LabelScan:
    """The scanned label of one specimen in the cabinet."""

    qid: Annotated[str, dc.Unique]
    image: Annotated[bytes, dc.Blob] = b""


def _handle(blob_field: object) -> dc.BlobHandle:
    """Return a blob field as the handle it holds after a commit (the field is typed bytes)."""
    return cast("dc.BlobHandle", blob_field)


def _label_chunks() -> Iterator[bytes]:
    """Yield the label scan as one chunk, the re-readable source of a streamed write."""
    return iter([LABEL_SCAN])


def _committed_scan(store: dc.Store, image: object) -> LabelScan:
    """Store a label scan holding ``image``, commit it, and return the caller's instance."""
    scan = LabelScan(qid="S1", image=cast("bytes", image))
    store.root = [scan]
    store.commit()
    return scan


def test_raw_bytes_become_a_handle_on_the_held_instance_after_commit(
    store_factory: StoreFactory,
) -> None:
    """Given a label scan assigned raw bytes
    When the store commits
    Then the held instance's image is a BlobHandle
    """
    store = store_factory()

    scan = _committed_scan(store, LABEL_SCAN)

    assert isinstance(scan.image, dc.BlobHandle)
    store.close()


def test_the_held_handle_knows_its_size_right_after_commit(store_factory: StoreFactory) -> None:
    """Given a label scan assigned raw bytes
    When the store commits
    Then the held handle's size is the byte length
    """
    store = store_factory()

    scan = _committed_scan(store, LABEL_SCAN)

    assert _handle(scan.image).size == len(LABEL_SCAN)
    store.close()


def test_the_held_handle_knows_its_hash_right_after_commit(store_factory: StoreFactory) -> None:
    """Given a label scan assigned raw bytes
    When the store commits
    Then the held handle's hash is the sha256 of the bytes
    """
    store = store_factory()

    scan = _committed_scan(store, LABEL_SCAN)

    assert _handle(scan.image).hash == hashlib.sha256(LABEL_SCAN).digest()
    store.close()


def test_the_held_handle_reads_its_bytes_right_after_commit(store_factory: StoreFactory) -> None:
    """Given a label scan assigned raw bytes
    When the store commits and the held handle's bytes are read
    Then they are the assigned bytes
    """
    store = store_factory()
    scan = _committed_scan(store, LABEL_SCAN)

    content = _handle(scan.image).bytes()

    assert content == LABEL_SCAN
    store.close()


def test_the_held_instance_holds_what_store_get_returns(store_factory: StoreFactory) -> None:
    """Given a committed label scan assigned raw bytes
    When the scan is fetched by its key
    Then the fetched image is the very handle the caller holds
    """
    store = store_factory()
    scan = _committed_scan(store, LABEL_SCAN)

    fetched = store.get(LabelScan, qid="S1")

    assert fetched is not None
    assert fetched.image is scan.image
    store.close()


def test_a_bytearray_becomes_a_handle_after_commit(store_factory: StoreFactory) -> None:
    """Given a label scan assigned a bytearray
    When the store commits
    Then the held handle reads the same bytes
    """
    store = store_factory()
    scan = _committed_scan(store, bytearray(LABEL_SCAN))

    content = _handle(scan.image).bytes()

    assert content == LABEL_SCAN
    store.close()


def test_a_streamed_source_still_becomes_a_handle_after_commit(
    store_factory: StoreFactory,
) -> None:
    """Given a label scan assigned a BlobSource
    When the store commits
    Then the held instance's image is a BlobHandle
    """
    store = store_factory()
    source = dc.BlobSource(len(LABEL_SCAN), _label_chunks)

    scan = _committed_scan(store, source)

    assert isinstance(scan.image, dc.BlobHandle)
    store.close()


def test_reassigned_bytes_become_a_handle_on_the_second_commit(
    store_factory: StoreFactory,
) -> None:
    """Given a committed label scan whose image is replaced by other raw bytes
    When the store commits again
    Then the held handle reads the replacement bytes
    """
    store = store_factory()
    scan = _committed_scan(store, LABEL_SCAN)
    scan.image = b"rescanned-label"
    store.commit()

    content = _handle(scan.image).bytes()

    assert content == b"rescanned-label"
    store.close()


def test_the_stored_image_reads_back_as_the_assigned_bytes_after_reopen(
    store_factory: StoreFactory,
) -> None:
    """Given a label scan committed from raw bytes and the store closed
    When the store is reopened and the scan fetched
    Then the fetched handle reads the assigned bytes
    """
    store = store_factory()
    _committed_scan(store, LABEL_SCAN)
    store.close()
    reopened = store_factory()

    fetched = reopened.get(LabelScan, qid="S1")

    assert fetched is not None
    assert _handle(fetched.image).bytes() == LABEL_SCAN
    reopened.close()
