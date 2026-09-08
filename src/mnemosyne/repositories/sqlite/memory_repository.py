"""
SQLite/SQLAlchemy implementation of the MemoryRepository port.

Every public method here either returns cleanly or raises one of the
domain exceptions (NotFoundError, ConflictError, RepositoryError,
ValidationError) — SQLAlchemy exceptions never leak past this module.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from mnemosyne.domain.dto import FieldChanges, SearchCriteria, SearchResult
from mnemosyne.domain.entities import Memory, MemoryVersion
from mnemosyne.domain.enums import MemoryStatus
from mnemosyne.domain.exceptions import ConflictError, NotFoundError, RepositoryError, ValidationError
from mnemosyne.repositories.interfaces import MemoryRepository
from mnemosyne.repositories.sqlite.mappers import memory_to_domain, version_to_domain
from mnemosyne.repositories.sqlite.orm_models import MemoryORM, MemoryVersionORM, TagORM
from mnemosyne.utils.time import utcnow

logger = logging.getLogger("mnemosyne.repositories.sqlite")

_SORTABLE_FIELDS = {
    "created_at": MemoryORM.created_at,
    "updated_at": MemoryORM.updated_at,
    "title": MemoryORM.title,
    "confidence_score": MemoryORM.confidence_score,
    "importance_score": MemoryORM.importance_score,
    "access_count": MemoryORM.access_count,
}


class SqliteMemoryRepository(MemoryRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    def add(self, memory: Memory) -> None:
        orm = MemoryORM(
            id=str(memory.id),
            title=memory.title,
            summary=memory.summary,
            content=memory.content,
            memory_type=memory.memory_type.value,
            confidence_score=memory.confidence_score,
            importance_score=memory.importance_score,
            status=memory.status.value,
            source=memory.source,
            extra_metadata=memory.metadata,
            notes=memory.notes,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
            access_count=memory.access_count,
            last_accessed_at=memory.last_accessed_at,
            deleted_at=memory.deleted_at,
        )
        orm.tags = self._resolve_tags(memory.tags)
        self._session.add(orm)
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise ConflictError(f"Memory with id {memory.id} already exists") from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to persist new memory") from exc

    # ------------------------------------------------------------------
    def get(self, memory_id: UUID) -> Optional[Memory]:
        orm = self._session.get(MemoryORM, str(memory_id))
        return memory_to_domain(orm) if orm else None

    # ------------------------------------------------------------------
    def update(self, memory_id: UUID, expected_version: int, changes: FieldChanges) -> Memory:
        orm = self._session.get(MemoryORM, str(memory_id))
        if orm is None:
            raise NotFoundError(f"Memory {memory_id} not found")
        if orm.version != expected_version:
            raise ConflictError(
                f"Memory {memory_id} was modified concurrently "
                f"(expected version {expected_version}, found {orm.version})"
            )

        for key, value in changes.items():
            if key == "tags":
                orm.tags = self._resolve_tags(value)
            elif key == "memory_type":
                orm.memory_type = value.value
            elif key == "status":
                orm.status = value.value
            elif key == "metadata":
                orm.extra_metadata = value
            else:
                setattr(orm, key, value)
        orm.updated_at = utcnow()

        try:
            self._session.flush()
        except StaleDataError as exc:
            self._session.rollback()
            raise ConflictError(f"Memory {memory_id} was modified concurrently") from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to update memory") from exc

        return memory_to_domain(orm)

    # ------------------------------------------------------------------
    def record_access(self, memory_id: UUID) -> Memory:
        orm = self._session.get(MemoryORM, str(memory_id))
        if orm is None:
            raise NotFoundError(f"Memory {memory_id} not found")
        orm.access_count += 1
        orm.last_accessed_at = utcnow()
        try:
            self._session.flush()
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to record memory access") from exc
        return memory_to_domain(orm)

    # ------------------------------------------------------------------
    def add_version(self, version: MemoryVersion) -> None:
        orm = MemoryVersionORM(
            id=str(version.id),
            memory_id=str(version.memory_id),
            version_number=version.version_number,
            title=version.title,
            summary=version.summary,
            content=version.content,
            memory_type=version.memory_type.value,
            confidence_score=version.confidence_score,
            importance_score=version.importance_score,
            status=version.status.value,
            source=version.source,
            tags_snapshot=list(version.tags),
            metadata_snapshot=dict(version.metadata),
            notes=version.notes,
            changed_fields=list(version.changed_fields),
            change_reason=version.change_reason,
            recorded_at=version.recorded_at,
        )
        self._session.add(orm)
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise ConflictError(
                f"Version {version.version_number} for memory {version.memory_id} already recorded"
            ) from exc
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to persist memory version") from exc

    # ------------------------------------------------------------------
    def list_versions(self, memory_id: UUID) -> list[MemoryVersion]:
        stmt = (
            select(MemoryVersionORM)
            .where(MemoryVersionORM.memory_id == str(memory_id))
            .order_by(MemoryVersionORM.version_number.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return [version_to_domain(row) for row in rows]

    # ------------------------------------------------------------------
    def get_version(self, memory_id: UUID, version_number: int) -> Optional[MemoryVersion]:
        stmt = select(MemoryVersionORM).where(
            MemoryVersionORM.memory_id == str(memory_id),
            MemoryVersionORM.version_number == version_number,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return version_to_domain(row) if row else None

    # ------------------------------------------------------------------
    def hard_delete(self, memory_id: UUID) -> bool:
        orm = self._session.get(MemoryORM, str(memory_id))
        if orm is None:
            return False
        self._session.delete(orm)
        try:
            self._session.flush()
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to permanently delete memory") from exc
        return True

    # ------------------------------------------------------------------
    def exists(self, memory_id: UUID) -> bool:
        stmt = select(MemoryORM.id).where(MemoryORM.id == str(memory_id))
        return self._session.execute(stmt).scalar_one_or_none() is not None

    # ------------------------------------------------------------------
    def search(self, criteria: SearchCriteria) -> SearchResult:
        if criteria.sort_by not in _SORTABLE_FIELDS:
            raise ValidationError(f"Cannot sort by '{criteria.sort_by}'")
        if criteria.page < 1 or criteria.page_size < 1:
            raise ValidationError("page and page_size must be >= 1")

        stmt = select(MemoryORM)

        if criteria.statuses:
            stmt = stmt.where(MemoryORM.status.in_([s.value for s in criteria.statuses]))
        else:
            # Default visibility excludes soft-deleted memories; callers
            # that want them back must ask for them explicitly via
            # `statuses=[MemoryStatus.DELETED, ...]`.
            stmt = stmt.where(MemoryORM.status != MemoryStatus.DELETED.value)

        if criteria.memory_types:
            stmt = stmt.where(MemoryORM.memory_type.in_([t.value for t in criteria.memory_types]))

        if criteria.query:
            like = f"%{criteria.query}%"
            stmt = stmt.where(
                or_(
                    MemoryORM.title.ilike(like),
                    MemoryORM.summary.ilike(like),
                    MemoryORM.content.ilike(like),
                )
            )

        if criteria.created_after:
            stmt = stmt.where(MemoryORM.created_at >= criteria.created_after)
        if criteria.created_before:
            stmt = stmt.where(MemoryORM.created_at <= criteria.created_before)
        if criteria.updated_after:
            stmt = stmt.where(MemoryORM.updated_at >= criteria.updated_after)
        if criteria.updated_before:
            stmt = stmt.where(MemoryORM.updated_at <= criteria.updated_before)

        if criteria.min_confidence is not None:
            stmt = stmt.where(MemoryORM.confidence_score >= criteria.min_confidence)
        if criteria.max_confidence is not None:
            stmt = stmt.where(MemoryORM.confidence_score <= criteria.max_confidence)
        if criteria.min_importance is not None:
            stmt = stmt.where(MemoryORM.importance_score >= criteria.min_importance)
        if criteria.max_importance is not None:
            stmt = stmt.where(MemoryORM.importance_score <= criteria.max_importance)

        if criteria.tags:
            wanted = sorted({t.strip().lower() for t in criteria.tags if t.strip()})
            if wanted:
                if criteria.match_all_tags:
                    matching_ids = (
                        select(MemoryORM.id)
                        .join(MemoryORM.tags)
                        .where(TagORM.name.in_(wanted))
                        .group_by(MemoryORM.id)
                        .having(func.count(func.distinct(TagORM.name)) == len(wanted))
                    )
                else:
                    matching_ids = (
                        select(MemoryORM.id).join(MemoryORM.tags).where(TagORM.name.in_(wanted)).distinct()
                    )
                stmt = stmt.where(MemoryORM.id.in_(matching_ids))

        # Count against the fully-filtered set, BEFORE pagination/order
        # are applied, so `total` reflects the whole matching set.
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = self._session.execute(count_stmt).scalar_one()

        sort_column = _SORTABLE_FIELDS[criteria.sort_by]
        stmt = stmt.order_by(sort_column.desc() if criteria.sort_desc else sort_column.asc())
        stmt = stmt.offset((criteria.page - 1) * criteria.page_size).limit(criteria.page_size)

        rows = self._session.execute(stmt).scalars().all()
        items = [memory_to_domain(row) for row in rows]

        return SearchResult(items=items, total=total, page=criteria.page, page_size=criteria.page_size)

    # ------------------------------------------------------------------
    def _resolve_tags(self, tag_names: list[str]) -> list[TagORM]:
        """Get-or-create TagORM rows for the given (already-normalized)
        tag names, preserving input order and de-duplicating."""
        if not tag_names:
            return []
        wanted = list(dict.fromkeys(t.strip().lower() for t in tag_names if t.strip()))
        if not wanted:
            return []
        existing = {
            tag.name: tag
            for tag in self._session.execute(select(TagORM).where(TagORM.name.in_(wanted))).scalars()
        }
        result: list[TagORM] = []
        for name in wanted:
            tag = existing.get(name)
            if tag is None:
                tag = TagORM(name=name)
                self._session.add(tag)
                self._session.flush()
                existing[name] = tag
            result.append(tag)
        return result
