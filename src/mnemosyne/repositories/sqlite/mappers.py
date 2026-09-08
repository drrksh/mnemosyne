"""
Bidirectional mapping between ORM rows and domain entities.

This is the only module allowed to import both `domain.entities` and
`repositories.sqlite.orm_models` — keeping the translation in one place
means a future schema change only requires updating this file, not
every call site that touches a memory.
"""
from __future__ import annotations

from uuid import UUID

from mnemosyne.domain.entities import Memory, MemoryVersion
from mnemosyne.domain.enums import MemoryStatus, MemoryType
from mnemosyne.repositories.sqlite.orm_models import MemoryORM, MemoryVersionORM


def memory_to_domain(orm: MemoryORM) -> Memory:
    return Memory(
        id=UUID(orm.id),
        title=orm.title,
        summary=orm.summary,
        content=orm.content,
        memory_type=MemoryType(orm.memory_type),
        confidence_score=orm.confidence_score,
        importance_score=orm.importance_score,
        tags=sorted(tag.name for tag in orm.tags),
        status=MemoryStatus(orm.status),
        version=orm.version,
        source=orm.source,
        metadata=dict(orm.extra_metadata or {}),
        notes=orm.notes,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
        access_count=orm.access_count,
        last_accessed_at=orm.last_accessed_at,
        deleted_at=orm.deleted_at,
    )


def version_to_domain(orm: MemoryVersionORM) -> MemoryVersion:
    return MemoryVersion(
        id=UUID(orm.id),
        memory_id=UUID(orm.memory_id),
        version_number=orm.version_number,
        title=orm.title,
        summary=orm.summary,
        content=orm.content,
        memory_type=MemoryType(orm.memory_type),
        confidence_score=orm.confidence_score,
        importance_score=orm.importance_score,
        tags=list(orm.tags_snapshot or []),
        status=MemoryStatus(orm.status),
        source=orm.source,
        metadata=dict(orm.metadata_snapshot or {}),
        notes=orm.notes,
        changed_fields=list(orm.changed_fields or []),
        change_reason=orm.change_reason,
        recorded_at=orm.recorded_at,
    )
