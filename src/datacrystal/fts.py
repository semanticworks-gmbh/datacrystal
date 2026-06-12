"""``datacrystal[fts]`` — the SQLite FTS5 full-text sidecar (ROADMAP item 10).

The first *real* COMMIT-DELTA-v1 consumer: it rides the watermark pipeline
exactly like the M3 contract spike (``tests/contract/fts_consumer.py``, its
embryo) and delivers what the spike's honesty notes deferred — index-time
Snowball stemming per ``dc.FullText(language=...)``, original + stemmed
columns per field (snippets vs. ranking), BM25-ranked search with snippets,
and the snapshot-bootstrap recipe for attaching to a lived-in store.

Sidecar doctrine (COMMIT-DELTA-v1 §5, invariant 11): the index is derived
data in its own SQLite file — rebuildable from ``store.snapshot()`` at any
time, never inside the store's commit transaction, and applied O(delta):
watermark, type lineage and document rows move in ONE sidecar transaction
per delta, so a crash mid-apply rolls back whole and replay resumes from
the persisted watermark.

Design notes (decided 2026-06-12, with the extra's resequencing pre-tag):

* **Two columns per prose field.** ``f_<field>`` holds the original text —
  snippets and exact-term ranking come from here; ``s_<field>`` holds the
  Snowball-stemmed tokens — recall ("Kristalle" finds "Kristall") comes
  from here. Queries match both, BM25 weights prefer exact (2.0 vs 1.0).
* **Stemming is index-time, in Python.** sqlite3 cannot load custom FTS5
  tokenizers, so both sides of the match — column content and query terms —
  run through the same fold-lowercase-stem transform; consistency is by
  construction. Bare ``dc.FullText`` (no language) means fold-only exact
  matching, exactly what the spike shipped.
* **Documents are rows, rowid = OID.** OIDs are globally unique across
  types (partitioned 64-bit space), so one table indexes every configured
  type; ``typename`` is a filterable stored column.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from datacrystal._entity import TYPES_BY_NAME
from datacrystal._errors import DataCrystalError
from datacrystal._records import decode_payload
from datacrystal.contract.applier import (
    CONTRACT_VERSION,
    FORMAT_MARKER,
    DeltaFormatError,
    DeltaGapError,
)

try:
    import snowballstemmer
except ImportError as exc:  # pragma: no cover — core-only installs
    raise ImportError(
        "datacrystal.fts requires the [fts] extra — install with "
        "`pip install 'datacrystal[fts]'` (adds snowballstemmer)"
    ) from exc

__all__ = ["FullTextIndex", "SearchHit", "FtsConfigError"]

SIDECAR_FORMAT = "datacrystal-fts-sidecar"
SIDECAR_VERSION = 1

# ISO-639-1 aliases for the Snowball algorithm names; full names pass through.
_ISO_TO_SNOWBALL = {
    "ar": "arabic", "da": "danish", "de": "german", "el": "greek",
    "en": "english", "es": "spanish", "et": "estonian", "eu": "basque",
    "fa": "persian", "fi": "finnish", "fr": "french", "ga": "irish",
    "hi": "hindi", "hu": "hungarian", "hy": "armenian", "id": "indonesian",
    "it": "italian", "lt": "lithuanian", "ne": "nepali", "nl": "dutch",
    "no": "norwegian", "pl": "polish", "pt": "portuguese", "ro": "romanian",
    "ru": "russian", "sr": "serbian", "sv": "swedish", "ta": "tamil",
    "tr": "turkish", "yi": "yiddish",
}

_TOKEN = re.compile(r"[^\W_]+")  # letters + digits, the unicode61 shape
_PHRASE = re.compile(r'"([^"]*)"')


class FtsConfigError(DataCrystalError):
    """The sidecar's configuration is unusable or contradicts what this
    sidecar file was built with — rebuild rather than guess (invariant 11)."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One ranked match: ``score`` is ``-bm25`` (higher = more relevant);
    ``snippets`` maps field name → excerpt of the original text with the
    matched surface forms marked ``[`` … ``]`` — highlighting is computed
    by the same fold+stem transform that indexed the text, so a stemmed
    match ("Kristall" finding "Kristalle") highlights correctly, which
    FTS5's own ``snippet()`` cannot do over a stem column."""

    oid: int
    typename: str
    score: float
    snippets: Mapping[str, str]

    @property
    def snippet(self) -> str | None:
        """The first excerpt, or None when the match has no surface form to
        excerpt (only fields without stored text matched)."""
        for text in self.snippets.values():
            return text
        return None


def _fold_token(token: str) -> str:
    """NFKD-fold diacritics + lowercase one token — the unicode61 shape.
    Snowball stemmers are case-sensitive ("BERGE" does not stem, "berge"
    does), so lowercasing first is correctness, not cosmetics."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", token)
        if not unicodedata.combining(ch)
    ).lower()


def _fold(text: str) -> list[str]:
    """Tokenize the way unicode61 will: fold the whole text first (so
    decomposed combining marks never split a token), then split on
    non-alphanumerics."""
    folded = "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    ).lower()
    return _TOKEN.findall(folded)


def _resolve_language(language: str | None) -> str | None:
    if language is None:
        return None
    name = _ISO_TO_SNOWBALL.get(language.lower(), language.lower())
    if name not in snowballstemmer.algorithms():
        raise FtsConfigError(
            f"unknown FullText language {language!r} — use an ISO code "
            f"({', '.join(sorted(_ISO_TO_SNOWBALL))}) or a Snowball "
            f"algorithm name ({', '.join(sorted(snowballstemmer.algorithms()))})"
        )
    return name


def _registry_fulltext() -> dict[str, dict[str, str | None]]:
    """Derive typename → {field: language} from every registered ``@entity``
    class carrying ``dc.FullText`` markers — the declaration lives in code,
    exactly like ``dc.Index`` (the engine records it, the extra acts on it)."""
    out: dict[str, dict[str, str | None]] = {}
    for typename, ti in TYPES_BY_NAME.items():
        fields = {
            spec.name: spec.fulltext_language for spec in ti.specs if spec.fulltext
        }
        if fields:
            out[typename] = fields
    return out


class FullTextIndex:
    """A COMMIT-DELTA-v1 consumer indexing ``dc.FullText`` fields into FTS5.

    Fresh store::

        idx = FullTextIndex("cabinet.fts")     # config from @entity registry
        store.attach(idx)
        ... store.commit() ...
        for hit in idx.search("Kristall"):     # stemming: finds "Kristalle"
            print(hit.score, hit.snippet)

    Lived-in store (deltas are not retained — spec §5)::

        with store.snapshot() as snap:
            idx = FullTextIndex.bootstrap("cabinet.fts", snap)
        store.attach(idx)

    ``fulltext`` overrides the registry-derived configuration with an
    explicit ``{typename: {field: language-or-None}}`` map. The
    configuration is persisted; reopening the same file with a different
    one raises :class:`FtsConfigError` — a half-matching index is stale
    derived data, rebuild it (invariant 11).
    """

    def __init__(self, path: str | Path, *,
                 fulltext: Mapping[str, Mapping[str, str | None]] | None = None,
                 _wipe: bool = False) -> None:
        config = fulltext if fulltext is not None else _registry_fulltext()
        self._fulltext: dict[str, dict[str, str | None]] = {
            typename: {field: _resolve_language(lang) for field, lang in fields.items()}
            for typename, fields in config.items()
        }
        if not self._fulltext:
            raise FtsConfigError(
                "nothing to index: no @entity class declares dc.FullText fields "
                "and no explicit fulltext= map was given"
            )
        self._fields: tuple[str, ...] = tuple(sorted({
            field for fields in self._fulltext.values() for field in fields
        }))
        self._stemmers: dict[str, Any] = {}
        self._languages_by_field: dict[str, tuple[str, ...]] = {
            field: tuple(sorted({
                lang for fields in self._fulltext.values()
                for f, lang in fields.items() if f == field and lang is not None
            }))
            for field in self._fields
        }
        if _wipe and not str(path).startswith(":memory:"):
            Path(path).unlink(missing_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._create_or_check_schema()
        row = self._conn.execute(
            "SELECT value FROM sidecar_meta WHERE key='watermark'"
        ).fetchone()
        self._watermark = int(row[0]) if row else 0
        self._types: dict[int, tuple[str, list[str]]] = {
            cid: (name, fields.split("\x1f") if fields else [])
            for cid, name, fields in self._conn.execute(
                "SELECT cid, name, fields FROM sidecar_types"
            )
        }
        self.statements = 0  # per-apply statement count (O(delta) evidence)

    # -- consumer surface (COMMIT-DELTA-v1 §4) --------------------------------

    @property
    def watermark(self) -> int:
        """Highest TID fully applied — persisted in the sidecar file, in the
        same transaction as each applied delta (§4.3)."""
        return self._watermark

    def apply(self, delta: dict[str, Any]) -> bool:
        if delta.get("f") != FORMAT_MARKER:
            raise DeltaFormatError(f"not a datacrystal delta: f={delta.get('f')!r}")
        if delta["v"] > CONTRACT_VERSION:
            raise DeltaFormatError(
                f"delta version {delta['v']} is newer than this index "
                f"supports ({CONTRACT_VERSION}); upgrade datacrystal[fts]"
            )
        tid = delta["tid"]
        if tid <= self._watermark:
            return False  # §4.2: apply-twice ≡ apply-once
        if tid != self._watermark + 1:
            raise DeltaGapError(
                f"delta tid {tid} skips past watermark {self._watermark} — "
                "deltas are not retained; rebuild via FullTextIndex.bootstrap()"
            )
        self.statements = 0
        self._exec("BEGIN")
        try:
            for cid, typename, fields in delta["types"]:
                self._types[cid] = (typename, list(fields))
                self._exec(
                    "INSERT OR REPLACE INTO sidecar_types (cid, name, fields) "
                    "VALUES (?, ?, ?)",
                    (cid, typename, "\x1f".join(fields)),
                )
            for op in delta["ops"]:
                self._apply_op(op)
            self._exec(
                "INSERT OR REPLACE INTO sidecar_meta (key, value) "
                "VALUES ('watermark', ?)",
                (str(tid),),
            )
            self._exec("COMMIT")
        except BaseException:
            self._exec("ROLLBACK")
            self._types = {  # the in-memory lineage mirrors durable state only
                cid: (name, fields.split("\x1f") if fields else [])
                for cid, name, fields in self._conn.execute(
                    "SELECT cid, name, fields FROM sidecar_types"
                )
            }
            raise
        self._watermark = tid
        return True

    # -- bootstrap (the §5 mid-life attach recipe) -----------------------------

    @classmethod
    def bootstrap(cls, path: str | Path, snapshot: Any, *,
                  fulltext: Mapping[str, Mapping[str, str | None]] | None = None,
                  ) -> "FullTextIndex":
        """(Re)build the index from one ``store.snapshot()`` — the canonical
        recipe for attaching to a store that already has history, and the
        rebuild path after a detach/staleness refusal. Any existing file at
        ``path`` is replaced: a sidecar that needed a rebuild is stale by
        definition."""
        idx = cls(path, fulltext=fulltext, _wipe=True)
        idx._exec("BEGIN")
        try:
            for cid, typename, fields in snapshot.types:
                idx._types[cid] = (typename, list(fields))
                idx._exec(
                    "INSERT OR REPLACE INTO sidecar_types (cid, name, fields) "
                    "VALUES (?, ?, ?)",
                    (cid, typename, "\x1f".join(fields)),
                )
            for typename in idx._fulltext:
                for view in snapshot.all(typename):
                    values = {
                        field: value
                        for field in idx._fulltext[typename]
                        if isinstance(value := view.fields().get(field), str)
                    }
                    idx._insert_doc(view.oid, typename, values)
            idx._exec(
                "INSERT OR REPLACE INTO sidecar_meta (key, value) "
                "VALUES ('watermark', ?)",
                (str(snapshot.tid),),
            )
            idx._exec("COMMIT")
        except BaseException:
            idx._exec("ROLLBACK")
            raise
        idx._watermark = snapshot.tid
        return idx

    # -- search ----------------------------------------------------------------

    def search(self, query: str, *, cls: type | str | None = None,
               limit: int = 20) -> list[SearchHit]:
        """BM25-ranked full-text matches for ``query``.

        Terms match the original text exactly (case/diacritic-folded) AND
        their Snowball stems against the stemmed columns, per the languages
        declared for each field — exact matches outrank stem-only matches
        (column weights 2.0 vs 1.0). Quoted phrases stay phrases. ``cls``
        narrows to one entity type (class or typename string).
        """
        typename = self._typename_of(cls) if cls is not None else None
        expression = self._match_expression(query, typename)
        if expression is None:
            return []
        needles = self._needles(query)
        weights = "0.0, " + ", ".join("2.0, 1.0" for _ in self._fields)
        originals = ", ".join(f"f_{f}" for f in self._fields)
        sql = (
            f"SELECT rowid, typename, bm25(docs, {weights}) AS r, {originals} "
            f"FROM docs WHERE docs MATCH ?"
        )
        params: list[Any] = [expression]
        if typename is not None:
            sql += " AND typename = ?"
            params.append(typename)
        sql += " ORDER BY r LIMIT ?"
        params.append(limit)
        hits: list[SearchHit] = []
        for row in self._conn.execute(sql, params):
            oid, hit_typename, rank = row[0], row[1], row[2]
            snippets: dict[str, str] = {}
            for field, text in zip(self._fields, row[3:]):
                if not text:
                    continue
                language = self._fulltext.get(hit_typename, {}).get(field)
                excerpt = self._highlight(text, language, needles)
                if excerpt is not None:
                    snippets[field] = excerpt
            hits.append(SearchHit(oid, hit_typename, -rank, snippets))
        return hits

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "FullTextIndex":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"<datacrystal.fts.FullTextIndex watermark={self._watermark} "
            f"types={sorted(self._fulltext)}>"
        )

    # -- internals ----------------------------------------------------------------

    def _create_or_check_schema(self) -> None:
        columns = ", ".join(f"f_{f}, s_{f}" for f in self._fields)
        self._conn.executescript(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
                typename, {columns}, tokenize='unicode61'
            );
            CREATE TABLE IF NOT EXISTS sidecar_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sidecar_types (
                cid INTEGER PRIMARY KEY, name TEXT NOT NULL, fields TEXT NOT NULL
            );
        """)
        config_json = json.dumps(self._fulltext, sort_keys=True)
        stamp = {
            "format": SIDECAR_FORMAT,
            "version": str(SIDECAR_VERSION),
            "config": config_json,
        }
        persisted = dict(self._conn.execute(
            "SELECT key, value FROM sidecar_meta WHERE key IN "
            "('format', 'version', 'config')"
        ))
        if persisted:
            for key, expected in stamp.items():
                if persisted.get(key) != expected:
                    raise FtsConfigError(
                        f"this sidecar file was built with a different {key} "
                        f"({persisted.get(key)!r} vs {expected!r}) — its content "
                        "would be stale for the new configuration; rebuild via "
                        "FullTextIndex.bootstrap()"
                    )
        else:
            self._conn.executemany(
                "INSERT INTO sidecar_meta (key, value) VALUES (?, ?)",
                list(stamp.items()),
            )

    def _exec(self, sql: str, params: tuple = ()) -> None:
        self.statements += 1
        self._conn.execute(sql, params)

    def _typename_of(self, cls: type | str) -> str:
        if isinstance(cls, str):
            return cls
        from datacrystal._entity import type_info  # loud for non-entity classes

        return type_info(cls).typename

    def _stem(self, tokens: list[str], language: str) -> list[str]:
        stemmer = self._stemmers.get(language)
        if stemmer is None:
            stemmer = self._stemmers[language] = snowballstemmer.stemmer(language)
        return stemmer.stemWords(tokens)

    def _prose_values(self, cid: int, payload: bytes) -> tuple[str, dict[str, str]] | None:
        """Decode one record payload to its indexable ``{field: text}`` —
        by NAME through its persisted shape, missing fields filled from the
        live class's defaults exactly like snapshot materialization (so
        incremental indexing ≡ bootstrap-from-snapshot, fitness #13)."""
        known = self._types.get(cid)
        if known is None:
            raise DeltaFormatError(
                f"op references cid {cid} this index never saw — a consumer "
                "joining mid-stream must FullTextIndex.bootstrap() from a snapshot"
            )
        typename, persisted = known
        wanted = self._fulltext.get(typename)
        if not wanted:
            return None
        by_name = dict(zip(persisted, decode_payload(payload)))
        ti = TYPES_BY_NAME.get(typename)
        values: dict[str, str] = {}
        for field in wanted:
            if field in by_name:
                value = by_name[field]
            else:
                factory = None if ti is None else ti.defaults.get(field)
                value = factory() if factory is not None else None
            if isinstance(value, str):
                values[field] = value
        return typename, values

    def _insert_doc(self, oid: int, typename: str, values: dict[str, str]) -> None:
        if not values:
            return
        row: list[str | None] = [typename]
        for field in self._fields:
            text = values.get(field)
            if text is None or field not in self._fulltext[typename]:
                row.extend((None, None))
                continue
            language = self._fulltext[typename][field]
            stemmed = (
                " ".join(self._stem(_fold(text), language))
                if language is not None else None
            )
            row.extend((text, stemmed))
        placeholders = ", ".join("?" for _ in range(1 + 2 * len(self._fields)))
        columns = "typename, " + ", ".join(f"f_{f}, s_{f}" for f in self._fields)
        self._exec(
            f"INSERT INTO docs (rowid, {columns}) VALUES (?, {placeholders})",
            (oid, *row),
        )

    def _apply_op(self, op: dict[str, Any]) -> None:
        kind, oid = op["op"], op["oid"]
        if kind == "upsert":
            self._exec("DELETE FROM docs WHERE rowid = ?", (oid,))
            prose = self._prose_values(op["cid"], op["payload"])
            if prose is not None:
                self._insert_doc(oid, *prose)
        elif kind == "delete":
            if op["prior"] is None:
                raise DeltaFormatError(f"delete of oid {oid} carries no prior")
            self._exec("DELETE FROM docs WHERE rowid = ?", (oid,))
        else:
            raise DeltaFormatError(f"unknown op {kind!r} — refusing to guess")

    def _needles(self, query: str) -> tuple[set[str], dict[str, set[str]]]:
        """What to highlight: the folded query tokens, plus their stems per
        language in use — the exact transform the index applied."""
        tokens = _fold(query.replace('"', " "))
        exact = set(tokens)
        stems = {
            language: set(self._stem(tokens, language))
            for languages in self._languages_by_field.values()
            for language in languages
        }
        return exact, stems

    def _highlight(self, text: str, language: str | None,
                   needles: tuple[set[str], dict[str, set[str]]]) -> str | None:
        """A ±~6-token excerpt of ``text`` around the first matched surface
        form, every matched token marked ``[`` … ``]`` — or None when no
        token of this text matches the query (the row matched via another
        field)."""
        exact, stems_by_language = needles
        stems = stems_by_language.get(language or "", set())
        spans: list[tuple[int, int, bool]] = []
        for m in _TOKEN.finditer(text):
            folded = _fold_token(m.group())
            matched = folded in exact or (
                language is not None
                and bool(stems)
                and self._stem([folded], language)[0] in stems
            )
            spans.append((m.start(), m.end(), matched))
        first = next((i for i, span in enumerate(spans) if span[2]), None)
        if first is None:
            return None
        lo, hi = max(0, first - 5), min(len(spans), first + 7)
        out: list[str] = ["…" if lo > 0 else ""]
        cursor = spans[lo][0]
        for start, end, matched in spans[lo:hi]:
            out.append(text[cursor:start])
            token = text[start:end]
            out.append(f"[{token}]" if matched else token)
            cursor = end
        out.append("…" if hi < len(spans) else text[spans[hi - 1][1]:])
        return "".join(out)

    def _match_expression(self, query: str, typename: str | None) -> str | None:
        """One FTS5 MATCH expression: AND over query terms, each an OR over
        (exact column, per-language stemmed column) alternatives. Terms are
        quoted, so user input can never inject FTS5 operators."""
        scope = (
            self._fulltext if typename is None
            else {typename: self._fulltext.get(typename, {})}
        )
        # a quoted phrase is one multi-token term; everything else single tokens
        terms: list[list[str]] = [
            _fold(phrase) for phrase in _PHRASE.findall(query)
        ]
        terms += [[token] for token in _fold(_PHRASE.sub(" ", query))]
        terms = [tokens for tokens in terms if tokens]
        if not terms:
            return None
        clauses: list[str] = []
        for tokens in terms:
            alternatives: list[str] = []
            for field in self._fields:
                declaring = [
                    fields[field] for fields in scope.values() if field in fields
                ]
                if not declaring:
                    continue
                alternatives.append(f'f_{field} : "{" ".join(tokens)}"')
                for language in self._languages_by_field[field]:
                    if language not in declaring:
                        continue
                    stemmed = self._stem(tokens, language)
                    alternatives.append(f's_{field} : "{" ".join(stemmed)}"')
            if not alternatives:
                return None
            # dedupe (stem may equal the surface form), keep order stable
            unique = list(dict.fromkeys(alternatives))
            clauses.append("(" + " OR ".join(unique) + ")")
        return " AND ".join(clauses)
