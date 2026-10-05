"""
RetrievalIndex: an immutable, query-ready snapshot of one principal's
active memories (lexical index + embedding matrix), and IndexCache,
which reuses snapshots for as long as the database says nothing changed.

Retrieval is always per-principal (personal memories plus their teams'
memories), so each snapshot covers a bounded corpus — typically
hundreds to tens of thousands of memories — and brute-force cosine over
a contiguous float32 matrix is both exact and fast at that size. A
larger deployment would swap this module for an ANN index behind the
same `search_vectors` contract; nothing above it would change.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

import numpy as np

from mnemosyne.domain.entities import Memory
from mnemosyne.repositories.interfaces import IndexSignature
from mnemosyne.retrieval.bm25 import Bm25Index
from mnemosyne.retrieval.documents import memory_fields, query_terms

logger = logging.getLogger("mnemosyne.retrieval.index")


@dataclass
class RetrievalIndex:
    model_id: str
    memories: list[Memory]
    #: (n, dim) float32, L2-normalized rows; zero rows where no embedding.
    vectors: np.ndarray
    has_vector: np.ndarray
    bm25: Bm25Index
    position: dict[UUID, int] = field(default_factory=dict)

    @classmethod
    def build(
        cls, *, model_id: str, memories: list[Memory], vectors: np.ndarray, has_vector: np.ndarray
    ) -> RetrievalIndex:
        return cls(
            model_id=model_id,
            memories=memories,
            vectors=vectors,
            has_vector=has_vector,
            bm25=Bm25Index.build(memory_fields(m) for m in memories),
            position={m.id: i for i, m in enumerate(memories)},
        )

    @property
    def size(self) -> int:
        return len(self.memories)

    def lexical_scores(self, query: str) -> np.ndarray:
        scores = np.zeros(self.size, dtype=np.float32)
        for doc_id, value in self.bm25.score(query_terms(query)).items():
            scores[doc_id] = value
        return scores

    def semantic_scores(self, query_vector: Optional[np.ndarray]) -> np.ndarray:
        if query_vector is None or not self.size or not np.any(query_vector):
            return np.zeros(self.size, dtype=np.float32)
        sims = self.vectors @ query_vector.astype(np.float32)
        return np.clip(sims, 0.0, 1.0)

    def similarity(self, i: int, j: int) -> float:
        if self.has_vector[i] and self.has_vector[j]:
            return float(self.vectors[i] @ self.vectors[j])
        return 0.0


class IndexCache:
    """Thread-safe LRU of RetrievalIndex snapshots keyed by
    (visibility, embedding model) and validated by IndexSignature.

    The signature comes from the database on every lookup, so a write
    made by *another* process (a CLI command, a hook, a second API
    worker) is noticed on the next query — no invalidation messages
    needed."""

    def __init__(self, max_entries: int = 64) -> None:
        self._max = max_entries
        self._entries: OrderedDict[str, tuple[IndexSignature, RetrievalIndex]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str, signature: IndexSignature) -> Optional[RetrievalIndex]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] != signature:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry[1]

    def put(self, key: str, signature: IndexSignature, index: RetrievalIndex) -> None:
        with self._lock:
            self._entries[key] = (signature, index)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
