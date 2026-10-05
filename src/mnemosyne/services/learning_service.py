"""
LearningService: turning observations into durable memory.

`ingest()` is the single entry point every extractor uses (repository
analysis, conversation learning, the optional LLM extractor, API
clients). For each candidate it:

1. redacts secrets, validates, and applies the confidence floor;
2. looks up what it could relate to — the same slot, the same content
   fingerprint, or a near-duplicate by embedding;
3. asks the pure `Consolidator` what that means; and
4. executes the decision atomically: memory, version snapshot, evidence,
   embedding, and one audit event for the batch.

`generalize()` lifts conventions observed consistently across several
repositories the developer actually works in into language- or
global-scope memories — the step that turns "this repo does X" into
"*you* do X".
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Optional, Sequence
from uuid import UUID, uuid4

import numpy as np

from mnemosyne.core.consolidation import (
    Consolidator,
    Decision,
    NearDuplicate,
    candidate_fingerprint,
    reinforce_confidence,
)
from mnemosyne.core.memory_engine import MemoryEngine
from mnemosyne.core.redaction import redact, remaining_substance
from mnemosyne.core.text import normalize_for_fingerprint, normalize_value, short_hash, truncate
from mnemosyne.db.unit_of_work import UnitOfWork
from mnemosyne.domain.dto import NewMemoryData
from mnemosyne.domain.entities import Evidence, Memory
from mnemosyne.domain.enums import (
    EvidenceKind,
    FeedbackSignal,
    MemoryOrigin,
    MemoryStatus,
)
from mnemosyne.domain.exceptions import NotFoundError, ValidationError
from mnemosyne.domain.learning import (
    CandidateMemory,
    EvidenceDraft,
    IngestAction,
    IngestOutcome,
    IngestReport,
)
from mnemosyne.domain.principal import LOCAL_PRINCIPAL, Permission, Principal
from mnemosyne.domain.scope import Scope, ScopeKind, canonical_scope
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.learning.conversation.heuristics import ConversationExtractor
from mnemosyne.learning.conversation.transcript import Transcript
from mnemosyne.learning.repo.analyzer import AnalysisOptions, RepoAnalysis, RepositoryAnalyzer
from mnemosyne.learning.workspace import detect_working_context
from mnemosyne.repositories.sqlite.memory_repository import DYNAMICS_FIELDS
from mnemosyne.retrieval.documents import embedding_text
from mnemosyne.retrieval.indexer import MemoryIndexer
from mnemosyne.services import audit
from mnemosyne.services.retrieval_service import RetrievalService
from mnemosyne.utils.time import utcnow

logger = logging.getLogger("mnemosyne.services.learning")

UowFactory = Callable[[], UnitOfWork]

_MAX_EXCERPT = 600
#: Key prefixes that name a language ecosystem; generalized memories with
#: these keys are scoped to the language rather than globally.
_ECOSYSTEM_LANGUAGE = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "go": "go",
    "rust": "rust",
    "ruby": "ruby",
    "jvm": "java",
    "php": "php",
    "dotnet": "csharp",
}


@dataclass(frozen=True)
class LearningSettings:
    min_confidence: float = 0.55
    evidence_cap: int = 50
    generalize_min_repos: int = 2
    generalize_min_agreement: float = 0.6
    generalize_min_author_share: float = 0.2


@dataclass
class RepositoryLearning:
    analysis: RepoAnalysis
    report: Optional[IngestReport]
    generalization: Optional[IngestReport]


def wilson_lower_bound(successes: float, total: float, z: float = 1.64) -> float:
    """Lower bound of the Wilson score interval: a confidence estimate
    that is honest about small samples (3/3 is less certain than 90/100)."""
    if total <= 0:
        return 0.0
    p = min(1.0, max(0.0, successes / total))
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return max(0.0, (centre - margin) / denom)


class LearningService:
    def __init__(
        self,
        uow_factory: UowFactory,
        engine: MemoryEngine,
        indexer: MemoryIndexer,
        retrieval: RetrievalService,
        *,
        consolidator: Optional[Consolidator] = None,
        settings: Optional[LearningSettings] = None,
        default_principal: Principal = LOCAL_PRINCIPAL,
        analysis_options: Optional[AnalysisOptions] = None,
        llm_extractor_factory: Optional[Callable[[], object]] = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._engine = engine
        self._indexer = indexer
        self._retrieval = retrieval
        self.consolidator = consolidator or Consolidator()
        self.settings = settings or LearningSettings()
        self._default_principal = default_principal
        self.analysis_options = analysis_options or AnalysisOptions()
        self._llm_extractor_factory = llm_extractor_factory
        self.conversation_extractor = ConversationExtractor()

    # ------------------------------------------------------------------
    # High-level learning entry points
    # ------------------------------------------------------------------
    def learn_repository(
        self,
        path: str,
        *,
        principal: Optional[Principal] = None,
        author_email: Optional[str] = None,
        dry_run: bool = False,
        generalize: bool = True,
    ) -> RepositoryLearning:
        """Analyse a repository on this machine and learn from it."""
        options = self.analysis_options
        if author_email:
            options = replace(options, author_email=author_email)
        analysis = RepositoryAnalyzer(options).analyze(path)
        if dry_run:
            return RepositoryLearning(analysis, None, None)
        report = self.ingest(analysis.to_candidates(), principal=principal, source_label="repository")
        generalization = self.generalize(principal=principal) if generalize else None
        return RepositoryLearning(analysis, report, generalization)

    def learn_conversation(
        self,
        transcript: Transcript,
        *,
        principal: Optional[Principal] = None,
        context: Optional[WorkingContext] = None,
        use_llm: bool = False,
    ) -> IngestReport:
        """Learn from a session transcript: heuristics always, the LLM
        extractor additionally when requested and configured."""
        if context is None:
            context = detect_working_context(transcript.cwd) if transcript.cwd else WorkingContext()
        candidates = self.conversation_extractor.extract(transcript, context)
        if use_llm and self._llm_extractor_factory is not None:
            covered = [normalize_for_fingerprint(e.excerpt) for c in candidates for e in c.evidence]
            try:
                extractor = self._llm_extractor_factory()
                llm_candidates = extractor.extract(transcript, context)  # type: ignore[attr-defined]
            except Exception:
                logger.warning("llm_extraction_unavailable", exc_info=True)
                llm_candidates = []
            for candidate in llm_candidates:
                quote = normalize_for_fingerprint(candidate.evidence[0].excerpt) if candidate.evidence else ""
                # The same sentence found by both extractors is one observation,
                # not two independent confirmations.
                if quote and any(quote in seen or seen in quote for seen in covered if seen):
                    continue
                candidates.append(candidate)
        return self.ingest(candidates, principal=principal, source_label="conversation")

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------
    def ingest(
        self,
        candidates: Sequence[CandidateMemory],
        *,
        principal: Optional[Principal] = None,
        source_label: str = "api",
    ) -> IngestReport:
        who = principal or self._default_principal
        if not who.has(Permission.WRITE):
            from mnemosyne.domain.exceptions import PermissionDeniedError

            raise PermissionDeniedError("this credential lacks the 'memories:write' permission")

        report = IngestReport()
        prepared: list[CandidateMemory] = []
        for candidate in candidates:
            ready, skip_reason = self._prepare(candidate, who)
            if ready is None:
                report.add(IngestOutcome(IngestAction.SKIPPED, truncate(candidate.title, 120), skip_reason))
            else:
                prepared.append(ready)
        if not prepared:
            return report

        needs_similarity = any(not c.key for c in prepared)
        index = self._retrieval.load_index(who) if needs_similarity else None
        batch: list[tuple[Memory, Optional[np.ndarray]]] = []
        now = utcnow()
        with self._uow_factory() as uow:
            for candidate in prepared:
                try:
                    report.add(self._ingest_one(uow, candidate, who, index, batch, now))
                except ValidationError as exc:
                    report.add(IngestOutcome(IngestAction.SKIPPED, truncate(candidate.title, 120), str(exc)))
            audit.record(uow, who, "learning.ingest", target_type="memory", source=source_label, counts=report.counts)
            uow.commit()
        logger.info("learning_ingested", extra={"source": source_label, "counts": report.counts})
        return report

    def _prepare(self, candidate: CandidateMemory, who: Principal) -> tuple[Optional[CandidateMemory], str]:
        title = redact(candidate.title)
        content = redact(candidate.content)
        summary = redact(candidate.summary)
        value = redact(candidate.value or "") if candidate.value else None
        if (title.redacted or content.redacted) and remaining_substance(f"{title.text} {content.text}") < 3:
            return None, "mostly secret material; not stored"
        if not title.text.strip():
            return None, "empty title"
        if candidate.team_id is not None and not who.can_write_to(candidate.team_id):
            return None, f"not a member of team {candidate.team_id}"
        try:
            scope = canonical_scope(candidate.scope)
        except ValidationError as exc:
            return None, str(exc)
        confidence = min(1.0, max(0.0, float(candidate.confidence)))
        if confidence < self.settings.min_confidence and candidate.origin != MemoryOrigin.EXPLICIT:
            return None, f"confidence {confidence:.2f} below threshold {self.settings.min_confidence:.2f}"
        evidence = [
            replace(e, excerpt=truncate(redact(e.excerpt).text, _MAX_EXCERPT), source_ref=e.source_ref[:255])
            for e in candidate.evidence
        ]
        return (
            replace(
                candidate,
                title=truncate(title.text.strip(), 200),
                content=content.text,
                summary=summary.text,
                value=value.text if value else None,
                key=(candidate.key or "").strip().lower() or None,
                scope=scope,
                confidence=confidence,
                importance=min(1.0, max(0.0, float(candidate.importance))),
                evidence=evidence,
            ),
            "",
        )

    def _ingest_one(
        self,
        uow: UnitOfWork,
        candidate: CandidateMemory,
        who: Principal,
        index,
        batch: list[tuple[Memory, Optional[np.ndarray]]],
        now: datetime,
    ) -> IngestOutcome:
        owner = who.user_id
        slot_matches: list[Memory] = []
        fingerprint_matches: list[Memory] = []
        near: list[NearDuplicate] = []
        vector: Optional[np.ndarray] = None

        if candidate.key:
            slot_matches = uow.memories.find_slot(
                owner_id=owner, team_id=candidate.team_id, scope=candidate.scope, key=candidate.key
            )
        else:
            fingerprint_matches = uow.memories.find_by_fingerprint(
                owner_id=owner,
                team_id=candidate.team_id,
                scope=candidate.scope,
                fingerprint=candidate_fingerprint(candidate),
            )
            if not fingerprint_matches:
                vector = self._embed(candidate)
                near = self._near_duplicates(uow, candidate, vector, index, batch)

        targets = [m for m in (slot_matches[:1] + fingerprint_matches[:1] + [d.memory for d in near]) if m]
        refs = [e.source_ref for e in candidate.evidence]
        known_refs = {t.id: uow.evidence.existing_refs(t.id, refs) for t in targets} if refs else {}

        decision = self.consolidator.decide(
            candidate,
            slot_matches=slot_matches,
            fingerprint_matches=fingerprint_matches,
            near_duplicates=near,
            known_refs=known_refs,
            now=now,
        )
        return self._apply(uow, decision, candidate, owner, batch, vector, now)

    # ------------------------------------------------------------------
    def _apply(
        self,
        uow: UnitOfWork,
        decision: Decision,
        candidate: CandidateMemory,
        owner: str,
        batch: list[tuple[Memory, Optional[np.ndarray]]],
        vector: Optional[np.ndarray],
        now: datetime,
    ) -> IngestOutcome:
        action = decision.action
        target = decision.target

        if action == IngestAction.SKIPPED:
            return IngestOutcome(action, candidate.title, decision.reason, memory_id=target.id if target else None,
                                 key=candidate.key)

        if action == IngestAction.CREATED:
            memory = self._create(uow, candidate, owner, now)
            batch.append((memory, vector))
            return IngestOutcome(action, memory.title, decision.reason, memory_id=memory.id, key=memory.key,
                                 new_value=memory.value)

        assert target is not None
        if action == IngestAction.SUPERSEDED:
            candidate.metadata = {
                **candidate.metadata,
                "supersedes": str(target.id),
                "previous_value": target.value if target.value is not None else target.title,
            }
            memory = self._create(uow, candidate, owner, now)
            plan = self._engine.plan_changes(
                target,
                {"status": MemoryStatus.ARCHIVED, "superseded_by": memory.id},
                change_reason=f"superseded: {decision.reason}",
            )
            uow.memories.add_version(plan.version_snapshot)
            uow.memories.update(target.id, target.version, plan.persist_changes)
            batch.append((memory, vector))
            return IngestOutcome(
                action,
                memory.title,
                decision.reason,
                memory_id=memory.id,
                key=candidate.key,
                previous_value=target.value if target.value is not None else target.title,
                new_value=candidate.value if candidate.value is not None else candidate.title,
                replaced_memory_id=target.id,
            )

        # REINFORCED / REFRESHED / KEPT_EXISTING: update the existing memory.
        current = uow.memories.get(target.id)
        if current is None or current.status != MemoryStatus.ACTIVE:
            # Superseded earlier in this same batch; treat as new knowledge.
            memory = self._create(uow, candidate, owner, now)
            return IngestOutcome(IngestAction.CREATED, memory.title, "previous match no longer active",
                                 memory_id=memory.id, key=memory.key, new_value=memory.value)

        self._update(uow, current, decision.changes, change_reason=action.value)
        contradiction = action == IngestAction.KEPT_EXISTING
        self._attach_evidence(uow, current.id, candidate.evidence, decision.new_refs, now, contradiction=contradiction,
                              contradicting_value=candidate.value)
        return IngestOutcome(action, current.title, decision.reason, memory_id=current.id, key=current.key,
                             previous_value=current.value if contradiction else None,
                             new_value=candidate.value if contradiction else None)

    def _create(self, uow: UnitOfWork, candidate: CandidateMemory, owner: str, now: datetime) -> Memory:
        memory = self._engine.build_new_memory(
            NewMemoryData(
                title=candidate.title,
                summary=candidate.summary,
                content=candidate.content,
                memory_type=candidate.memory_type,
                confidence_score=candidate.confidence,
                importance_score=candidate.importance,
                tags=list(candidate.tags),
                source=candidate.source,
                metadata=dict(candidate.metadata),
                scope=candidate.scope,
                key=candidate.key,
                value=candidate.value,
                origin=candidate.origin,
                pinned=candidate.pinned,
                team_id=candidate.team_id,
            ),
            owner_id=owner,
        )
        uow.memories.add(memory)
        self._attach_evidence(uow, memory.id, candidate.evidence, [e.source_ref for e in candidate.evidence], now)
        self._indexer.index(uow, memory)
        return memory

    def _update(self, uow: UnitOfWork, memory: Memory, changes: dict, *, change_reason: str) -> Memory:
        if not changes:
            return memory
        dynamics = {k: v for k, v in changes.items() if k in DYNAMICS_FIELDS}
        content = {k: v for k, v in changes.items() if k not in DYNAMICS_FIELDS}
        if content:
            plan = self._engine.plan_changes(memory, content, change_reason=change_reason)
            if not plan.is_noop:
                uow.memories.add_version(plan.version_snapshot)
                memory = uow.memories.update(memory.id, memory.version, plan.persist_changes)
                if {"title", "summary", "content", "tags", "key", "value"} & set(plan.changed_fields):
                    self._indexer.index(uow, memory)
        if dynamics:
            # Validate through the engine's validators, then write without
            # creating a content version.
            validated = self._engine.plan_changes(memory, dynamics, change_reason=change_reason).changed_fields
            if validated:
                memory = uow.memories.update_dynamics(memory.id, validated)
        return memory

    def _attach_evidence(
        self,
        uow: UnitOfWork,
        memory_id: UUID,
        drafts: Sequence[EvidenceDraft],
        refs: Sequence[str],
        now: datetime,
        *,
        contradiction: bool = False,
        contradicting_value: Optional[str] = None,
    ) -> None:
        wanted = set(refs)
        added = False
        for draft in drafts:
            if draft.source_ref not in wanted:
                continue
            excerpt = draft.excerpt
            weight = draft.weight
            if contradiction:
                label = f"Contradicting observation ({contradicting_value})" if contradicting_value else (
                    "Contradicting observation"
                )
                excerpt = truncate(f"{label}: {excerpt}", _MAX_EXCERPT)
                weight = -abs(weight)
            added |= uow.evidence.add(
                Evidence(uuid4(), memory_id, draft.kind, draft.source_ref, excerpt, weight, now)
            )
        if added and uow.evidence.count_for_memory(memory_id) > self.settings.evidence_cap:
            uow.evidence.trim(memory_id, self.settings.evidence_cap)

    # ------------------------------------------------------------------
    # Similarity
    # ------------------------------------------------------------------
    def _embed(self, candidate: CandidateMemory) -> Optional[np.ndarray]:
        probe = Memory(  # a throwaway view of the candidate, for embedding text only
            id=uuid4(), title=candidate.title, summary=candidate.summary, content=candidate.content,
            memory_type=candidate.memory_type, confidence_score=candidate.confidence,
            importance_score=candidate.importance, tags=list(candidate.tags), status=MemoryStatus.ACTIVE,
            version=1, source=None, metadata={}, notes=None, created_at=utcnow(), updated_at=utcnow(),
            access_count=0, last_accessed_at=None, key=candidate.key, value=candidate.value,
        )
        try:
            return self._indexer.embedder.embed_documents([embedding_text(probe)])[0]
        except Exception:
            logger.warning("candidate_embedding_failed", exc_info=True)
            return None

    def _near_duplicates(
        self,
        uow: UnitOfWork,
        candidate: CandidateMemory,
        vector: Optional[np.ndarray],
        index,
        batch: list[tuple[Memory, Optional[np.ndarray]]],
    ) -> list[NearDuplicate]:
        if vector is None or not np.any(vector):
            return []
        threshold = self.consolidator.policy.near_duplicate_threshold
        found: dict[UUID, NearDuplicate] = {}

        def consider(memory: Memory, other: Optional[np.ndarray]) -> None:
            if other is None or memory.scope != candidate.scope or memory.team_id != candidate.team_id:
                return
            if memory.status != MemoryStatus.ACTIVE or memory.key:
                return
            similarity = float(other @ vector)
            if similarity >= threshold and (memory.id not in found or found[memory.id].similarity < similarity):
                found[memory.id] = NearDuplicate(memory, similarity)

        if index is not None and index.size:
            sims = index.vectors @ vector.astype(np.float32)
            for position in np.flatnonzero(sims >= threshold):
                if index.has_vector[position]:
                    consider(index.memories[position], index.vectors[position])
        for memory, other in batch:
            consider(memory, other)
        return sorted(found.values(), key=lambda d: d.similarity, reverse=True)[:3]

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------
    def feedback(
        self,
        memory_id: UUID,
        signal: FeedbackSignal,
        *,
        note: Optional[str] = None,
        principal: Optional[Principal] = None,
    ) -> Memory:
        """Learn from the developer's reaction to a served memory."""
        who = principal or self._default_principal
        if not who.has(Permission.WRITE):
            from mnemosyne.domain.exceptions import PermissionDeniedError

            raise PermissionDeniedError("this credential lacks the 'memories:write' permission")
        now = utcnow()
        with self._uow_factory() as uow:
            memory = uow.memories.get(memory_id)
            if memory is None or not who.can_see(memory) or not who.can_write_to(memory.team_id):
                raise NotFoundError(f"Memory {memory_id} not found")

            changes: dict = {}
            if signal == FeedbackSignal.HELPFUL:
                changes = {
                    "importance_score": min(1.0, memory.importance_score + 0.05),
                    "confidence_score": reinforce_confidence(memory.confidence_score, 0.1, 0.99),
                    "reinforcement_count": memory.reinforcement_count + 1,
                    "last_reinforced_at": now,
                }
            elif signal == FeedbackSignal.NOT_HELPFUL:
                changes = {"importance_score": max(0.05, memory.importance_score - 0.1)}
            elif signal == FeedbackSignal.CONFIRM:
                changes = {
                    "origin": MemoryOrigin.EXPLICIT,
                    "confidence_score": max(memory.confidence_score, 0.95),
                    "last_reinforced_at": now,
                }
            elif signal in (FeedbackSignal.WRONG, FeedbackSignal.OUTDATED):
                changes = {"status": MemoryStatus.ARCHIVED}

            updated = self._update(uow, memory, changes, change_reason=f"feedback:{signal.value}")
            ref = f"feedback:{signal.value}:{now.isoformat()}"
            uow.evidence.add(
                Evidence(uuid4(), memory_id, EvidenceKind.FEEDBACK, ref, truncate(redact(note or "").text, 400),
                         -1.0 if signal in (FeedbackSignal.WRONG, FeedbackSignal.OUTDATED,
                                            FeedbackSignal.NOT_HELPFUL) else 1.0, now)
            )
            audit.record(uow, who, f"memory.feedback.{signal.value}", target_type="memory", target_id=str(memory_id))
            uow.commit()
        return updated

    # ------------------------------------------------------------------
    # Generalization
    # ------------------------------------------------------------------
    def generalize(self, *, principal: Optional[Principal] = None) -> IngestReport:
        """Lift repository-level conventions that hold across the
        developer's own repositories to language or global scope."""
        who = principal or self._default_principal
        with self._uow_factory() as uow:
            memories = uow.memories.list_for_index(who.visibility)
            uow.commit()

        by_key: dict[str, dict[str, Memory]] = defaultdict(dict)
        for memory in memories:
            if (
                memory.key
                and memory.team_id is None
                and memory.origin == MemoryOrigin.INFERRED
                and memory.scope.startswith("repo:")
                and not memory.key.startswith("repo.")
                and float(memory.metadata.get("author_share", 1.0)) >= self.settings.generalize_min_author_share
            ):
                by_key[memory.key][memory.scope] = memory

        candidates: list[CandidateMemory] = []
        for key, per_repo in sorted(by_key.items()):
            votes: dict[str, list[Memory]] = defaultdict(list)
            for memory in per_repo.values():
                votes[normalize_value(memory.value)].append(memory)
            winner_value, supporters = max(votes.items(), key=lambda item: (len(item[1]), item[0]))
            total = len(per_repo)
            if len(supporters) < self.settings.generalize_min_repos:
                continue
            if len(supporters) / total < self.settings.generalize_min_agreement:
                continue
            exemplar = max(supporters, key=lambda m: m.confidence_score)
            repos = sorted(m.scope.removeprefix("repo:") for m in supporters)
            ecosystem = key.split(".", 1)[0]
            scope = str(Scope.language(_ECOSYSTEM_LANGUAGE[ecosystem])) if ecosystem in _ECOSYSTEM_LANGUAGE else (
                str(Scope.global_())
            )
            confidence = round(
                min(0.95, wilson_lower_bound(len(supporters), total) * 0.5
                    + 0.5 * sum(m.confidence_score for m in supporters) / len(supporters)),
                3,
            )
            candidates.append(
                CandidateMemory(
                    title=_generalized_title(exemplar.title),
                    content=f"{exemplar.content}\n\nObserved consistently in {len(supporters)} of {total} of your "
                            f"repositories: {', '.join(repos)}.",
                    memory_type=exemplar.memory_type,
                    origin=MemoryOrigin.SYSTEM,
                    confidence=confidence,
                    importance=exemplar.importance_score,
                    scope=scope,
                    key=key,
                    value=exemplar.value,
                    tags=list(exemplar.tags),
                    source="generalizer",
                    metadata={"generalized_from": repos, "agreement": round(len(supporters) / total, 3)},
                    evidence=[
                        EvidenceDraft(
                            EvidenceKind.GENERALIZATION,
                            f"generalize:{key}:{winner_value}:{short_hash('|'.join(repos))}",
                            f"{len(supporters)}/{total} repositories agree: {', '.join(repos)}",
                        )
                    ],
                )
            )
        if not candidates:
            return IngestReport()
        return self.ingest(candidates, principal=who, source_label="generalizer")


def _generalized_title(title: str) -> str:
    """Repository-level titles read "In this repository: ..."; strip that."""
    for prefix in ("In this repository: ", "This repository ", "Repo: "):
        if title.startswith(prefix):
            return title[len(prefix):][:1].upper() + title[len(prefix):][1:]
    return title


def scope_kind(scope: str) -> ScopeKind:
    return Scope.parse(scope).kind
