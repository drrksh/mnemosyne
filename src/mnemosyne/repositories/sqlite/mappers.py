"""
Bidirectional mapping between ORM rows and domain entities.

This is the only module allowed to import both `domain.entities` and
`repositories.sqlite.orm_models` — keeping the translation in one place
means a future schema change only requires updating this file, not
every call site that touches a memory.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping, Optional
from uuid import UUID

from mnemosyne.domain.entities import (
    ApiKey,
    AuditEvent,
    Evidence,
    Memory,
    MemoryVersion,
    Team,
    TeamMembership,
    User,
)
from mnemosyne.domain.enums import EvidenceKind, MemoryOrigin, MemoryStatus, MemoryType, TeamRole
from mnemosyne.repositories.sqlite.orm_models import (
    ApiKeyORM,
    AuditEventORM,
    MemoryEvidenceORM,
    MemoryORM,
    MemoryVersionORM,
    TeamMemberORM,
    TeamORM,
    UserORM,
)


def aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite drops tzinfo on round-trip even for `DateTime(timezone=True)`
    columns; every timestamp in Mnemosyne is UTC, so re-attach it. On
    PostgreSQL values already come back aware and pass through."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _uuid_or_none(value: Optional[str]) -> Optional[UUID]:
    return UUID(value) if value else None


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
        created_at=aware(orm.created_at),  # type: ignore[arg-type]
        updated_at=aware(orm.updated_at),  # type: ignore[arg-type]
        access_count=orm.access_count,
        last_accessed_at=aware(orm.last_accessed_at),
        deleted_at=aware(orm.deleted_at),
        owner_id=orm.owner_id,
        team_id=orm.team_id,
        scope=orm.scope,
        key=orm.slot_key,
        value=orm.slot_value,
        fingerprint=orm.fingerprint,
        origin=MemoryOrigin(orm.origin),
        reinforcement_count=orm.reinforcement_count,
        last_reinforced_at=aware(orm.last_reinforced_at),
        pinned=bool(orm.pinned),
        superseded_by=_uuid_or_none(orm.superseded_by),
    )


def memory_from_row(row: Mapping[str, Any], tags: list[str]) -> Memory:
    """Build a Memory from a Core result row (no ORM identity map). Used
    by the retrieval index loader, which reads thousands of rows."""
    return Memory(
        id=UUID(row["id"]),
        title=row["title"],
        summary=row["summary"],
        content=row["content"],
        memory_type=MemoryType(row["memory_type"]),
        confidence_score=row["confidence_score"],
        importance_score=row["importance_score"],
        tags=sorted(tags),
        status=MemoryStatus(row["status"]),
        version=row["version"],
        source=row["source"],
        metadata=dict(row["metadata"] or {}),
        notes=row["notes"],
        created_at=aware(row["created_at"]),  # type: ignore[arg-type]
        updated_at=aware(row["updated_at"]),  # type: ignore[arg-type]
        access_count=row["access_count"],
        last_accessed_at=aware(row["last_accessed_at"]),
        deleted_at=aware(row["deleted_at"]),
        owner_id=row["owner_id"],
        team_id=row["team_id"],
        scope=row["scope"],
        key=row["slot_key"],
        value=row["slot_value"],
        fingerprint=row["fingerprint"],
        origin=MemoryOrigin(row["origin"]),
        reinforcement_count=row["reinforcement_count"],
        last_reinforced_at=aware(row["last_reinforced_at"]),
        pinned=bool(row["pinned"]),
        superseded_by=_uuid_or_none(row["superseded_by"]),
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
        recorded_at=aware(orm.recorded_at),  # type: ignore[arg-type]
        scope=orm.scope,
        key=orm.slot_key,
        value=orm.slot_value,
        origin=MemoryOrigin(orm.origin),
        pinned=bool(orm.pinned),
        superseded_by=_uuid_or_none(orm.superseded_by),
    )


def evidence_to_domain(orm: MemoryEvidenceORM) -> Evidence:
    return Evidence(
        id=UUID(orm.id),
        memory_id=UUID(orm.memory_id),
        kind=EvidenceKind(orm.kind),
        source_ref=orm.source_ref,
        excerpt=orm.excerpt,
        weight=orm.weight,
        observed_at=aware(orm.observed_at),  # type: ignore[arg-type]
    )


def user_to_domain(orm: UserORM) -> User:
    return User(
        id=orm.id,
        handle=orm.handle,
        display_name=orm.display_name,
        is_admin=bool(orm.is_admin),
        created_at=aware(orm.created_at),  # type: ignore[arg-type]
        disabled=bool(orm.disabled),
    )


def team_to_domain(orm: TeamORM) -> Team:
    return Team(id=orm.id, name=orm.name, created_at=aware(orm.created_at))  # type: ignore[arg-type]


def membership_to_domain(orm: TeamMemberORM) -> TeamMembership:
    return TeamMembership(
        team_id=orm.team_id,
        user_id=orm.user_id,
        role=TeamRole(orm.role),
        joined_at=aware(orm.joined_at),  # type: ignore[arg-type]
    )


def api_key_to_domain(orm: ApiKeyORM) -> ApiKey:
    return ApiKey(
        id=orm.id,
        user_id=orm.user_id,
        name=orm.name,
        prefix=orm.prefix,
        key_hash=orm.key_hash,
        scopes=list(orm.scopes or []),
        created_at=aware(orm.created_at),  # type: ignore[arg-type]
        last_used_at=aware(orm.last_used_at),
        expires_at=aware(orm.expires_at),
        revoked_at=aware(orm.revoked_at),
    )


def audit_to_domain(orm: AuditEventORM) -> AuditEvent:
    return AuditEvent(
        id=UUID(orm.id),
        occurred_at=aware(orm.occurred_at),  # type: ignore[arg-type]
        actor_id=orm.actor_id,
        action=orm.action,
        target_type=orm.target_type,
        target_id=orm.target_id,
        request_id=orm.request_id,
        details=dict(orm.details or {}),
    )
