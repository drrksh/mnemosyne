"""
Learning endpoints — the write side of developer memory.

Integrations push observations here: structured candidates (an IDE
plugin, a code-review bot), raw conversations, or — in local mode only —
a repository path to analyse on the server's own disk. Every path goes
through the same ingestion pipeline: redaction, consolidation, evidence.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from mnemosyne.api.dependencies import get_container, get_principal
from mnemosyne.api.routers.context import WorkingContextModel
from mnemosyne.api.schemas import MemoryResponse
from mnemosyne.container import Mnemosyne
from mnemosyne.domain.enums import EvidenceKind, FeedbackSignal, MemoryOrigin, MemoryType
from mnemosyne.domain.exceptions import PermissionDeniedError
from mnemosyne.domain.learning import CandidateMemory, EvidenceDraft, IngestReport
from mnemosyne.domain.principal import Principal
from mnemosyne.learning.conversation.transcript import parse_messages, parse_transcript_text

router = APIRouter(prefix="/api/v1", tags=["learning"])


class EvidenceModel(BaseModel):
    kind: EvidenceKind = EvidenceKind.EXPLICIT
    source_ref: str = Field(..., min_length=1, max_length=255)
    excerpt: str = Field(default="", max_length=4000)
    weight: float = Field(default=1.0, ge=-1.0, le=1.0)


class CandidateModel(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(default="", max_length=50_000)
    summary: str = Field(default="", max_length=2000)
    memory_type: MemoryType = MemoryType.PREFERENCE
    origin: Literal["explicit", "conversation", "inferred", "imported"] = "conversation"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    scope: str = "global"
    key: Optional[str] = None
    value: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    evidence: list[EvidenceModel] = Field(default_factory=list, max_length=20)
    team_id: Optional[str] = None
    pinned: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_domain(self, source: str) -> CandidateMemory:
        return CandidateMemory(
            title=self.title, content=self.content, summary=self.summary, memory_type=self.memory_type,
            origin=MemoryOrigin(self.origin), confidence=self.confidence, importance=self.importance,
            scope=self.scope, key=self.key, value=self.value, tags=self.tags, team_id=self.team_id,
            pinned=self.pinned, metadata=self.metadata, source=source,
            evidence=[EvidenceDraft(e.kind, e.source_ref, e.excerpt, e.weight) for e in self.evidence],
        )


class OutcomeModel(BaseModel):
    action: str
    title: str
    reason: str
    memory_id: Optional[UUID] = None
    key: Optional[str] = None
    previous_value: Optional[str] = None
    new_value: Optional[str] = None
    replaced_memory_id: Optional[UUID] = None


class IngestResponse(BaseModel):
    summary: str
    counts: dict[str, int]
    outcomes: list[OutcomeModel]

    @classmethod
    def from_report(cls, report: IngestReport) -> IngestResponse:
        return cls(
            summary=report.summary_line(),
            counts=report.counts,
            outcomes=[OutcomeModel(action=o.action.value, title=o.title, reason=o.reason, memory_id=o.memory_id,
                                   key=o.key, previous_value=o.previous_value, new_value=o.new_value,
                                   replaced_memory_id=o.replaced_memory_id) for o in report.outcomes],
        )


class CandidatesRequest(BaseModel):
    candidates: list[CandidateModel] = Field(..., min_length=1, max_length=500)
    source: str = Field(default="api", max_length=100)


@router.post("/learn/candidates", response_model=IngestResponse)
def learn_candidates(
    payload: CandidatesRequest,
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> IngestResponse:
    """Ingest structured observations produced by any integration."""
    report = container.learning.ingest(
        [c.to_domain(f"api:{payload.source}") for c in payload.candidates],
        principal=principal,
        source_label=payload.source,
    )
    return IngestResponse.from_report(report)


class MessageModel(BaseModel):
    role: Literal["user", "assistant", "human", "ai"]
    content: Any


class ConversationRequest(BaseModel):
    transcript: Optional[str] = Field(default=None, max_length=5_000_000,
                                      description="Claude Code JSONL, chat JSON, or 'User:'/'Assistant:' text.")
    messages: Optional[list[MessageModel]] = Field(default=None, max_length=5000)
    session_id: Optional[str] = Field(default=None, max_length=200)
    context: WorkingContextModel = Field(default_factory=WorkingContextModel)
    use_llm: bool = False

    @model_validator(mode="after")
    def _one_source(self) -> ConversationRequest:
        if (self.transcript is None) == (self.messages is None):
            raise ValueError("provide exactly one of 'transcript' or 'messages'")
        return self


@router.post("/learn/conversation", response_model=IngestResponse)
def learn_conversation(
    payload: ConversationRequest,
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> IngestResponse:
    if payload.transcript is not None:
        transcript = parse_transcript_text(payload.transcript, session_id=payload.session_id)
    else:
        transcript = parse_messages([m.model_dump() for m in payload.messages or []], session_id=payload.session_id)
    transcript.session_id = payload.session_id or transcript.session_id
    context = payload.context.resolve(container)
    report = container.learning.learn_conversation(
        transcript, principal=principal, context=context,
        use_llm=payload.use_llm and container.settings.llm_extraction,
    )
    return IngestResponse.from_report(report)


class RepositoryRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=4096)
    author_email: Optional[str] = None
    dry_run: bool = False
    generalize: bool = True


class RepositoryResponse(IngestResponse):
    repo_id: str
    languages: dict[str, int]
    findings: int
    duration_ms: float
    generalization: Optional[IngestResponse] = None


@router.post("/learn/repository", response_model=RepositoryResponse)
def learn_repository(
    payload: RepositoryRequest,
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> RepositoryResponse:
    """Analyse a repository *on the server's filesystem*. Allowed only in
    local mode (or when explicitly enabled): in a shared deployment,
    clients run `mnemosyne learn repo` locally and push the results."""
    settings = container.settings
    if settings.auth_mode != "local" and not settings.allow_server_side_repo_analysis:
        raise PermissionDeniedError(
            "server-side repository analysis is disabled; run `mnemosyne learn repo` on the client instead"
        )
    result = container.learning.learn_repository(
        payload.path, principal=principal, author_email=payload.author_email, dry_run=payload.dry_run,
        generalize=payload.generalize,
    )
    base = IngestResponse.from_report(result.report) if result.report else IngestResponse(
        summary="dry run: nothing stored", counts={}, outcomes=[])
    return RepositoryResponse(
        **base.model_dump(),
        repo_id=result.analysis.identity.repo_id,
        languages=result.analysis.languages,
        findings=len(result.analysis.findings),
        duration_ms=result.analysis.duration_ms,
        generalization=IngestResponse.from_report(result.generalization) if result.generalization else None,
    )


@router.post("/learn/generalize", response_model=IngestResponse)
def generalize(
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> IngestResponse:
    return IngestResponse.from_report(container.learning.generalize(principal=principal))


class FeedbackRequest(BaseModel):
    signal: FeedbackSignal
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/memories/{memory_id}/feedback", response_model=MemoryResponse, tags=["memories"])
def feedback(
    memory_id: UUID,
    payload: FeedbackRequest,
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> MemoryResponse:
    """helpful / not_helpful adjust importance; confirm makes an inferred
    memory explicit; wrong / outdated archive it."""
    memory = container.learning.feedback(memory_id, payload.signal, note=payload.note, principal=principal)
    return MemoryResponse.from_domain(memory)
