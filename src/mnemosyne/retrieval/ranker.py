"""
Hybrid ranking.

    relevance = s · semantic + (1 − s) · lexical            (s = semantic share)
    score     = Σ wᵢ · componentᵢ / Σ wᵢ
    components: relevance, importance, confidence, recency, scope, reinforcement

* **semantic** — cosine similarity of query and memory embeddings.
* **lexical** — BM25F, normalized to an absolute [0, 1] scale.
* **importance / confidence** — stored on the memory, adjusted by
  learning and feedback.
* **recency** — exponential decay (half-life configurable) since the
  memory was last reinforced, accessed, or edited. Memories that keep
  proving useful stay fresh; untouched inferences fade.
* **scope** — how specifically the memory applies to the working
  context (this repo > this language > global). Memories scoped to a
  *different* repository or language are excluded outright.
* **reinforcement** — log-scaled count of independent confirmations.

Every score carries its breakdown, so "why did Mnemosyne show me this?"
always has a precise answer (`explain=true` on the API).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional, Sequence

import numpy as np

from mnemosyne.domain.entities import Memory
from mnemosyne.domain.scope import Scope
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.retrieval.index import RetrievalIndex

if TYPE_CHECKING:  # pragma: no cover
    from mnemosyne.config import Settings

_REINFORCEMENT_SATURATION = math.log1p(9.0)


@dataclass(frozen=True)
class RankingWeights:
    relevance: float = 0.55
    importance: float = 0.12
    confidence: float = 0.08
    recency: float = 0.07
    scope: float = 0.13
    reinforcement: float = 0.05
    semantic_share: float = 0.5
    min_relevance: float = 0.06
    recency_half_life_days: float = 90.0
    mmr_lambda: float = 0.8

    @classmethod
    def from_settings(cls, settings: Settings) -> RankingWeights:
        return cls(
            relevance=settings.weight_relevance,
            importance=settings.weight_importance,
            confidence=settings.weight_confidence,
            recency=settings.weight_recency,
            scope=settings.weight_scope,
            reinforcement=settings.weight_reinforcement,
            semantic_share=settings.semantic_share,
            min_relevance=settings.min_relevance,
            recency_half_life_days=settings.recency_half_life_days,
            mmr_lambda=settings.mmr_lambda,
        )


@dataclass(frozen=True)
class ScoreBreakdown:
    semantic: float
    lexical: float
    relevance: float
    importance: float
    confidence: float
    recency: float
    scope: float
    reinforcement: float
    total: float

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 4) for k, v in asdict(self).items()}


@dataclass(frozen=True)
class ScoredMemory:
    memory: Memory
    score: float
    breakdown: ScoreBreakdown


class HybridRanker:
    def __init__(self, weights: Optional[RankingWeights] = None) -> None:
        self.weights = weights or RankingWeights()

    # ------------------------------------------------------------------
    def rank(
        self,
        index: RetrievalIndex,
        *,
        query: Optional[str],
        query_vector: Optional[np.ndarray],
        context: WorkingContext,
        now: datetime,
        include: Optional[Callable[[Memory], bool]] = None,
        require_relevance: bool = True,
        limit: Optional[int] = None,
    ) -> list[ScoredMemory]:
        n = index.size
        if n == 0:
            return []
        w = self.weights
        has_query = bool(query and query.strip())

        scope_values = self._scope_affinities(index, context)
        eligible = ~np.isnan(scope_values)
        if include is not None:
            eligible &= np.fromiter((include(m) for m in index.memories), dtype=bool, count=n)

        if has_query:
            lexical = index.lexical_scores(query or "")
            semantic = index.semantic_scores(query_vector)
            share = np.where(index.has_vector, w.semantic_share, 0.0).astype(np.float32)
            relevance = share * semantic + (1.0 - share) * lexical
            if require_relevance:
                eligible &= relevance >= w.min_relevance
        else:
            lexical = semantic = relevance = np.zeros(n, dtype=np.float32)

        if not eligible.any():
            return []

        importance = np.fromiter((m.importance_score for m in index.memories), dtype=np.float32, count=n)
        confidence = np.fromiter((m.confidence_score for m in index.memories), dtype=np.float32, count=n)
        recency = self._recency(index, now)
        reinforcement = np.fromiter(
            (_reinforcement_signal(m.reinforcement_count) for m in index.memories), dtype=np.float32, count=n
        )
        scope = np.nan_to_num(scope_values, nan=0.0).astype(np.float32)

        weight_relevance = w.relevance if has_query else 0.0
        total_weight = weight_relevance + w.importance + w.confidence + w.recency + w.scope + w.reinforcement
        total = (
            weight_relevance * relevance
            + w.importance * importance
            + w.confidence * confidence
            + w.recency * recency
            + w.scope * scope
            + w.reinforcement * reinforcement
        ) / (total_weight or 1.0)

        candidates = np.flatnonzero(eligible)
        order = candidates[np.argsort(-total[candidates], kind="stable")]
        if limit is not None:
            order = order[:limit]
        return [
            ScoredMemory(
                memory=index.memories[i],
                score=float(total[i]),
                breakdown=ScoreBreakdown(
                    semantic=float(semantic[i]),
                    lexical=float(lexical[i]),
                    relevance=float(relevance[i]),
                    importance=float(importance[i]),
                    confidence=float(confidence[i]),
                    recency=float(recency[i]),
                    scope=float(scope[i]),
                    reinforcement=float(reinforcement[i]),
                    total=float(total[i]),
                ),
            )
            for i in order
        ]

    # ------------------------------------------------------------------
    def diversify(self, index: RetrievalIndex, ranked: Sequence[ScoredMemory], k: int) -> list[ScoredMemory]:
        """Maximal Marginal Relevance (Carbonell & Goldstein, 1998): pick
        greedily by `λ·score − (1−λ)·max_similarity_to_already_picked`,
        so five near-identical memories don't crowd out everything else."""
        if k <= 0 or not ranked:
            return []
        lam = self.weights.mmr_lambda
        pool = list(ranked[: max(k * 4, k)])
        positions = [index.position[s.memory.id] for s in pool]
        selected: list[int] = []
        remaining = list(range(len(pool)))
        while remaining and len(selected) < k:
            best_idx, best_value = remaining[0], -math.inf
            for candidate in remaining:
                redundancy = max(
                    (index.similarity(positions[candidate], positions[chosen]) for chosen in selected), default=0.0
                )
                value = lam * pool[candidate].score - (1.0 - lam) * redundancy
                if value > best_value:
                    best_idx, best_value = candidate, value
            selected.append(best_idx)
            remaining.remove(best_idx)
        return [pool[i] for i in selected]

    # ------------------------------------------------------------------
    def _recency(self, index: RetrievalIndex, now: datetime) -> np.ndarray:
        half_life = max(self.weights.recency_half_life_days, 1e-6)
        ages = np.fromiter(
            (max(0.0, (now - m.last_signal_at).total_seconds() / 86400.0) for m in index.memories),
            dtype=np.float64,
            count=index.size,
        )
        return np.power(0.5, ages / half_life).astype(np.float32)

    @staticmethod
    def _scope_affinities(index: RetrievalIndex, context: WorkingContext) -> np.ndarray:
        cache: dict[str, float] = {}
        values = np.empty(index.size, dtype=np.float64)
        for i, memory in enumerate(index.memories):
            if memory.scope not in cache:
                affinity = context.scope_affinity(memory.scope)
                cache[memory.scope] = np.nan if affinity is None else affinity
            values[i] = cache[memory.scope]
        return values


def _reinforcement_signal(count: int) -> float:
    """0 for a single observation, saturating at 1 around ten."""
    return min(1.0, math.log1p(max(0, count - 1)) / _REINFORCEMENT_SATURATION)


# ----------------------------------------------------------------------
# Slot resolution
# ----------------------------------------------------------------------
def resolve_slots(items: Sequence[ScoredMemory]) -> tuple[list[ScoredMemory], list[ScoredMemory]]:
    """Among memories sharing a slot key, keep exactly one: the most
    specific scope (repo > language > global), then personal over team,
    then the higher confidence. "You usually use pytest, but this repo
    uses unittest" must surface as *unittest* inside that repo.

    Returns (kept, overridden), both preserving input order."""
    winners: dict[str, ScoredMemory] = {}
    for item in items:
        key = item.memory.key
        if not key:
            continue
        current = winners.get(key)
        if current is None or _slot_rank(item.memory) > _slot_rank(current.memory):
            winners[key] = item
    kept: list[ScoredMemory] = []
    overridden: list[ScoredMemory] = []
    for item in items:
        key = item.memory.key
        if key and winners[key] is not item:
            overridden.append(item)
        else:
            kept.append(item)
    return kept, overridden


def _slot_rank(memory: Memory) -> tuple[int, int, float]:
    try:
        specificity = Scope.parse(memory.scope).specificity
    except Exception:
        specificity = 0
    personal = 1 if memory.team_id is None else 0
    return (specificity, personal, memory.confidence_score)
