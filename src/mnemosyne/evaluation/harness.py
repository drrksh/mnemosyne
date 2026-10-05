"""
Retrieval evaluation harness.

Seeds a labeled corpus into an isolated in-memory Mnemosyne, runs every
query through the real RetrievalService, and reports standard IR
metrics:

* **Recall@k** — share of the relevant (grade ≥ 2) memories found in the
  top k, with the denominator capped at k so a query with more relevant
  memories than slots is not penalized for the cap.
* **MRR** — reciprocal rank of the first grade-3 memory.
* **nDCG@k** — graded gain (2^grade − 1) with logarithmic discount,
  normalized by the ideal ordering.
* **Leakage** — share of queries whose top k contains a memory scoped to
  a *different* repository or a label the query marks as forbidden.
  For a developer memory this is a correctness property, not a quality
  nuance: suggesting another repo's conventions is a wrong answer.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, replace
from typing import Optional, Sequence
from uuid import UUID

from mnemosyne.config import Settings
from mnemosyne.domain.dto import NewMemoryData
from mnemosyne.domain.enums import MemoryOrigin
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.evaluation.persona import MEMORIES, QUERIES, PersonaMemory, PersonaQuery
from mnemosyne.services.retrieval_service import RecallRequest


@dataclass(frozen=True)
class QueryOutcome:
    query: PersonaQuery
    ranked_labels: list[str]
    recall: float
    reciprocal_rank: float
    ndcg: float
    leaked: list[str]


@dataclass
class EvaluationReport:
    variant: str
    k: int
    outcomes: list[QueryOutcome] = field(default_factory=list)
    p50_ms: float = 0.0
    p95_ms: float = 0.0

    @property
    def recall_at_k(self) -> float:
        return statistics.fmean(o.recall for o in self.outcomes)

    @property
    def mrr(self) -> float:
        return statistics.fmean(o.reciprocal_rank for o in self.outcomes)

    @property
    def ndcg_at_k(self) -> float:
        return statistics.fmean(o.ndcg for o in self.outcomes)

    @property
    def leakage_rate(self) -> float:
        return sum(1 for o in self.outcomes if o.leaked) / max(1, len(self.outcomes))

    def summary(self) -> dict[str, float]:
        return {
            f"recall@{self.k}": round(self.recall_at_k, 3),
            "mrr": round(self.mrr, 3),
            f"ndcg@{self.k}": round(self.ndcg_at_k, 3),
            "leakage": round(self.leakage_rate, 3),
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
        }

    def failures(self, threshold: float = 0.5) -> list[QueryOutcome]:
        return [o for o in self.outcomes if o.ndcg < threshold or o.leaked]


# ----------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------
def recall_at_k(ranked: Sequence[str], relevant: dict[str, int], k: int) -> float:
    wanted = {label for label, grade in relevant.items() if grade >= 2}
    if not wanted:
        return 1.0
    hits = len(wanted & set(ranked[:k]))
    return hits / min(k, len(wanted))


def reciprocal_rank(ranked: Sequence[str], relevant: dict[str, int]) -> float:
    essential = {label for label, grade in relevant.items() if grade >= 3} or set(relevant)
    for position, label in enumerate(ranked, start=1):
        if label in essential:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: dict[str, int], k: int) -> float:
    def dcg(grades: Sequence[int]) -> float:
        return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades[:k]))

    actual = dcg([relevant.get(label, 0) for label in ranked])
    ideal = dcg(sorted(relevant.values(), reverse=True))
    return actual / ideal if ideal > 0 else 1.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(pct / 100 * len(ordered)) - 1))
    return ordered[index]


# ----------------------------------------------------------------------
# Seeding & running
# ----------------------------------------------------------------------
def to_new_memory(item: PersonaMemory) -> NewMemoryData:
    return NewMemoryData(
        title=item.title,
        content=item.content,
        memory_type=item.memory_type,
        scope=item.scope,
        key=item.key,
        value=item.value,
        importance_score=item.importance,
        confidence_score=item.confidence,
        tags=list(item.tags),
        origin=MemoryOrigin.EXPLICIT,
        source="persona",
        metadata={"persona_label": item.label},
    )


def seed(container, memories: Sequence[PersonaMemory] = MEMORIES) -> dict[UUID, str]:
    created = container.memories.create_memories_batch([to_new_memory(m) for m in memories])
    return {memory.id: item.label for memory, item in zip(created, memories, strict=True)}


VARIANTS: dict[str, dict[str, float]] = {
    "hybrid": {},
    "lexical-only": {"semantic_share": 0.0},
    "semantic-only": {"semantic_share": 1.0},
    "relevance-only": {
        "weight_importance": 0.0,
        "weight_confidence": 0.0,
        "weight_recency": 0.0,
        "weight_scope": 0.0,
        "weight_reinforcement": 0.0,
    },
}


def evaluate(
    variant: str = "hybrid",
    *,
    k: int = 5,
    settings: Optional[Settings] = None,
    memories: Sequence[PersonaMemory] = MEMORIES,
    queries: Sequence[PersonaQuery] = QUERIES,
) -> EvaluationReport:
    from mnemosyne.container import Mnemosyne

    base = settings or Settings(database_url="sqlite:///:memory:", app_env="testing", log_level="WARNING")
    tuned = replace_settings(base, **VARIANTS.get(variant, {}))
    report = EvaluationReport(variant=variant, k=k)
    latencies: list[float] = []
    label_scope = {m.label: m.scope for m in memories}
    with Mnemosyne(tuned, schema="create") as container:
        labels = seed(container, memories)
        container.retrieval.load_index()  # warm the cache: measure queries, not the first build
        for query in queries:
            context = WorkingContext.create(repo_id=query.repo, languages=list(query.languages))
            result = container.retrieval.recall(RecallRequest(query=query.text, context=context, limit=10))
            latencies.append(result.took_ms)
            ranked = [labels[s.memory.id] for s in result.items]
            # Leakage = a forbidden label, or (when the query has a repo) a
            # memory scoped to a *different* repo. Without a repo context,
            # repo-scoped results are precision misses (nDCG), not leakage.
            leaked = [
                label
                for label in ranked[:k]
                if label in query.forbidden
                or (
                    query.repo is not None
                    and label_scope[label].startswith("repo:")
                    and label_scope[label] != f"repo:{query.repo}"
                )
            ]
            report.outcomes.append(
                QueryOutcome(
                    query=query,
                    ranked_labels=ranked,
                    recall=recall_at_k(ranked, query.relevant, k),
                    reciprocal_rank=reciprocal_rank(ranked, query.relevant),
                    ndcg=ndcg_at_k(ranked, query.relevant, k),
                    leaked=leaked,
                )
            )
    report.p50_ms = _percentile(latencies, 50)
    report.p95_ms = _percentile(latencies, 95)
    return report


def replace_settings(settings: Settings, **overrides: float) -> Settings:
    if not overrides:
        return settings
    return settings.model_copy(update=overrides)


def format_report(reports: Sequence[EvaluationReport]) -> str:
    if not reports:
        return ""
    keys = list(reports[0].summary())
    header = f"{'variant':<16}" + "".join(f"{key:>12}" for key in keys)
    rows = [header, "-" * len(header)]
    for report in reports:
        summary = report.summary()
        rows.append(f"{report.variant:<16}" + "".join(f"{summary[key]:>12}" for key in keys))
    return "\n".join(rows)


__all__ = ["VARIANTS", "EvaluationReport", "evaluate", "format_report", "replace"]


# ----------------------------------------------------------------------
# End-to-end: what actually reaches the model
# ----------------------------------------------------------------------
@dataclass
class ContextEvaluation:
    max_tokens: int
    coverage_essential: float = 0.0  # grade-3 labels present in the compiled context
    coverage_relevant: float = 0.0  # grade >= 2 labels present
    noise_rate: float = 0.0  # share of included memories that are unlabeled haystack
    leakage: float = 0.0  # share of queries including another repository's memories
    avg_items: float = 0.0
    avg_tokens: float = 0.0
    p95_ms: float = 0.0

    def summary(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in self.__dict__.items()}


def evaluate_context(
    *,
    max_tokens: int = 1500,
    settings: Optional[Settings] = None,
    memories: Sequence[PersonaMemory] = MEMORIES,
    queries: Sequence[PersonaQuery] = QUERIES,
) -> ContextEvaluation:
    """Measure the compiled context (profile + task-relevant layers), not
    just the ranked list: this is what the assistant actually receives."""
    from mnemosyne.container import Mnemosyne
    from mnemosyne.services.context_service import ContextRequest

    base = settings or Settings(database_url="sqlite:///:memory:", app_env="testing", log_level="WARNING")
    label_scope = {m.label: m.scope for m in memories}
    essential, relevant, noise, items, tokens, latencies = [], [], [], [], [], []
    leaks = 0
    with Mnemosyne(base, schema="create") as container:
        labels = seed(container, memories)
        container.retrieval.load_index()
        for query in queries:
            context = WorkingContext.create(repo_id=query.repo, languages=list(query.languages))
            pack = container.context.compile(
                ContextRequest(task=query.text, context=context, max_tokens=max_tokens, record_access=False)
            )
            included = {labels[item.memory.id] for item in pack.items}
            must = {label for label, grade in query.relevant.items() if grade >= 3}
            want = {label for label, grade in query.relevant.items() if grade >= 2}
            essential.append(len(must & included) / len(must) if must else 1.0)
            relevant.append(len(want & included) / len(want) if want else 1.0)
            haystack = {label for label in included if label.startswith("n_") and label not in query.relevant}
            noise.append(len(haystack) / max(1, len(included)))
            if query.repo and any(
                label_scope[label].startswith("repo:") and label_scope[label] != f"repo:{query.repo}"
                for label in included
            ):
                leaks += 1
            items.append(len(pack.items))
            tokens.append(pack.estimated_tokens)
            latencies.append(pack.took_ms)
    return ContextEvaluation(
        max_tokens=max_tokens,
        coverage_essential=statistics.fmean(essential),
        coverage_relevant=statistics.fmean(relevant),
        noise_rate=statistics.fmean(noise),
        leakage=leaks / max(1, len(queries)),
        avg_items=statistics.fmean(items),
        avg_tokens=statistics.fmean(tokens),
        p95_ms=_percentile(latencies, 95),
    )
