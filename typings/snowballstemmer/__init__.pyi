"""Minimal local type stub for snowballstemmer (ships no ``py.typed`` as of 3.x).

datacrystal[fts] is the only consumer; this covers exactly the surface
``datacrystal.fts`` uses — ``stemmer(lang).stemWords(...)`` and
``algorithms()``. Dev-only: lives under ``typings/`` (pyright ``stubPath``),
so it types the strict-src gate without ever shipping in the wheel.
"""

from __future__ import annotations

class _Stemmer:
    def stemWord(self, word: str) -> str: ...
    def stemWords(self, words: list[str]) -> list[str]: ...

def stemmer(algorithm: str) -> _Stemmer: ...
def algorithms() -> list[str]: ...
