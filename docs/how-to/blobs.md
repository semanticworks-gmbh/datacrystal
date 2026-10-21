# How-to: store binary blobs

After this page you can keep large binaries out of your entity records and read them whole or streamed.

```python
from pathlib import Path
from typing import Annotated

import datacrystal as dc

@dc.entity
class Photo:
    number: Annotated[str, dc.Unique]
    jpeg: Annotated[bytes, dc.Blob]
    thumbnail: Annotated[bytes | None, dc.Blob] = None

with dc.Store.open("cabinet.store") as store:
    if store.root is None:
        store.root = {"seeded": True}
        store.store(Photo(number="azurite-1", jpeg=b"\xff\xd8" + bytes(5000)))
        store.commit()

    photo = store.get(Photo, number="azurite-1")
    print(photo.jpeg.size, photo.jpeg.hash.hex()[:8])
    print(len(photo.jpeg.bytes()))

    with store.open_blob(photo, "jpeg") as stream:
        print(stream.read(2))
        stream.seek(-4, 2)
        print(len(stream.read()))

    Path("scan.jpg").write_bytes(bytes(10_000))
    photo.jpeg = dc.blob_from_path("scan.jpg")  # type: ignore
    store.commit()
    print(photo.jpeg.size)
```

- A plain `bytes` field is stored inline, so every hydration, commit and scan of that type carries the bytes; use it for a few hundred KB at most.
  There is no automatic spill threshold: mark the field `dc.Blob` when the bytes would bloat a scan of the owning type.
- A `dc.Blob` field keeps a 48-byte descriptor (`blob_oid`, size, sha256) in the record, and the bytes go to a sibling `blobs` table in the same commit transaction.
  A crash leaves the record and its blob both present or both absent, and `query`, `count` and `pluck` never touch the bytes.
- A blob is immutable: reassigning the field writes a new blob under a new OID.
- On read the field is a `dc.BlobHandle`; `.size` and `.hash` come from the descriptor without a fetch, `.bytes()` fetches the whole value once and caches it.
- `store.open_blob(entity, field)` returns an `io.BufferedReader` over a private read view, so you may keep reading it from another thread while the owner commits.
  Close it promptly, because it pins a read transaction; a snapshot has the twin `snapshot.open_blob(view, field)`.
- To write without holding the bytes in memory, assign a `dc.BlobSource(size, open_chunks)`.
  The size must be known up front, and `open_chunks` must return a fresh iterable on each call, because the engine reads it twice: once to hash and length-check before the commit takes its TID, once to fill the cell.
  A one-shot iterator or a wrong size fails loudly and changes nothing.
- A single blob is capped at SQLite's cell limit of about 954 MiB and fails loudly beyond it; an unbounded stream needs a chunked layout, `[planned — #76]`.
- A type checker sees `bytes` on the field, so assigning a `BlobSource` needs a line-level ignore; the other checker quirks are in [Typing](../reference.md#typing).
- If the binary is a thing in its own right (dedup by hash, metadata, several owners), make it its own `@entity` with a `dc.Blob` field and an `Annotated[str, dc.Unique]` sha256, and reference it through `dc.Lazy`.
  Core does no content-addressing, because that would force refcounting and garbage collection.

Reference: [Storing binary blobs](../reference.md#storing-binary-blobs).
Rationale: [ADR-007](../design/ADR-007-blob-fields.md).
