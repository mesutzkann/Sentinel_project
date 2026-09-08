"""The lexical half of retrieval: BM25 over the chunks, held in memory.

Dense retrieval is bad at exactly the things an incident query is made of — `MaxPoolSize`,
`40P01`, `NpgsqlException`, `PaymentProcessor.cs:183`. An embedding places those near their
neighbours in meaning, and there is no meaning in an error code. BM25 matches them literally,
which is why the two are fused rather than one being picked.

Two decisions worth stating:

*The scoring is written out rather than taken from ``rank_bm25``.* The textbook Okapi IDF,
``log((N - df + 0.5) / (df + 0.5))``, goes **negative** once a term appears in more than half
the corpus, so a chunk can be pushed down for containing a query term. Over a few hundred
chunks about five services, "orders" and "service" are in more than half of them. ``rank_bm25``
patches around it with an epsilon floor; Lucene solved it properly by adding one inside the log,
which is what is used here.

*Identifiers are indexed twice.* `MaxPoolSize` is also indexed as `max`, `pool`, `size`, and
`Npgsql.NpgsqlException` as its dotted parts. Without that, "max pool size" — how a person asks
— matches nothing, and the term that would have found the runbook is the one term the query
does not contain.
"""

from __future__ import annotations

import abc
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from rag.documents import RetrievedChunk, StoredChunk
from rag.filters import matches

# Words, numbers, and the punctuation that holds identifiers together: `payment_processor`,
# `PaymentProcessor.cs`, `40P01`, `p99`. Splitting on those would destroy the terms that make
# lexical search worth having.
_TOKEN = re.compile(r"[A-Za-zÇĞİÖŞÜçğıöşü0-9]+(?:[._-][A-Za-zÇĞİÖŞÜçğıöşü0-9]+)*")

# The letter classes include the Turkish letters rather than being ASCII-only. With `[A-Z]`
# alone, `İSTEK` splits into `İ` (unmatched) and `STEK`, and the index gains a term that is a
# word with its first letter missing.
_UPPER = "A-ZÇĞİÖŞÜ"
_LOWER = "a-zçğıöşü"
_CAMEL = re.compile(rf"[{_UPPER}]?[{_LOWER}]+|[{_UPPER}]{{2,}}(?![{_LOWER}])|\d+")

# Free parameters, at their standard values. k1 controls how quickly repeated terms stop adding
# score, b how strongly long chunks are penalised.
_K1 = 1.5
_B = 0.75


def tokenize(text: str) -> list[str]:
    """Text to search terms, keeping identifiers both whole and split.

    Lowercasing is the invariant kind: Python's ``str.lower`` maps Turkish ``İ`` to ``i`` plus a
    combining dot, which then fails to match a plain ``i``. The dot is stripped so a Turkish
    query and an English document meet on the same token.
    """
    terms: list[str] = []

    for match in _TOKEN.finditer(text):
        raw = match.group(0)
        whole = _fold(raw)
        terms.append(whole)

        parts = [_fold(p) for p in _CAMEL.findall(raw)]
        parts += [_fold(p) for p in re.split(r"[._-]", raw) if p]

        # Only when they add something: splitting `orders` yields `orders` again, and indexing a
        # term twice inflates its frequency for no gain.
        terms.extend(p for p in parts if p and p != whole)

    return terms


def _fold(text: str) -> str:
    return text.lower().replace("̇", "")


class LexicalIndex(abc.ABC):
    """A keyword index over the chunk corpus."""

    @abc.abstractmethod
    def build(self, chunks: list[StoredChunk]) -> None:
        """Replace the index contents. Called at startup and after every ingest."""

    @abc.abstractmethod
    def search(
        self,
        query: str,
        k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """The ``k`` best-matching chunks, best first."""

    @property
    @abc.abstractmethod
    def size(self) -> int:
        """How many chunks are indexed."""


@dataclass
class Bm25Index(LexicalIndex):
    """BM25 over an in-memory posting list.

    Rebuilt wholesale rather than updated incrementally. The corpus is a few hundred chunks and
    a rebuild is milliseconds; an incremental index would need document-frequency bookkeeping
    that can drift out of step with the store, and a lexical index that disagrees with the
    vector store about what exists is a bug that only shows up as slightly wrong rankings.
    """

    _chunks: list[StoredChunk] = field(default_factory=list)
    _lengths: list[int] = field(default_factory=list)
    _frequencies: list[Counter[str]] = field(default_factory=list)
    _postings: dict[str, list[int]] = field(default_factory=dict)
    _average_length: float = 0.0

    def build(self, chunks: list[StoredChunk]) -> None:
        self._chunks = list(chunks)
        self._frequencies = [Counter(tokenize(c.content)) for c in self._chunks]
        self._lengths = [sum(f.values()) for f in self._frequencies]
        self._average_length = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0

        postings: dict[str, list[int]] = {}

        for index, frequencies in enumerate(self._frequencies):
            for term in frequencies:
                postings.setdefault(term, []).append(index)

        self._postings = postings

    @property
    def size(self) -> int:
        return len(self._chunks)

    def search(
        self,
        query: str,
        k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        terms = tokenize(query)

        if not terms or not self._chunks:
            return []

        scores: dict[int, float] = {}

        for term in set(terms):
            documents = self._postings.get(term)

            if not documents:
                continue

            idf = self._idf(len(documents))

            for index in documents:
                # Filtering here rather than after scoring: the filter is part of the query, and
                # scoring chunks that cannot be returned is work whose only output is discarded.
                if filters and not matches(self._chunks[index].metadata, filters):
                    continue

                scores[index] = scores.get(index, 0.0) + idf * self._saturation(term, index)

        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:k]

        return [
            RetrievedChunk(chunk=self._chunks[index], score=score, rank=rank, retriever="bm25")
            for rank, (index, score) in enumerate(ordered, start=1)
        ]

    def _idf(self, document_frequency: int) -> float:
        """Lucene's BM25 IDF: always positive, so a matched term never costs a chunk score."""
        n = len(self._chunks)

        return math.log(1.0 + (n - document_frequency + 0.5) / (document_frequency + 0.5))

    def _saturation(self, term: str, index: int) -> float:
        """The term-frequency half of BM25, normalised for chunk length."""
        frequency = self._frequencies[index][term]
        length_ratio = (
            self._lengths[index] / self._average_length if self._average_length else 1.0
        )

        return (frequency * (_K1 + 1)) / (frequency + _K1 * (1 - _B + _B * length_ratio))
