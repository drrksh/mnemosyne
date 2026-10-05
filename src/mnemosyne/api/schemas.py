"""
Pydantic request/response schemas for the HTTP API.

These are the ONLY place that knows about JSON wire format; everything
below (service, engine, repository) works with plain domain
dataclasses. Keeping schemas separate from domain entities means the
HTTP contract can evolve (rename a field for REST convention, add an
optional field) without touching business logic.

Field length limits are read from Settings once at import time (rather
than hardcoded) so there is exactly one place — `config.py` — where
"no magic numbers" actually lives; see DECISIONS.md, "Configuration
strategy" for the trade-off this implies.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from mnemosyne.config import get_settings
from mnemosyne.domain.dto import MemoryUpdateData, NewMemoryData, SearchResult
from mnemosyne.domain.entities import Evidence, Memory, MemoryVersion
from mnemosyne.domain.enums import EvidenceKind, MemoryOrigin, MemoryStatus, MemoryType

_settings = get_settings()


class MemoryCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=_settings.max_title_length)
    summary: str = Field(default="", max_length=_settings.max_summary_length)
    content: str = Field(default="")
    memory_type: MemoryType = MemoryType.UNKNOWN
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)
    importance_score: float = Field(default=0.5, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    source: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None
    scope: str = Field(default="global", description="'global', 'lang:<language>' or 'repo:<repo-id>'")
    key: Optional[str] = Field(
        default=None, description="Slot key such as 'python.test_framework'; a new value supersedes the old one."
    )
    value: Optional[str] = None
    origin: MemoryOrigin = MemoryOrigin.EXPLICIT
    pinned: bool = False
    team_id: Optional[str] = Field(default=None, description="Create as a team memory (caller must be a member).")

    def to_domain(self) -> NewMemoryData:
        return NewMemoryData(
            title=self.title,
            summary=self.summary,
            content=self.content,
            memory_type=self.memory_type,
            confidence_score=self.confidence_score,
            importance_score=self.importance_score,
            tags=self.tags,
            source=self.source,
            metadata=self.metadata,
            notes=self.notes,
            scope=self.scope,
            key=self.key,
            value=self.value,
            origin=self.origin,
            pinned=self.pinned,
            team_id=self.team_id,
        )


class MemoryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=_settings.max_title_length)
    summary: Optional[str] = Field(default=None, max_length=_settings.max_summary_length)
    content: Optional[str] = None
    memory_type: Optional[MemoryType] = None
    confidence_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    importance_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    tags: Optional[list[str]] = None
    source: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    notes: Optional[str] = None
    scope: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    pinned: Optional[bool] = None
    expected_version: Optional[int] = Field(
        default=None,
        description="If set, the update is rejected with 409 unless the memory is still at this version.",
    )

    def to_domain(self) -> MemoryUpdateData:
        provided = self.model_fields_set
        data = MemoryUpdateData()
        for field_name in (
            "title", "summary", "content", "memory_type", "confidence_score",
            "importance_score", "tags", "source", "metadata", "notes",
            "scope", "key", "value", "pinned",
        ):
            if field_name in provided:
                setattr(data, field_name, getattr(self, field_name))
        return data


class MemoryResponse(BaseModel):
    id: UUID
    title: str
    summary: str
    content: str
    memory_type: MemoryType
    confidence_score: float
    importance_score: float
    tags: list[str]
    status: MemoryStatus
    version: int
    source: Optional[str]
    metadata: dict[str, Any]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime
    access_count: int
    last_accessed_at: Optional[datetime]
    deleted_at: Optional[datetime]
    owner_id: str
    team_id: Optional[str]
    scope: str
    key: Optional[str]
    value: Optional[str]
    origin: MemoryOrigin
    reinforcement_count: int
    last_reinforced_at: Optional[datetime]
    pinned: bool
    superseded_by: Optional[UUID]

    @classmethod
    def from_domain(cls, memory: Memory) -> MemoryResponse:
        return cls(
            id=memory.id,
            title=memory.title,
            summary=memory.summary,
            content=memory.content,
            memory_type=memory.memory_type,
            confidence_score=memory.confidence_score,
            importance_score=memory.importance_score,
            tags=memory.tags,
            status=memory.status,
            version=memory.version,
            source=memory.source,
            metadata=memory.metadata,
            notes=memory.notes,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
            access_count=memory.access_count,
            last_accessed_at=memory.last_accessed_at,
            deleted_at=memory.deleted_at,
            owner_id=memory.owner_id,
            team_id=memory.team_id,
            scope=memory.scope,
            key=memory.key,
            value=memory.value,
            origin=memory.origin,
            reinforcement_count=memory.reinforcement_count,
            last_reinforced_at=memory.last_reinforced_at,
            pinned=memory.pinned,
            superseded_by=memory.superseded_by,
        )


class MemoryVersionResponse(BaseModel):
    id: UUID
    memory_id: UUID
    version_number: int
    title: str
    summary: str
    content: str
    memory_type: MemoryType
    confidence_score: float
    importance_score: float
    tags: list[str]
    status: MemoryStatus
    source: Optional[str]
    metadata: dict[str, Any]
    notes: Optional[str]
    changed_fields: list[str]
    change_reason: Optional[str]
    recorded_at: datetime
    scope: str
    key: Optional[str]
    value: Optional[str]
    origin: MemoryOrigin
    pinned: bool
    superseded_by: Optional[UUID]

    @classmethod
    def from_domain(cls, version: MemoryVersion) -> MemoryVersionResponse:
        return cls(
            id=version.id,
            memory_id=version.memory_id,
            version_number=version.version_number,
            title=version.title,
            summary=version.summary,
            content=version.content,
            memory_type=version.memory_type,
            confidence_score=version.confidence_score,
            importance_score=version.importance_score,
            tags=version.tags,
            status=version.status,
            source=version.source,
            metadata=version.metadata,
            notes=version.notes,
            changed_fields=version.changed_fields,
            change_reason=version.change_reason,
            recorded_at=version.recorded_at,
            scope=version.scope,
            key=version.key,
            value=version.value,
            origin=version.origin,
            pinned=version.pinned,
            superseded_by=version.superseded_by,
        )


class PaginatedMemoryResponse(BaseModel):
    items: list[MemoryResponse]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool

    @classmethod
    def from_result(cls, result: SearchResult) -> PaginatedMemoryResponse:
        return cls(
            items=[MemoryResponse.from_domain(m) for m in result.items],
            total=result.total,
            page=result.page,
            page_size=result.page_size,
            total_pages=result.total_pages,
            has_next=result.has_next,
        )


class BatchCreateRequest(BaseModel):
    items: list[MemoryCreateRequest] = Field(..., min_length=1, max_length=500)


class BulkUpdateEntry(BaseModel):
    memory_id: UUID
    changes: MemoryUpdateRequest


class BulkUpdateRequest(BaseModel):
    items: list[BulkUpdateEntry] = Field(..., min_length=1, max_length=500)


class EvidenceResponse(BaseModel):
    id: UUID
    memory_id: UUID
    kind: EvidenceKind
    source_ref: str
    excerpt: str
    weight: float
    observed_at: datetime

    @classmethod
    def from_domain(cls, evidence: Evidence) -> EvidenceResponse:
        return cls(
            id=evidence.id,
            memory_id=evidence.memory_id,
            kind=evidence.kind,
            source_ref=evidence.source_ref,
            excerpt=evidence.excerpt,
            weight=evidence.weight,
            observed_at=evidence.observed_at,
        )


class ErrorResponse(BaseModel):
    error: str
    detail: str
    request_id: Optional[str] = None
