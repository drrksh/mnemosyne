"""
BM25F lexical index.

A compact inverted index over per-field weighted term frequencies
(Robertson & Zaragoza's BM25F simplification: field boosts are folded
into term frequency and document length before the usual BM25
saturation). Scores are additionally normalized to an *absolute* [0, 1]
scale by dividing by the best score the query could possibly achieve,
so a weak best match stays weak instead of being stretched to 1.0 —
which is what makes the lexical signal fusable with cosine similarity.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from mnemosyne.retrieval.documents import CONCEPT_PREFIX, FIELD_BOOSTS, IndexedFields

#: Query-side weight of expanded concept terms relative to literal terms.
CONCEPT_QUERY_WEIGHT = 0.35


@dataclass
class Bm25Index:
    k1: float = 1.2
    b: float = 0.75
    postings: dict[str, list[tuple[int, float]]] = field(default_factory=lambda: defaultdict(list))
    doc_lengths: list[float] = field(default_factory=list)

    @classmethod
    def build(cls, documents: Iterable[IndexedFields], *, k1: float = 1.2, b: float = 0.75) -> Bm25Index:
        index = cls(k1=k1, b=b)
        for doc_id, fields in enumerate(documents):
            weighted: dict[str, float] = defaultdict(float)
            length = 0.0
            for name, tokens in fields.items():
                boost = FIELD_BOOSTS[name]
                for token in tokens:
                    weighted[token] += boost
                length += boost * len(tokens)
            for term, wtf in weighted.items():
                index.postings[term].append((doc_id, wtf))
            index.doc_lengths.append(length)
        index.postings = dict(index.postings)
        return index

    @property
    def size(self) -> int:
        return len(self.doc_lengths)

    @property
    def avg_length(self) -> float:
        return (sum(self.doc_lengths) / len(self.doc_lengths)) if self.doc_lengths else 0.0

    def idf(self, term: str) -> float:
        df = len(self.postings.get(term, ()))
        n = self.size
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def score(self, terms: Sequence[str]) -> dict[int, float]:
        """doc_id -> normalized score in [0, 1] for every doc matching any
        term. Duplicate query terms count once."""
        if not terms or not self.size:
            return {}
        weights: dict[str, float] = {}
        for term in terms:
            weight = CONCEPT_QUERY_WEIGHT if term.startswith(CONCEPT_PREFIX) else 1.0
            weights[term] = max(weights.get(term, 0.0), weight)

        avgdl = self.avg_length or 1.0
        k1, b = self.k1, self.b
        raw: dict[int, float] = defaultdict(float)
        max_possible = 0.0
        for term, q_weight in weights.items():
            idf = self.idf(term)
            max_possible += q_weight * idf * (k1 + 1.0)
            for doc_id, wtf in self.postings.get(term, ()):
                norm = k1 * (1.0 - b + b * self.doc_lengths[doc_id] / avgdl)
                raw[doc_id] += q_weight * idf * (wtf * (k1 + 1.0)) / (wtf + norm)
        if max_possible <= 0:
            return {}
        return {doc_id: min(1.0, value / max_possible) for doc_id, value in raw.items()}
