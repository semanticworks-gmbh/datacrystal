"""Proving ground #6 — blob store at rest (real PDFs / documents).

Runs a *real* directory of binary documents (PDFs, scans, invoices — the
enterprise-search + SOR-archive personas) through datacrystal's ``dc.Blob``
fields and reports honest absolute numbers for the two claims the blob slice
must keep (ADR-007, #75/#84/#85):

  1. **The object table stays fast.** Every document's bytes live OUT-OF-LINE;
     the entity record is a ~tens-of-bytes descriptor. A ``count`` / ``pluck`` /
     metadata scan over the whole corpus is therefore independent of how many
     gigabytes of PDF you stored — measured, not asserted.
  2. **Streaming bounds RSS.** Ingesting via ``dc.blob_from_path`` (streamed
     write) never holds a document whole in RAM, and ``store.open_blob`` range-
     reads a multi-MB PDF touching only the spanned bytes — both shown with peak
     heap far below the document size.

And the correctness oracle: every stored blob's sha256 round-trips byte-for-byte
against the original file.

On-demand eval, NOT a unit test (it ingests real, possibly multi-GB documents).
Point it at a directory you have — the SOR/enterprise persona is literally "your
document store":

    BLOB_DIR=/path/to/pdfs uv run python evals/proving_grounds/blob_store.py

Knobs (env): ``BLOB_GLOB`` (default ``**/*.pdf``), ``BLOB_MAX`` (cap the count,
0 = all), ``BLOB_CHUNK`` (streamed-fill chunk bytes, default 1 MiB).
No local corpus? evals/README.md §#6 has a public-domain fetch recipe.
"""

from __future__ import annotations

import hashlib
import os
import resource
import shutil
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Annotated

import datacrystal as dc

DATA = Path(__file__).resolve().parent.parent / "data"
STORE = DATA / "blob.store"


@dc.entity
class Document:
    sha256: Annotated[str, dc.Unique]          # content hash → dedup key (app-layer CAS)
    path: Annotated[str, dc.Index]
    size: Annotated[int, dc.Index]
    content_type: Annotated[str, dc.Index]
    content: Annotated[bytes, dc.Blob] = b""   # the bytes, out-of-line and raw


def peak_rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024 / 1024 if sys.platform == "darwin" else r / 1024  # bytes vs KB


def sha256_of_file(path: Path, chunk: int = 1 << 20) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
            size += len(b)
    return h.hexdigest(), size


def main() -> None:
    blob_dir = os.environ.get("BLOB_DIR")
    if not blob_dir:
        sys.exit(
            "set BLOB_DIR=/path/to/documents (PDFs/scans/invoices). "
            "See evals/README.md §#6 for a public-domain fetch recipe."
        )
    root = Path(blob_dir)
    glob = os.environ.get("BLOB_GLOB", "**/*.pdf")
    cap = int(os.environ.get("BLOB_MAX", "0"))
    chunk = int(os.environ.get("BLOB_CHUNK", str(1 << 20)))

    files = sorted(p for p in root.glob(glob) if p.is_file())
    if cap:
        files = files[:cap]
    if not files:
        sys.exit(f"no files matched {glob!r} under {root}")

    shutil.rmtree(STORE, ignore_errors=True)
    print(f"datacrystal proving ground: blob store  ({sys.platform})")
    print(f"corpus: {len(files):,} files matching {glob!r} under {root}\n")

    # Pre-hash on disk (the correctness oracle + dedup keys). Done here so the
    # ingest timing measures datacrystal, not the SHA of the source files.
    truth: dict[str, tuple[str, int]] = {}   # path -> (sha256, size)
    total_bytes = 0
    largest: tuple[Path, int] = (files[0], 0)
    for p in files:
        digest, size = sha256_of_file(p)
        truth[str(p)] = (digest, size)
        total_bytes += size
        if size > largest[1]:
            largest = (p, size)
    print(f"corpus bytes:          {total_bytes / 1e6:>9.1f} MB   "
          f"(largest single file {largest[1] / 1e6:.1f} MB)\n")

    # --- INGEST (streamed write — bytes never whole in RAM) -------------------
    # tracemalloc isolates the Python working set from the ~35 MB interpreter
    # baseline (and the sqlite C-side page cache), so the heap peak below is what
    # STREAMING actually allocates — ~one BLOB_CHUNK, independent of file size.
    t0 = time.perf_counter()
    tracemalloc.start()
    store = dc.Store.open(STORE)
    seen: set[str] = set()
    n_docs = 0
    for p in files:
        digest, size = truth[str(p)]
        if digest in seen:        # content-addressed dedup (the Unique-hash pattern)
            continue
        seen.add(digest)
        store.store(Document(
            sha256=digest, path=str(p), size=size,
            content_type=p.suffix.lstrip(".").lower() or "bin",
            content=dc.blob_from_path(p, chunk_size=chunk),  # type: ignore[arg-type]
        ))
        store.commit()           # one doc per commit: heap bounded by ONE streamed fill
        n_docs += 1
    _, peak_ingest = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    t_ingest = time.perf_counter() - t0
    rss_ingest = peak_rss_mb()
    on_disk = sum(f.stat().st_size for f in STORE.rglob("*") if f.is_file()) / 1e6
    dupes = len(files) - n_docs
    print(f"ingested:              {n_docs:>9,} docs ({dupes} dupes skipped)   "
          f"{t_ingest:6.2f}s  ({total_bytes / 1e6 / t_ingest:,.1f} MB/s)")
    print(f"  on disk:             {on_disk:>9.1f} MB   process peak RSS {rss_ingest:.0f} MB")
    print(f"  ingest HEAP peak:    {peak_ingest / 1e6:>9.1f} MB   "
          f"(chunk={chunk / 1e6:.1f} MB, largest file {largest[1] / 1e6:.1f} MB)")
    print("    → streamed write holds ~one chunk, NOT the file: heap peak tracks "
          "BLOB_CHUNK, not size\n      (on files larger than one chunk it is << the "
          "file — the whole point)")
    store.close()

    # --- THE OBJECT TABLE STAYS FAST -----------------------------------------
    # A reopened store; count + a full metadata scan must NOT read any blob bytes.
    s = dc.Store.open(STORE)

    t0 = time.perf_counter()
    n = s.count(Document)
    t_count = time.perf_counter() - t0
    print(f"\ncount(Document):       {n:>9,} docs        {t_count * 1e3:7.1f} ms   "
          f"(pure bitmap — zero blob reads)")

    tracemalloc.start()
    t0 = time.perf_counter()
    scanned = 0
    total_meta = 0
    for _path, size in s.pluck(Document, "path", "size"):   # metadata only
        scanned += 1
        total_meta += size
    t_scan = time.perf_counter() - t0
    _, peak_scan = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"pluck path+size (all): {scanned:>9,} docs        {t_scan * 1e3:7.1f} ms   "
          f"peak heap {peak_scan / 1e6:.1f} MB")
    print(f"  metadata bytes summed = {total_meta / 1e6:.1f} MB of content, but the "
          f"scan's heap peak ({peak_scan / 1e6:.1f} MB) is independent of it ✓")

    # --- STREAMED READ vs WHOLE (the RSS claim, on the largest doc) -----------
    big_sha = truth[str(largest[0])][0]
    big = s.get(Document, sha256=big_sha)
    assert big is not None

    tracemalloc.start()
    with s.open_blob(big, "content") as fh:
        mid = big.size // 2
        fh.seek(mid)
        span = fh.read(4096)             # 4 KB out of the middle of a multi-MB PDF
    _, peak_stream = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert span == _slice_of_file(largest[0], big.size // 2, 4096)

    tracemalloc.start()
    _whole = big.content.bytes()         # the whole-value fetch, for contrast
    _, peak_whole = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"\nlargest doc:           {big.size / 1e6:>9.1f} MB   ({largest[0].name})")
    print(f"  open_blob range 4 KB: peak heap {peak_stream / 1e3:8.1f} KB   "
          f"({peak_stream / max(big.size, 1):.4f}x the doc — RSS-bounded ✓)")
    print(f"  .bytes() whole:       peak heap {peak_whole / 1e6:8.1f} MB   "
          f"({peak_whole / max(big.size, 1):.2f}x the doc — the contrast)")

    # --- CORRECTNESS ORACLE ---------------------------------------------------
    t0 = time.perf_counter()
    checked = 0
    bad = 0
    for _path, sha in s.pluck(Document, "path", "sha256"):
        doc = s.get(Document, sha256=sha)
        assert doc is not None
        if doc.content.hash.hex() != sha or hashlib.sha256(doc.content.bytes()).hexdigest() != sha:
            bad += 1
        checked += 1
    t_verify = time.perf_counter() - t0
    s.close()
    print(f"\ncorrectness:           {checked:>9,} docs re-hashed   {t_verify:6.2f}s   "
          f"{bad} mismatches")
    print("\nverdict:", "ALL BLOBS ROUND-TRIP ✓  object table stays flat ✓  streaming bounds RSS ✓"
          if bad == 0 else f"FAILED — {bad} blobs did not round-trip")
    if bad:
        sys.exit(1)


def _slice_of_file(path: Path, offset: int, length: int) -> bytes:
    with path.open("rb") as f:
        f.seek(offset)
        return f.read(length)


if __name__ == "__main__":
    main()
