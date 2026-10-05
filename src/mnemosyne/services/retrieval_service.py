"""
RetrievalService: "which of my memories matter for this?"

Loads (or reuses) the caller's RetrievalIndex snapshot, embeds the
query, ranks with HybridRanker, resolves slot conflicts, and optionally
diversifies with MMR. Read-only apart from two side effects that are
safe to retry: repairing missing embeddings, and (when asked) recording
that memories were served.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from uuid import UUID

import numpy as np

from mnemosyne.db.unit_of_work import UnitOfWork
from mnemosyne.domain.entities import Memory
from mnemosyne.domain.enums import MemoryOrigin, MemoryType
from mnemosyne.domain.principal import LOCAL_PRINCIPAL, Principal
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.retrieval.index import DiskIndexCache, IndexCache, RetrievalIndex
from mnemosyne.retrieval.indexer import MemoryIndexer
from mnemosyne.retrieval.ranker import HybridRanker, ScoredMemory, resolve_slots
from mnemosyne.utils.time import utcnow

logger = logging.getLogger("mnemosyne.services.retrieval")

UowFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class RecallRequest:
    query: Optional[str] = None
    context: WorkingContext = field(default_factory=WorkingContext)
    limit: int = 10
    memory_types: Optional[list[MemoryType]] = None
    scopes: Optional[list[str]] = None
    origins: Optional[list[MemoryOrigin]] = None
    min_confidence: Optional[float] = None
    diversify: bool = True
    resolve_slots: bool = True
    #: Drop memories whose relevance to the query is below the threshold.
    require_relevance: bool = True
    record_access: bool = False


@dataclass
class RecallResult:
    items: list[ScoredMemory]
    overridden: list[ScoredMemory]
    candidates: int
    index_size: int
    took_ms: float
    semantic_available: bool


class RetrievalService:
    def __init__(
        self,
        uow_factory: UowFactory,
        indexer: MemoryIndexer,
        ranker: HybridRanker,
        cache: Optional[IndexCache] = None,
        *,
        default_principal: Principal = LOCAL_PRINCIPAL,
        disk_cache: Optional[DiskIndexCache] = None,
    ) -> None:
        self._uow_factory = uow_factory
        self.indexer = indexer
        self.ranker = ranker
        self.cache = cache or IndexCache()
        self.disk_cache = disk_cache
        self._default_principal = default_principal

    # ------------------------------------------------------------------
    def load_index(self, principal: Optional[Principal] = None) -> RetrievalIndex:
        principal = principal or self._default_principal
        visibility = principal.visibility
        key = f"{visibility.cache_key}|{self.indexer.model_id}"
        with self._uow_factory() as uow:
            signature = uow.memories.index_signature(visibility)
            cached = self.cache.get(key, signature)
            if cached is None and self.disk_cache is not None:
                cached = self.disk_cache.get(key, signature)
                if cached is not None:
                    self.cache.put(key, signature, cached)
            if cached is not None:
                uow.commit()
                return cached
            started = time.perf_counter()
            memories = uow.memories.list_for_index(visibility)
            vectors, mask = self.indexer.load_vectors(uow, memories, repair=True)
            uow.commit()
        index = RetrievalIndex.build(
            model_id=self.indexer.model_id, memories=memories, vectors=vectors, has_vector=mask
        )
        self.cache.put(key, signature, index)
        if self.disk_cache is not None:
            self.disk_cache.put(key, signature, index)
        logger.info(
            "retrieval_index_built",
            extra={
                "size": index.size,
                "with_vectors": int(mask.sum()),
                "ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return index

    def embed_query(self, query: Optional[str]) -> Optional[np.ndarray]:
        if not query or not query.strip():
            return None
        try:
            return self.indexer.embedder.embed_query(query)
        except Exception:
            logger.warning("query_embedding_failed_degrading_to_lexical", exc_info=True)
            return None

    # ------------------------------------------------------------------
    def recall(self, request: RecallRequest, principal: Optional[Principal] = None) -> RecallResult:
        principal = principal or self._default_principal
        started = time.perf_counter()
        index = self.load_index(principal)
        query_vector = self.embed_query(request.query)

        allowed_types = set(request.memory_types) if request.memory_types else None
        allowed_scopes = set(request.scopes) if request.scopes else None
        allowed_origins = set(request.origins) if request.origins else None

        def include(memory: Memory) -> bool:
            if allowed_types is not None and memory.memory_type not in allowed_types:
                return False
            if allowed_scopes is not None and memory.scope not in allowed_scopes:
                return False
            if allowed_origins is not None and memory.origin not in allowed_origins:
                return False
            return request.min_confidence is None or memory.confidence_score >= request.min_confidence

        ranked = self.ranker.rank(
            index,
            query=request.query,
            query_vector=query_vector,
            context=request.context,
            now=utcnow(),
            include=include,
            require_relevance=request.require_relevance,
        )
        overridden: list[ScoredMemory] = []
        if request.resolve_slots:
            ranked, overridden = resolve_slots(ranked)
        items = (
            self.ranker.diversify(index, ranked, request.limit) if request.diversify else ranked[: request.limit]
        )
        if request.record_access and items:
            self.record_access([s.memory.id for s in items])

        return RecallResult(
            items=items,
            overridden=overridden,
            candidates=len(ranked),
            index_size=index.size,
            took_ms=round((time.perf_counter() - started) * 1000, 2),
            semantic_available=query_vector is not None and bool(index.has_vector.any()),
        )

    def record_access(self, memory_ids: list[UUID]) -> None:
        with self._uow_factory() as uow:
            uow.memories.record_access_many(memory_ids)
            uow.commit()
