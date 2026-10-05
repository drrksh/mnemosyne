"""
MemoryIndexer: keeps stored embeddings in step with memory content.

Services call `index()` inside the same UnitOfWork that writes the
memory, so a memory and its embedding commit together. If the embedder
fails (a remote embedding API is down, say), the memory is still saved
and the failure is logged: retrieval degrades to lexical-only for that
memory, and the next index load or `mnemosyne maintain` fills the gap.
Losing the ability to *store* a memory because an optional ranking
signal is unavailable would be the wrong trade.
"""
from __future__ import annotations

import logging
from typing import Sequence

import numpy as np

from mnemosyne.db.unit_of_work import UnitOfWork
from mnemosyne.domain.entities import Memory
from mnemosyne.repositories.interfaces import StoredEmbedding
from mnemosyne.retrieval.documents import embedding_text, embedding_text_hash
from mnemosyne.retrieval.embeddings import Embedder, pack_vector, unpack_vector

logger = logging.getLogger("mnemosyne.retrieval.indexer")


class MemoryIndexer:
    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder

    @property
    def model_id(self) -> str:
        return self.embedder.model_id

    def index(self, uow: UnitOfWork, memory: Memory) -> None:
        self.index_many(uow, [memory])

    def index_many(self, uow: UnitOfWork, memories: Sequence[Memory]) -> int:
        """Embed and store vectors for `memories`; returns how many were
        written. Never raises for embedder failures."""
        if not memories:
            return 0
        try:
            matrix = self.embedder.embed_documents([embedding_text(m) for m in memories])
        except Exception:
            logger.warning(
                "embedding_failed_degrading_to_lexical",
                extra={"model": self.model_id, "count": len(memories)},
                exc_info=True,
            )
            return 0
        uow.embeddings.upsert_many(
            [
                StoredEmbedding(
                    memory_id=memory.id,
                    model=self.model_id,
                    dim=int(vector.shape[0]),
                    vector=pack_vector(vector),
                    text_hash=embedding_text_hash(memory, self.model_id),
                )
                for memory, vector in zip(memories, matrix, strict=True)
            ]
        )
        return len(memories)

    def load_vectors(
        self, uow: UnitOfWork, memories: Sequence[Memory], *, repair: bool = True
    ) -> tuple[np.ndarray, np.ndarray]:
        """Vectors for `memories` in order, as (matrix, has_vector mask).

        Missing or stale embeddings (content changed since embedding, or
        written by a different model) are recomputed when `repair` is
        set; the caller's UnitOfWork decides whether those repairs are
        committed."""
        n = len(memories)
        position = {m.id: i for i, m in enumerate(memories)}
        stored = uow.embeddings.get_many([m.id for m in memories], model=self.model_id)
        dim = self.embedder.dim or next((s.dim for s in stored.values()), 0)
        stale: list[Memory] = []
        vectors: dict[int, np.ndarray] = {}
        for i, memory in enumerate(memories):
            record = stored.get(memory.id)
            if record is None or record.text_hash != embedding_text_hash(memory, self.model_id):
                stale.append(memory)
                continue
            try:
                vectors[i] = unpack_vector(record.vector, record.dim)
                dim = dim or record.dim
            except ValueError:
                stale.append(memory)

        if stale and repair:
            written = self.index_many(uow, stale)
            if written:
                refreshed = uow.embeddings.get_many([m.id for m in stale], model=self.model_id)
                for memory in stale:
                    record = refreshed.get(memory.id)
                    if record is not None:
                        vectors[position[memory.id]] = unpack_vector(record.vector, record.dim)
                        dim = dim or record.dim

        dim = dim or self.embedder.dim or 1
        matrix = np.zeros((n, dim), dtype=np.float32)
        mask = np.zeros(n, dtype=bool)
        for i, vector in vectors.items():
            if vector.shape[0] == dim:
                matrix[i] = vector
                mask[i] = True
        return matrix, mask
