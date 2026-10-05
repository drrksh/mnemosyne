"""
ContextService: compiling "what the assistant should know right now".

Two layers, packed into a token budget:

1. **Task-relevant** memories — ranked by the hybrid retriever against
   the task description and diversified with MMR. Rendered with detail.
2. **Profile** — standing knowledge that applies to *any* task in this
   working context: rules, communication preferences, codified
   conventions and tooling for the active languages and repository, how
   to run things, the developer's expertise. Rendered as one-liners.

Slot resolution runs across both layers, so a repository's own
convention replaces the developer's general habit for the same slot
("you usually use pytest; this repo uses unittest" renders as unittest).
Memories scoped to a different repository or language never appear.

The pack is budgeted in estimated tokens and, for editor hooks, also
hard-capped in characters (Claude Code truncates hook context beyond
10,000 characters).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from mnemosyne.context.render import (
    ContextItem,
    RenderFormat,
    RenderPlan,
    estimate_tokens,
    render,
    section_for,
)
from mnemosyne.domain.entities import Memory
from mnemosyne.domain.enums import MemoryType
from mnemosyne.domain.principal import LOCAL_PRINCIPAL, Principal
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.retrieval.index import RetrievalIndex
from mnemosyne.retrieval.ranker import ScoredMemory, resolve_slots
from mnemosyne.services.retrieval_service import RetrievalService
from mnemosyne.utils.time import utcnow

logger = logging.getLogger("mnemosyne.services.context")

_LINE_OVERHEAD_TOKENS = 2


@dataclass(frozen=True)
class ContextRequest:
    task: Optional[str] = None
    context: WorkingContext = field(default_factory=WorkingContext)
    max_tokens: int = 1500
    #: Hard cap on rendered characters (hooks); None = tokens only.
    max_chars: Optional[int] = None
    format: RenderFormat = "markdown"
    #: Wrap in <mnemosyne-context> (for text injected into a conversation).
    wrap: bool = False
    include_profile: bool = True
    min_confidence: float = 0.5
    relevant_limit: int = 12
    #: Relevant items must reach this fraction of the best match's
    #: relevance. Injected context competes for the model's attention, so
    #: the weak tail of a ranking costs more than it adds.
    relative_relevance: float = 0.4
    #: Relevance at which a memory is injected regardless of importance.
    strong_relevance: float = 0.25
    record_access: bool = True
    explain: bool = False


@dataclass
class ContextPack:
    text: str
    items: list[ContextItem]
    estimated_tokens: int
    #: Candidates that applied but did not fit the budget.
    omitted: int
    #: Memories hidden because a more specific memory owns the same slot.
    overridden: int
    context: WorkingContext
    took_ms: float

    @property
    def is_empty(self) -> bool:
        return not self.items


def is_profile_memory(memory: Memory) -> bool:
    """Standing knowledge worth stating for any task in its scope."""
    if memory.pinned:
        return True
    kind = memory.memory_type
    if kind in (MemoryType.RULE, MemoryType.ANTI_PATTERN):
        return memory.importance_score >= 0.4
    if kind == MemoryType.PREFERENCE:
        return bool(memory.key and memory.key.startswith("communication.")) or memory.importance_score >= 0.6
    if kind == MemoryType.SKILL:
        return True
    if kind in (MemoryType.CONVENTION, MemoryType.PATTERN, MemoryType.TOOLING):
        return bool(memory.key) or memory.importance_score >= 0.6
    if kind == MemoryType.PROCEDURE:
        return memory.key == "repo.commands" or memory.importance_score >= 0.7
    if kind in (MemoryType.ARCHITECTURE, MemoryType.FACT):
        return memory.scope.startswith("repo:") and memory.importance_score >= 0.55
    if kind == MemoryType.DECISION:
        return memory.scope.startswith("repo:") and memory.importance_score >= 0.75
    return False


class ContextService:
    def __init__(
        self,
        retrieval: RetrievalService,
        *,
        profile_share: float = 0.45,
        default_principal: Principal = LOCAL_PRINCIPAL,
    ) -> None:
        self._retrieval = retrieval
        self.profile_share = profile_share
        self._default_principal = default_principal

    # ------------------------------------------------------------------
    def enrich_context(self, index: RetrievalIndex, context: WorkingContext) -> WorkingContext:
        """Fill in languages from what Mnemosyne already knows about the
        repository when the caller could not detect them."""
        if context.repo_id:
            scope = f"repo:{context.repo_id}"
            for memory in index.memories:
                if memory.scope == scope and memory.key == "repo.overview":
                    languages = memory.metadata.get("languages") or []
                    if languages:
                        context = context.with_languages(list(languages))
                    break
        if "typescript" in context.languages and "javascript" not in context.languages:
            context = context.with_languages(["javascript"])
        return context

    def compile(self, request: ContextRequest, principal: Optional[Principal] = None) -> ContextPack:
        who = principal or self._default_principal
        started = time.perf_counter()
        index = self._retrieval.load_index(who)
        context = self.enrich_context(index, request.context)
        ranker = self._retrieval.ranker
        now = utcnow()

        def confident(memory: Memory) -> bool:
            return memory.confidence_score >= request.min_confidence

        relevant: list[ScoredMemory] = []
        if request.task and request.task.strip():
            relevant = ranker.rank(
                index,
                query=request.task,
                query_vector=self._retrieval.embed_query(request.task),
                context=context,
                now=now,
                include=confident,
                require_relevance=True,
                limit=request.relevant_limit * 4,
            )
            if relevant:
                floor = request.relative_relevance * max(s.breakdown.relevance for s in relevant)
                relevant = [
                    s for s in relevant
                    if s.breakdown.relevance >= floor
                    # A weak match must also matter: low-importance trivia that
                    # only shares a broad concept with the task is not injected.
                    and (s.breakdown.relevance >= request.strong_relevance or s.memory.importance_score >= 0.5)
                ]

        profile: list[ScoredMemory] = []
        if request.include_profile:
            no_repo = context.repo_id is None

            def profile_filter(memory: Memory) -> bool:
                if no_repo and memory.scope.startswith("repo:"):
                    return False  # without a repository, other projects' knowledge is noise
                return confident(memory) and is_profile_memory(memory)

            profile = ranker.rank(index, query=None, query_vector=None, context=context, now=now,
                                  include=profile_filter, require_relevance=False)
            profile.sort(key=lambda s: not s.memory.pinned)  # stable: keeps the ranker's deterministic order

        relevant_ids = {s.memory.id for s in relevant}
        combined = relevant + [s for s in profile if s.memory.id not in relevant_ids]
        kept, overridden = resolve_slots(combined)
        kept_ids = {s.memory.id for s in kept}
        relevant = ranker.diversify(index, [s for s in relevant if s.memory.id in kept_ids], request.relevant_limit)
        relevant_ids = {s.memory.id for s in relevant}
        profile = [s for s in profile if s.memory.id in kept_ids and s.memory.id not in relevant_ids]

        plan = RenderPlan(context=context, task=request.task)
        omitted = self._pack(plan, relevant, profile, request)
        text = render(plan, request.format, wrap=request.wrap)
        if request.max_chars is not None:
            while plan.items and len(text) > request.max_chars:
                plan.items.pop()  # lowest-priority item last
                omitted += 1
                text = render(plan, request.format, wrap=request.wrap)

        if request.record_access and plan.items:
            self._retrieval.record_access([item.memory.id for item in plan.items])
        took = round((time.perf_counter() - started) * 1000, 2)
        logger.info("context_compiled", extra={"items": len(plan.items), "omitted": omitted, "ms": took})
        return ContextPack(
            text=text,
            items=plan.items,
            estimated_tokens=estimate_tokens(text),
            omitted=omitted,
            overridden=len(overridden),
            context=context,
            took_ms=took,
        )

    def profile(
        self,
        context: Optional[WorkingContext] = None,
        *,
        principal: Optional[Principal] = None,
        max_tokens: int = 4000,
        fmt: RenderFormat = "markdown",
    ) -> ContextPack:
        """The developer's standing profile for a working context — what
        rule files (CLAUDE.md, AGENTS.md...) and `mnemosyne profile` show."""
        return self.compile(
            ContextRequest(task=None, context=context or WorkingContext(), max_tokens=max_tokens, format=fmt,
                           record_access=False),
            principal,
        )

    # ------------------------------------------------------------------
    def _pack(
        self,
        plan: RenderPlan,
        relevant: list[ScoredMemory],
        profile: list[ScoredMemory],
        request: ContextRequest,
    ) -> int:
        """Greedy packing by priority. Relevant items get first claim on
        their share of the budget; whatever either layer leaves unused
        flows to the other."""
        budget = request.max_tokens
        used = estimate_tokens(render(RenderPlan(plan.context, plan.task), "markdown", wrap=request.wrap)) + 10
        relevant_cap = budget if not profile else max(budget * (1.0 - self.profile_share), budget * 0.3)

        def item(scored: ScoredMemory, reason: str, detail: bool) -> ContextItem:
            return ContextItem(
                memory=scored.memory,
                section=section_for(scored.memory),
                reason="pinned" if scored.memory.pinned and reason == "profile" else reason,
                score=scored.score,
                detail=detail,
                breakdown=scored.breakdown.as_dict() if request.explain else None,
            )

        def try_add(candidate: ContextItem, limit: float) -> bool:
            nonlocal used
            cost = estimate_tokens(candidate.line()) + _LINE_OVERHEAD_TOKENS
            if used + cost > limit:
                return False
            plan.items.append(candidate)
            used += cost
            return True

        pending_relevant = [item(s, "relevant", detail=True) for s in relevant]
        pending_profile = [item(s, "profile", detail=s.memory.pinned) for s in profile]
        leftovers: list[ContextItem] = []
        for candidate in pending_relevant:
            if not try_add(candidate, relevant_cap):
                leftovers.append(candidate)
        omitted = 0
        for candidate in pending_profile:
            if not try_add(candidate, budget):
                omitted += 1
        for candidate in leftovers:
            if not try_add(candidate, budget):
                omitted += 1
        return omitted
