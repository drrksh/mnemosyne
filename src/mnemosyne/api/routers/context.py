"""
Context and recall endpoints — the read side of developer memory.

`POST /api/v1/context` is what an assistant integration calls at the
start of a task: it returns a ready-to-inject briefing. `POST
/api/v1/recall` is search with explainable scores.
"""
from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from mnemosyne.api.dependencies import get_container, get_principal
from mnemosyne.api.schemas import MemoryResponse
from mnemosyne.container import Mnemosyne
from mnemosyne.domain.enums import MemoryOrigin, MemoryType
from mnemosyne.domain.exceptions import PermissionDeniedError
from mnemosyne.domain.principal import Permission, Principal
from mnemosyne.domain.scope import canonical_scope
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.learning.workspace import detect_working_context
from mnemosyne.services.context_service import ContextRequest
from mnemosyne.services.retrieval_service import RecallRequest

router = APIRouter(prefix="/api/v1", tags=["context"])


class WorkingContextModel(BaseModel):
    repo_id: Optional[str] = Field(default=None, description="Normalized repository id, e.g. github.com/acme/api")
    languages: list[str] = Field(default_factory=list)
    file_path: Optional[str] = None
    repo_path: Optional[str] = Field(
        default=None,
        description="Local mode only: a path on the server's filesystem to detect the repository and languages from.",
    )

    def resolve(self, container: Mnemosyne) -> WorkingContext:
        if self.repo_path:
            if container.settings.auth_mode != "local" and not container.settings.allow_server_side_repo_analysis:
                raise PermissionDeniedError("repo_path is only accepted in local mode; send repo_id and languages")
            detected = detect_working_context(self.repo_path, file_path=self.file_path,
                                              extra_languages=self.languages)
            return detected
        return WorkingContext.create(repo_id=self.repo_id, languages=self.languages, file_path=self.file_path)


class ContextRequestModel(BaseModel):
    task: Optional[str] = Field(default=None, max_length=20_000, description="What the developer is about to do.")
    context: WorkingContextModel = Field(default_factory=WorkingContextModel)
    max_tokens: int = Field(default=1500, ge=50, le=16_000)
    max_chars: Optional[int] = Field(default=None, ge=200, le=200_000)
    format: Literal["markdown", "xml", "json"] = "markdown"
    wrap: bool = False
    include_profile: bool = True
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    record_access: bool = True
    explain: bool = False


class ContextItemModel(BaseModel):
    id: UUID
    title: str
    memory_type: MemoryType
    scope: str
    section: str
    reason: str
    score: float
    breakdown: Optional[dict[str, float]] = None


class ContextResponse(BaseModel):
    text: str
    estimated_tokens: int
    omitted: int
    overridden: int
    took_ms: float
    repo_id: Optional[str]
    languages: list[str]
    items: list[ContextItemModel]


@router.post("/context", response_model=ContextResponse)
def compile_context(
    payload: ContextRequestModel,
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> ContextResponse:
    if not principal.has(Permission.READ):
        raise PermissionDeniedError("this credential lacks the 'memories:read' permission")
    pack = container.context.compile(
        ContextRequest(
            task=payload.task,
            context=payload.context.resolve(container),
            max_tokens=payload.max_tokens,
            max_chars=payload.max_chars,
            format=payload.format,
            wrap=payload.wrap,
            include_profile=payload.include_profile,
            min_confidence=payload.min_confidence,
            record_access=payload.record_access,
            explain=payload.explain,
        ),
        principal,
    )
    return ContextResponse(
        text=pack.text,
        estimated_tokens=pack.estimated_tokens,
        omitted=pack.omitted,
        overridden=pack.overridden,
        took_ms=pack.took_ms,
        repo_id=pack.context.repo_id,
        languages=list(pack.context.languages),
        items=[
            ContextItemModel(id=i.memory.id, title=i.memory.title, memory_type=i.memory.memory_type,
                             scope=i.memory.scope, section=i.section, reason=i.reason, score=round(i.score, 4),
                             breakdown=i.breakdown)
            for i in pack.items
        ],
    )


@router.get("/profile", response_model=ContextResponse)
def get_profile(
    repo_id: Optional[str] = None,
    language: Optional[list[str]] = None,
    max_tokens: int = 4000,
    format: Literal["markdown", "xml", "json"] = "markdown",
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> ContextResponse:
    """The developer's standing profile for a working context."""
    if not principal.has(Permission.READ):
        raise PermissionDeniedError("this credential lacks the 'memories:read' permission")
    pack = container.context.profile(
        WorkingContext.create(repo_id=repo_id, languages=language or []), principal=principal,
        max_tokens=max(50, min(max_tokens, 16_000)), fmt=format,
    )
    return ContextResponse(
        text=pack.text, estimated_tokens=pack.estimated_tokens, omitted=pack.omitted, overridden=pack.overridden,
        took_ms=pack.took_ms, repo_id=pack.context.repo_id, languages=list(pack.context.languages),
        items=[ContextItemModel(id=i.memory.id, title=i.memory.title, memory_type=i.memory.memory_type,
                                scope=i.memory.scope, section=i.section, reason=i.reason, score=round(i.score, 4))
               for i in pack.items],
    )


class RulesResponse(BaseModel):
    markdown: str
    items: int
    repo_id: Optional[str]


@router.get("/rules", response_model=RulesResponse)
def get_rules(
    repo_id: Optional[str] = None,
    language: Optional[list[str]] = None,
    audience: Literal["repo", "personal"] = "repo",
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> RulesResponse:
    """The body Mnemosyne writes into CLAUDE.md, AGENTS.md and other rule
    files, so `mnemosyne sync` works the same against a team server."""
    from mnemosyne.integrations.exporters import build_profile_markdown

    if not principal.has(Permission.READ):
        raise PermissionDeniedError("this credential lacks the 'memories:read' permission")
    context = WorkingContext.create(repo_id=repo_id, languages=language or [])
    markdown, count = build_profile_markdown(container, context, audience=audience, principal=principal)
    return RulesResponse(markdown=markdown, items=count, repo_id=context.repo_id)


class RecallRequestModel(BaseModel):
    query: str = Field(..., min_length=1, max_length=20_000)
    context: WorkingContextModel = Field(default_factory=WorkingContextModel)
    limit: int = Field(default=10, ge=1, le=100)
    memory_types: Optional[list[MemoryType]] = None
    scopes: Optional[list[str]] = None
    origins: Optional[list[MemoryOrigin]] = None
    min_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    diversify: bool = True
    explain: bool = False


class RecallItem(BaseModel):
    memory: MemoryResponse
    score: float
    breakdown: Optional[dict[str, float]] = None


class RecallResponse(BaseModel):
    items: list[RecallItem]
    overridden: list[UUID]
    candidates: int
    index_size: int
    took_ms: float
    semantic_available: bool


@router.post("/recall", response_model=RecallResponse)
def recall(
    payload: RecallRequestModel,
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> RecallResponse:
    if not principal.has(Permission.READ):
        raise PermissionDeniedError("this credential lacks the 'memories:read' permission")
    result = container.retrieval.recall(
        RecallRequest(
            query=payload.query,
            context=payload.context.resolve(container),
            limit=payload.limit,
            memory_types=payload.memory_types,
            scopes=[canonical_scope(s) for s in payload.scopes] if payload.scopes else None,
            origins=payload.origins,
            min_confidence=payload.min_confidence,
            diversify=payload.diversify,
        ),
        principal,
    )
    return RecallResponse(
        items=[RecallItem(memory=MemoryResponse.from_domain(s.memory), score=round(s.score, 4),
                          breakdown=s.breakdown.as_dict() if payload.explain else None) for s in result.items],
        overridden=[s.memory.id for s in result.overridden],
        candidates=result.candidates,
        index_size=result.index_size,
        took_ms=result.took_ms,
        semantic_available=result.semantic_available,
    )
