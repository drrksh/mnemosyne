"""
REST API for memory CRUD, versioning, and search.

Route handlers are intentionally thin: parse/validate the request via
Pydantic, call exactly one MemoryService method, map the domain result
back to a response schema. No business logic lives here.

Route registration order matters: the literal `/batch` routes are
registered before the parametrized `/{memory_id}` routes so that a
request to `/memories/batch` is not swallowed by the `{memory_id}`
path parameter.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from mnemosyne.api.dependencies import get_memory_service, get_principal
from mnemosyne.api.schemas import (
    BatchCreateRequest,
    BulkUpdateRequest,
    EvidenceResponse,
    MemoryCreateRequest,
    MemoryResponse,
    MemoryUpdateRequest,
    MemoryVersionResponse,
    PaginatedMemoryResponse,
)
from mnemosyne.config import get_settings
from mnemosyne.domain.dto import BulkUpdateItem, SearchCriteria
from mnemosyne.domain.enums import MemoryOrigin, MemoryStatus, MemoryType
from mnemosyne.domain.principal import Principal
from mnemosyne.domain.scope import canonical_scope
from mnemosyne.services.memory_service import MemoryService

_settings = get_settings()

router = APIRouter(prefix="/api/v1/memories", tags=["memories"])


# ----------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------
@router.post("", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
def create_memory(
    payload: MemoryCreateRequest,
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> MemoryResponse:
    memory = service.create_memory(payload.to_domain(), principal=principal)
    return MemoryResponse.from_domain(memory)


@router.post("/batch", response_model=list[MemoryResponse], status_code=status.HTTP_201_CREATED)
def create_memories_batch(
    payload: BatchCreateRequest,
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> list[MemoryResponse]:
    memories = service.create_memories_batch([item.to_domain() for item in payload.items], principal=principal)
    return [MemoryResponse.from_domain(m) for m in memories]


@router.patch("/batch", response_model=list[MemoryResponse])
def bulk_update_memories(
    payload: BulkUpdateRequest,
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> list[MemoryResponse]:
    items = [
        BulkUpdateItem(
            memory_id=entry.memory_id,
            changes=entry.changes.to_domain(),
            expected_version=entry.changes.expected_version,
        )
        for entry in payload.items
    ]
    memories = service.bulk_update_memories(items, principal=principal)
    return [MemoryResponse.from_domain(m) for m in memories]


# ----------------------------------------------------------------------
# Search / list
# ----------------------------------------------------------------------
@router.get("", response_model=PaginatedMemoryResponse)
def search_memories(
    q: Optional[str] = Query(default=None, description="Case-insensitive keyword search across title/summary/content"),
    memory_type: Optional[list[MemoryType]] = Query(default=None),
    tag: Optional[list[str]] = Query(default=None),
    match_all_tags: bool = Query(default=False),
    status_filter: Optional[list[MemoryStatus]] = Query(default=None, alias="status"),
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    updated_after: Optional[datetime] = None,
    updated_before: Optional[datetime] = None,
    min_confidence: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    max_confidence: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    min_importance: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    max_importance: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    scope: Optional[list[str]] = Query(default=None, description="e.g. global, lang:python, repo:github.com/acme/api"),
    origin: Optional[list[MemoryOrigin]] = Query(default=None),
    key: Optional[list[str]] = Query(default=None, description="Slot key(s), e.g. python.test_framework"),
    pinned: Optional[bool] = None,
    team_id: Optional[str] = None,
    sort_by: Literal[
        "created_at", "updated_at", "title", "confidence_score", "importance_score", "access_count",
        "reinforcement_count",
    ] = "updated_at",
    sort_desc: bool = True,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=_settings.default_page_size, ge=1, le=_settings.max_page_size),
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> PaginatedMemoryResponse:
    criteria = SearchCriteria(
        query=q,
        memory_types=memory_type,
        tags=tag,
        match_all_tags=match_all_tags,
        statuses=status_filter,
        created_after=created_after,
        created_before=created_before,
        updated_after=updated_after,
        updated_before=updated_before,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        min_importance=min_importance,
        max_importance=max_importance,
        sort_by=sort_by,
        sort_desc=sort_desc,
        page=page,
        page_size=page_size,
        scopes=[canonical_scope(s) for s in scope] if scope else None,
        origins=origin,
        keys=key,
        pinned=pinned,
        team_id=team_id,
    )
    result = service.search_memories(criteria, principal=principal)
    return PaginatedMemoryResponse.from_result(result)


# ----------------------------------------------------------------------
# Single-memory CRUD
# ----------------------------------------------------------------------
@router.get("/{memory_id}", response_model=MemoryResponse)
def get_memory(
    memory_id: UUID,
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> MemoryResponse:
    memory = service.get_memory(memory_id, principal=principal)
    return MemoryResponse.from_domain(memory)


@router.patch("/{memory_id}", response_model=MemoryResponse)
def update_memory(
    memory_id: UUID,
    payload: MemoryUpdateRequest,
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> MemoryResponse:
    memory = service.update_memory(
        memory_id, payload.to_domain(), expected_version=payload.expected_version, principal=principal
    )
    return MemoryResponse.from_domain(memory)


@router.post("/{memory_id}/archive", response_model=MemoryResponse)
def archive_memory(
    memory_id: UUID,
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> MemoryResponse:
    return MemoryResponse.from_domain(service.archive_memory(memory_id, principal=principal))


@router.post("/{memory_id}/restore", response_model=MemoryResponse)
def restore_memory(
    memory_id: UUID,
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> MemoryResponse:
    return MemoryResponse.from_domain(service.restore_memory(memory_id, principal=principal))


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(
    memory_id: UUID,
    permanent: bool = Query(default=False, description="If true, permanently erase the memory and its history."),
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> None:
    if permanent:
        service.hard_delete_memory(memory_id, principal=principal)
    else:
        service.soft_delete_memory(memory_id, principal=principal)
    return None


# ----------------------------------------------------------------------
# Version history
# ----------------------------------------------------------------------
@router.get("/{memory_id}/versions", response_model=list[MemoryVersionResponse])
def list_versions(
    memory_id: UUID,
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> list[MemoryVersionResponse]:
    versions = service.list_versions(memory_id, principal=principal)
    return [MemoryVersionResponse.from_domain(v) for v in versions]


@router.get("/{memory_id}/versions/{version_number}", response_model=MemoryVersionResponse)
def get_version(
    memory_id: UUID,
    version_number: int,
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> MemoryVersionResponse:
    version = service.get_version(memory_id, version_number, principal=principal)
    return MemoryVersionResponse.from_domain(version)


# ----------------------------------------------------------------------
# Evidence
# ----------------------------------------------------------------------
@router.get("/{memory_id}/evidence", response_model=list[EvidenceResponse])
def list_evidence(
    memory_id: UUID,
    limit: int = Query(default=50, ge=1, le=500),
    service: MemoryService = Depends(get_memory_service),
    principal: Principal = Depends(get_principal),
) -> list[EvidenceResponse]:
    """The observations behind a memory: why Mnemosyne believes it."""
    return [EvidenceResponse.from_domain(e) for e in service.list_evidence(memory_id, limit=limit, principal=principal)]
