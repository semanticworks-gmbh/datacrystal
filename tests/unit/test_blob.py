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
dc.Blob) like the existing evolution tests, hence the per-file pyright pragmas.
"""
# pyright: reportCallIssue=false, reportArgumentType=false, reportAttributeAccessIssue=false
# pyright: reportFunctionMemberAccess=false

from __future__ import annotations

import hashlib
from typing import Annotated

import pytest

import datacrystal as dc
from datacrystal._records import BlobToken
from datacrystal._storage.memory import MemoryBackend


@dc.entity
class Scan:
    qid: Annotated[str, dc.Unique]
    label: Annotated[str, dc.Index]
    image: Annotated[bytes, dc.Blob] = b""
    thumb: Annotated[bytes | None, dc.Blob] = None


IMG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 4096  # ~24 KB, not trivially small
BIG = b"M" * (1024 * 1024)                     # 1 MB, the "stays tiny" probe


class _SpyBackend(MemoryBackend):
    """A memory backend that counts ``load_blob`` calls — the op-count gate
    (invariant 12) for 'a query never reads the blobs table'."""

    def __init__(self) -> None:
        super().__init__()
        self.load_blob_calls = 0

    def load_blob(self, oid: int):
        self.load_blob_calls += 1
        return super().load_blob(oid)


# -- round-trip --------------------------------------------------------------


def test_blob_roundtrip_reopen(store_factory):
    store = store_factory()
    store.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    store.commit()
    store.close()

    reopened = store_factory()
    scan = reopened.root[0]
    handle = scan.image
    assert isinstance(handle, dc.BlobHandle)
    assert handle.size == len(IMG)
    assert handle.hash == hashlib.sha256(IMG).digest()
    assert handle.bytes() == IMG
    reopened.close()


def test_size_and_hash_need_no_fetch(store_factory):
    store = store_factory()
    store.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    store.commit()
    store.close()

    reopened = store_factory()
    handle = reopened.root[0].image
    # .size / .hash come straight from the descriptor — never loaded.
    assert not handle.loaded
    assert handle.size == len(IMG)
    assert handle.hash == hashlib.sha256(IMG).digest()
    assert not handle.loaded  # still not fetched
    reopened.close()


def test_bytes_caches_then_demotes_and_reloads():
    class _Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = _Clock()
    backend = _SpyBackend()
    seed = dc.Store._from_backend(backend)
    seed.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    seed.commit()
    seed.close()

    store = dc.Store._from_backend(backend, lazy_timeout=10.0, lazy_clock=clock)
    handle = store.root[0].image
    assert handle.bytes() == IMG  # first fetch, tracked at t=0
    assert backend.load_blob_calls == 1
    assert handle.bytes() == IMG  # cached — no re-fetch
    assert backend.load_blob_calls == 1

    clock.now = 11.0
    store.root  # boundary sweep: idle 11s > 10s → demote the cached bytes
    assert not handle.loaded
    assert handle.bytes() == IMG  # transparent reload through the store
    assert backend.load_blob_calls == 2
    store.close()


# -- the promise: a scan/query/count never reads the blobs table -------------


def test_query_does_not_read_blobs():
    backend = _SpyBackend()
    seed = dc.Store._from_backend(backend)
    seed.root = [
        Scan(qid="Q1", label="azurite", image=IMG),
        Scan(qid="Q2", label="cuprite", image=IMG),
    ]
    seed.commit()
    seed.close()

    store = dc.Store._from_backend(backend)
    assert store.count(Scan) == 2
    assert len(store.query(Scan.label == "azurite")) == 1
    assert len(store.query(Scan)) == 2
    plucked = store.pluck(Scan, "label", "image")
    # No blob fetch happened for any decode-level read.
    assert backend.load_blob_calls == 0
    # pluck returns the inert descriptor, not the bytes.
    for _label, image in plucked:
        assert isinstance(image, BlobToken)
        assert image.size == len(IMG)
    # the bytes only load when explicitly asked for
    store.query(Scan.label == "azurite")[0].image.bytes()
    assert backend.load_blob_calls == 1
    store.close()


def test_object_record_stays_tiny_for_a_megabyte_blob():
    backend = MemoryBackend()
    store = dc.Store._from_backend(backend)
    store.root = [Scan(qid="Q1", label="big", image=BIG)]
    store.commit()
    store.close()

    payloads = {oid: rec.payload for oid, rec in backend._objects.items()}
    # The Scan record must be a couple dozen bytes — the descriptor, never the
    # 1 MB. (The largest record in the store is the Scan; assert it's tiny.)
    biggest = max(len(p) for p in payloads.values())
    assert biggest < 200, f"a blob-bearing record is not tiny: {biggest} bytes"
    # The bytes live in exactly one blob row, raw and whole.
    assert len(backend._blobs) == 1
    (stored,) = backend._blobs.values()
    assert stored.size == len(BIG)
    assert stored.data == BIG


# -- None blob, immutability --------------------------------------------------


def test_none_blob_writes_no_row_and_reads_back_none(store_factory):
    store = store_factory()
    store.root = [Scan(qid="Q1", label="azurite", image=IMG, thumb=None)]
    store.commit()
    store.close()

    reopened = store_factory()
    scan = reopened.root[0]
    assert scan.thumb is None          # a None blob field is just None
    assert isinstance(scan.image, dc.BlobHandle)  # the non-None one is a handle
    reopened.close()


def test_none_blob_makes_exactly_one_blob_row():
    backend = MemoryBackend()
    store = dc.Store._from_backend(backend)
    store.root = [Scan(qid="Q1", label="a", image=IMG, thumb=None)]
    store.commit()
    # image → 1 row; thumb=None → 0 rows
    assert len(backend._blobs) == 1
    store.close()


def test_changing_a_blob_mints_a_new_oid():
    backend = MemoryBackend()
    store = dc.Store._from_backend(backend)
    scan = Scan(qid="Q1", label="azurite", image=b"original")
    store.root = [scan]
    store.commit()
    (first_oid,) = list(backend._blobs)

    scan.image = b"replacement"  # mutate the live field
    store.commit()

    # A new blob row exists; the OLD row is untouched (immutable, ADR-007).
    assert first_oid in backend._blobs
    assert backend._blobs[first_oid].data == b"original"
    new_oids = [o for o in backend._blobs if o != first_oid]
    assert len(new_oids) == 1
    assert backend._blobs[new_oids[0]].data == b"replacement"
    store.close()

    reopened = dc.Store._from_backend(backend)
    assert reopened.root[0].image.bytes() == b"replacement"
    reopened.close()


# -- both backends agree (parametrized) --------------------------------------


def test_both_backends_whole_value(store_factory):
    store = store_factory()
    store.root = [Scan(qid="Q1", label="azurite", image=IMG, thumb=b"tiny-thumb")]
    store.commit()
    store.close()

    reopened = store_factory()
    scan = reopened.root[0]
    assert scan.image.bytes() == IMG
    assert scan.thumb.bytes() == b"tiny-thumb"
    assert scan.image.size == len(IMG)
    reopened.close()


# -- atomicity: a failed apply rolls back BOTH the record and the blob -------


class _FailOnApply(MemoryBackend):
    """Raises mid-``apply`` so the commit's P2 fails after the batch was
    handed over — proving the record and its blob land together or not at all."""

    def apply(self, batch) -> None:
        raise RuntimeError("injected storage failure")


def test_failed_apply_persists_neither_record_nor_blob():
    backend = _FailOnApply()
    store = dc.Store._from_backend(backend)
    store.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    with pytest.raises(RuntimeError, match="injected storage failure"):
        store.commit()
    # Nothing durable: no records, no blobs.
    assert backend._objects == {}
    assert backend._blobs == {}
    store.close()


class _FlakyConn:
    """A thin proxy over a real sqlite3 connection that blows up on the meta
    INSERT — i.e. AFTER apply() has put the objects AND blobs rows in the open
    BEGIN IMMEDIATE txn but BEFORE COMMIT. Everything else delegates, so the
    real ROLLBACK path runs on the real connection."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def executemany(self, sql, seq):
        if sql.startswith("INSERT OR REPLACE INTO meta"):
            raise RuntimeError("injected mid-txn failure")
        return self._conn.executemany(sql, seq)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_sqlite_failed_txn_rolls_back_both(tmp_path):
    """On the real sqlite backend, an exception raised inside apply()'s
    BEGIN IMMEDIATE…COMMIT must roll back the blobs INSERT together with the
    record INSERT (ADR-007: one atomic transaction)."""
    store = dc.Store.open(tmp_path / "store", lock_ttl=0.5)
    backend = store._backend
    real_conn = backend._conn
    backend._conn = _FlakyConn(real_conn)  # apply() now fails at the meta INSERT

    store.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    with pytest.raises(RuntimeError, match="injected mid-txn failure"):
        store.commit()

    backend._conn = real_conn  # restore for the assertions + close
    # The whole transaction rolled back: neither the record nor the blob row.
    objects = real_conn.execute("SELECT count(*) FROM objects").fetchone()
    blobs = real_conn.execute("SELECT count(*) FROM blobs").fetchone()
    assert objects[0] == 0
    assert blobs[0] == 0
    store.close()


# -- schema evolution: inline bytes -> dc.Blob -------------------------------


def _evolve_scan(image_annotation):
    """(Re)define a 'EvolvingScan' entity with a given annotation for `image`,
    same typename across runs (the code-changed-between-runs simulation)."""
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


def test_inline_bytes_to_blob_evolution(store_factory):
    # v1: image is a plain inline `bytes` field.
    V1 = _evolve_scan(bytes)
    store = store_factory()
    store.root = [V1(qid="Q1", image=b"inline-bytes")]
    store.commit()
    store.close()

    # v2: the SAME field is now Annotated[bytes, dc.Blob] (out-of-line).
    V2 = _evolve_scan(Annotated[bytes, dc.Blob])
    reopened = store_factory()
    scan = reopened.root[0]
    assert isinstance(scan, V2)
    # Old inline record decodes by name through its own shape: the bytes come
    # back as raw bytes (no descriptor was ever written), NOT a handle.
    assert scan.image == b"inline-bytes"
    assert not isinstance(scan.image, dc.BlobHandle)

    # A NEW record written under the v2 shape stores out-of-line and reads back
    # as a handle. NOTE: marking a field dc.Blob does NOT mint a new cid (the cid
    # splits on the field-NAME shape, unchanged here), so the asymmetry is
    # value-driven — old payloads carry inline bytes → bytes, new ones a
    # descriptor → handle. migrate() does not yet normalize old inline records
    # into blob rows; cid-shape-aware mode change is a follow-on (#76 sibling).
    reopened.store(V2(qid="Q2", image=b"out-of-line-bytes"))
    reopened.commit()
    reopened.close()

    third = store_factory()
    q1 = third.get(V2, qid="Q1")
    q2 = third.get(V2, qid="Q2")
    assert q1.image == b"inline-bytes"            # still inline → raw bytes
    assert isinstance(q2.image, dc.BlobHandle)    # new shape → handle
    assert q2.image.bytes() == b"out-of-line-bytes"
    third.close()


# -- debug fingerprint net (must not false-positive on blob fields) ----------


def test_debug_mode_does_not_flag_unchanged_blob_entity(recwarn):
    backend = MemoryBackend()
    seed = dc.Store._from_backend(backend, debug=True)
    seed.root = [Scan(qid="Q1", label="azurite", image=IMG)]
    seed.commit()
    # touch nothing, just re-commit a no-op and read — the sweep recomputes the
    # fingerprint and must NOT think the (unchanged) blob entity mutated.
    seed.commit()  # no-op
    seed.close()

    reopened = dc.Store._from_backend(backend, debug=True)
    scan = reopened.root[0]
    assert scan.image.bytes() == IMG  # rehydrate to a handle
    reopened.store(Scan(qid="Q2", label="other", image=b"small"))
    reopened.commit()  # the sweep walks the CLEAN Q1 (a handle) — no warning
    from datacrystal import UntrackedMutationWarning
    assert not any(isinstance(w.message, UntrackedMutationWarning)
                   for w in recwarn.list)
    reopened.close()


# -- marker guards ------------------------------------------------------------


def test_blob_on_non_bytes_field_raises():
    with pytest.raises(TypeError, match="must be bytes"):
        @dc.entity
        class Bad:
            qid: Annotated[str, dc.Unique]
            oops: Annotated[str, dc.Blob] = ""


def test_blob_plus_index_raises():
    with pytest.raises(TypeError, match="cannot also be Index/Unique/SortedIndex"):
        @dc.entity
        class Bad:
            qid: Annotated[str, dc.Unique]
            oops: Annotated[bytes, dc.Blob, dc.Index] = b""


def test_blob_plus_unique_raises():
    with pytest.raises(TypeError, match="cannot also be Index/Unique/SortedIndex"):
        @dc.entity
        class Bad:
            qid: Annotated[str, dc.Unique]
            oops: Annotated[bytes, dc.Blob, dc.Unique] = b""


def test_blob_plus_renamed_from_raises():
    with pytest.raises(TypeError, match="cannot also be FullText/RenamedFrom/Glue"):
        @dc.entity
        class Bad:
            qid: Annotated[str, dc.Unique]
            oops: Annotated[bytes, dc.Blob, dc.RenamedFrom("old")] = b""


def test_optional_bytes_blob_is_allowed():
    @dc.entity
    class Ok:
        qid: Annotated[str, dc.Unique]
        data: Annotated[bytes | None, dc.Blob] = None

    # resolving specs (eager at @entity) did not raise — the field is a blob.
    from datacrystal._entity import type_info
    spec = type_info(Ok).spec("data")
    assert spec is not None and spec.blob


# -- regression: re-committing a REOPENED blob entity (adversarial review) ----
# The encode/upsert paths must understand a hydrated BlobHandle, or a reopened
# blob entity cannot be updated and upsert silently re-stores/wipes the blob.


def test_edit_sibling_field_of_reopened_blob_preserves_blob(store_factory):
    store = store_factory()
    store.root = [Scan(qid="S1", label="draft", image=IMG)]
    store.commit()
    store.close()

    reopened = store_factory()
    scan = reopened.get(Scan, qid="S1")
    assert isinstance(scan.image, dc.BlobHandle)   # reopened → a handle, not bytes
    scan.label = "final"                            # edit a SIBLING field
    reopened.commit()                               # must NOT raise (was the crash)
    reopened.close()

    third = store_factory()
    scan = third.get(Scan, qid="S1")
    assert scan.label == "final"
    assert scan.image.bytes() == IMG               # the blob survived untouched
    assert scan.image.hash == hashlib.sha256(IMG).digest()
    third.close()


def test_upsert_resupplying_identical_blob_does_not_restore(store_factory):
    store = store_factory()
    store.root = []
    store.upsert(Scan(qid="S1", label="v1", image=IMG))
    store.commit()
    oid1 = store.get(Scan, qid="S1").image.blob_oid

    store.upsert(Scan(qid="S1", label="v2", image=IMG))   # same image, new label
    store.commit()
    scan = store.get(Scan, qid="S1")
    assert scan.label == "v2"
    assert scan.image.bytes() == IMG
    assert scan.image.blob_oid == oid1               # identical blob → not re-stored
    store.close()


def test_upsert_with_a_changed_blob_restores(store_factory):
    store = store_factory()
    store.root = []
    store.upsert(Scan(qid="S1", label="v1", image=IMG))
    store.commit()
    oid1 = store.get(Scan, qid="S1").image.blob_oid

    bigger = IMG + b"!"
    store.upsert(Scan(qid="S1", label="v1", image=bigger))
    store.commit()
    scan = store.get(Scan, qid="S1")
    assert scan.image.bytes() == bigger
    assert scan.image.blob_oid != oid1               # changed → a fresh blob OID
    store.close()


def test_unmarking_a_blob_field_errors_clearly(store_factory):
    # store a blob, then re-declare the field as plain `bytes` (drop dc.Blob):
    # the old descriptor record hydrates to a handle the inline encoder can't
    # write — fail loudly with the remedy, never a cryptic msgpack error.
    blobbed = _evolve_scan(Annotated[bytes, dc.Blob])
    store = store_factory()
    store.root = [blobbed(qid="Q1", image=b"x" * 100)]
    store.commit()
    store.close()

    _evolve_scan(bytes)  # same typename, field now plain bytes
    reopened = store_factory()
    scan = reopened.get(_evolve_scan(bytes), qid="Q1")
    reopened.mark_dirty(scan)
    with pytest.raises(TypeError, match="un-marked dc.Blob"):
        reopened.commit()
    reopened.close()
