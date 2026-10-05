"""
SQLAlchemy implementation of the MemoryRepository port (SQLite and
PostgreSQL).

Every public method here either returns cleanly or raises one of the
domain exceptions (NotFoundError, ConflictError, RepositoryError,
ValidationError) — SQLAlchemy exceptions never leak past this module.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Optional, Sequence
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from mnemosyne.domain.dto import FieldChanges, SearchCriteria, SearchResult
from mnemosyne.domain.entities import Memory, MemoryVersion
from mnemosyne.domain.enums import MemoryStatus
from mnemosyne.domain.exceptions import ConflictError, NotFoundError, RepositoryError, ValidationError
from mnemosyne.domain.principal import Visibility
from mnemosyne.repositories.interfaces import IndexSignature, MemoryRepository
from mnemosyne.repositories.sqlite.mappers import aware, memory_from_row, memory_to_domain, version_to_domain
from mnemosyne.repositories.sqlite.orm_models import (
    MemoryORM,
    MemoryVersionORM,
    TagORM,
    memory_tags_table,
)
from mnemosyne.utils.time import utcnow

logger = logging.getLogger("mnemosyne.repositories.sqlite")

_SORTABLE_FIELDS = {
    "created_at": MemoryORM.created_at,
    "updated_at": MemoryORM.updated_at,
    "title": MemoryORM.title,
    "confidence_score": MemoryORM.confidence_score,
    "importance_score": MemoryORM.importance_score,
    "access_count": MemoryORM.access_count,
    "reinforcement_count": MemoryORM.reinforcement_count,
}

#: Domain field name -> ORM attribute name, where they differ.
_FIELD_TO_COLUMN = {"key": "slot_key", "value": "slot_value", "metadata": "extra_metadata"}

#: Fields `update_dynamics` may change: belief strength, not content.
DYNAMICS_FIELDS = frozenset({"confidence_score", "importance_score", "reinforcement_count", "last_reinforced_at"})

#: Large IN-lists are chunked to stay under SQLite's bound-parameter limit.
_IN_CHUNK = 500


def visibility_clause(visibility: Visibility) -> ColumnElement[bool]:
    """SQL form of `Principal.can_see`: personal memories of the owner, or
    any memory belonging to one of the principal's teams."""
    personal = and_(MemoryORM.owner_id == visibility.owner_id, MemoryORM.team_id.is_(None))
    if visibility.team_ids:
        return or_(personal, MemoryORM.team_id.in_(sorted(visibility.team_ids)))
    return personal


def _team_clause(team_id: Optional[str]) -> ColumnElement[bool]:
    return MemoryORM.team_id.is_(None) if team_id is None else MemoryORM.team_id == team_id


def _chunks(items: Sequence[Any], size: int = _IN_CHUNK) -> list[Sequence[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


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
            owner_id=memory.owner_id,
            team_id=memory.team_id,
            scope=memory.scope,
            slot_key=memory.key,
            slot_value=memory.value,
            fingerprint=memory.fingerprint,
            origin=memory.origin.value,
            reinforcement_count=memory.reinforcement_count,
            last_reinforced_at=memory.last_reinforced_at,
            pinned=memory.pinned,
            superseded_by=str(memory.superseded_by) if memory.superseded_by else None,
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

    def get_many(self, memory_ids: Sequence[UUID]) -> list[Memory]:
        if not memory_ids:
            return []
        found: dict[str, Memory] = {}
        for chunk in _chunks([str(i) for i in memory_ids]):
            rows = self._session.execute(select(MemoryORM).where(MemoryORM.id.in_(chunk))).scalars()
            for row in rows:
                found[row.id] = memory_to_domain(row)
        return [found[str(i)] for i in memory_ids if str(i) in found]

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
            elif key in ("memory_type", "status", "origin"):
                setattr(orm, key, value.value)
            elif key == "superseded_by":
                orm.superseded_by = str(value) if value else None
            else:
                setattr(orm, _FIELD_TO_COLUMN.get(key, key), value)
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
    def update_dynamics(self, memory_id: UUID, changes: FieldChanges) -> Memory:
        unknown = set(changes) - DYNAMICS_FIELDS
        if unknown:
            raise ValidationError(f"not belief-dynamics fields: {sorted(unknown)}")
        try:
            result = self._session.execute(
                update(MemoryORM)
                .where(MemoryORM.id == str(memory_id))
                .values(**changes, updated_at=utcnow())
                .execution_options(synchronize_session=False)
            )
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to update memory dynamics") from exc
        if result.rowcount == 0:
            raise NotFoundError(f"Memory {memory_id} not found")
        orm = self._session.get(MemoryORM, str(memory_id), populate_existing=True)
        assert orm is not None
        return memory_to_domain(orm)

    # ------------------------------------------------------------------
    def record_access(self, memory_id: UUID) -> Memory:
        # A Core UPDATE rather than an ORM attribute change: the ORM's
        # `version_id_col` would otherwise treat a *read* as a new content
        # revision, leaving holes in version history and making concurrent
        # readers conflict with writers.
        now = utcnow()
        try:
            result = self._session.execute(
                update(MemoryORM)
                .where(MemoryORM.id == str(memory_id))
                .values(access_count=MemoryORM.access_count + 1, last_accessed_at=now)
                .execution_options(synchronize_session=False)
            )
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to record memory access") from exc
        if result.rowcount == 0:
            raise NotFoundError(f"Memory {memory_id} not found")
        orm = self._session.get(MemoryORM, str(memory_id), populate_existing=True)
        assert orm is not None
        return memory_to_domain(orm)

    def record_access_many(self, memory_ids: Sequence[UUID]) -> None:
        if not memory_ids:
            return
        now = utcnow()
        try:
            for chunk in _chunks([str(i) for i in memory_ids]):
                self._session.execute(
                    update(MemoryORM)
                    .where(MemoryORM.id.in_(chunk))
                    .values(access_count=MemoryORM.access_count + 1, last_accessed_at=now)
                    .execution_options(synchronize_session=False)
                )
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to record memory access") from exc

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
            change_reason=(version.change_reason or "")[:100] or None,
            recorded_at=version.recorded_at,
            scope=version.scope,
            slot_key=version.key,
            slot_value=version.value,
            origin=version.origin.value,
            pinned=version.pinned,
            superseded_by=str(version.superseded_by) if version.superseded_by else None,
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

    def hard_delete_personal(self, owner_id: str) -> int:
        ids = list(
            self._session.execute(
                select(MemoryORM.id).where(MemoryORM.owner_id == owner_id, MemoryORM.team_id.is_(None))
            ).scalars()
        )
        try:
            for chunk in _chunks(ids):
                # Version/evidence/embedding/tag rows go with their memory
                # via ON DELETE CASCADE.
                self._session.execute(
                    delete(MemoryORM).where(MemoryORM.id.in_(chunk)).execution_options(synchronize_session=False)
                )
            self._session.expire_all()
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to erase memories") from exc
        return len(ids)

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

        if criteria.visibility is not None:
            stmt = stmt.where(visibility_clause(criteria.visibility))

        if criteria.statuses:
            stmt = stmt.where(MemoryORM.status.in_([s.value for s in criteria.statuses]))
        else:
            # Default visibility excludes soft-deleted memories; callers
            # that want them back must ask for them explicitly via
            # `statuses=[MemoryStatus.DELETED, ...]`.
            stmt = stmt.where(MemoryORM.status != MemoryStatus.DELETED.value)

        if criteria.memory_types:
            stmt = stmt.where(MemoryORM.memory_type.in_([t.value for t in criteria.memory_types]))
        if criteria.scopes:
            stmt = stmt.where(MemoryORM.scope.in_(criteria.scopes))
        if criteria.origins:
            stmt = stmt.where(MemoryORM.origin.in_([o.value for o in criteria.origins]))
        if criteria.keys:
            stmt = stmt.where(MemoryORM.slot_key.in_(criteria.keys))
        if criteria.pinned is not None:
            stmt = stmt.where(MemoryORM.pinned.is_(criteria.pinned))
        if criteria.team_id is not None:
            stmt = stmt.where(MemoryORM.team_id == criteria.team_id)

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
        stmt = stmt.order_by(
            sort_column.desc() if criteria.sort_desc else sort_column.asc(),
            MemoryORM.id.asc(),  # deterministic order across equal sort keys
        )
        stmt = stmt.offset((criteria.page - 1) * criteria.page_size).limit(criteria.page_size)

        rows = self._session.execute(stmt).scalars().all()
        items = [memory_to_domain(row) for row in rows]

        return SearchResult(items=items, total=total, page=criteria.page, page_size=criteria.page_size)

    # ------------------------------------------------------------------
    # Retrieval / consolidation support
    # ------------------------------------------------------------------
    def list_for_index(
        self, visibility: Visibility, statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,)
    ) -> list[Memory]:
        table = MemoryORM.__table__
        stmt = select(table).where(
            visibility_clause(visibility), table.c.status.in_([s.value for s in statuses])
        )
        try:
            rows = self._session.execute(stmt).mappings().all()
            ids = [row["id"] for row in rows]
            tags_by_id: dict[str, list[str]] = defaultdict(list)
            for chunk in _chunks(ids):
                tag_rows = self._session.execute(
                    select(memory_tags_table.c.memory_id, TagORM.name)
                    .join(TagORM, TagORM.id == memory_tags_table.c.tag_id)
                    .where(memory_tags_table.c.memory_id.in_(chunk))
                )
                for memory_id, name in tag_rows:
                    tags_by_id[memory_id].append(name)
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to load memories for indexing") from exc
        return [memory_from_row(row, tags_by_id.get(row["id"], [])) for row in rows]

    def index_signature(self, visibility: Visibility) -> IndexSignature:
        stmt = select(
            func.count(MemoryORM.id), func.max(MemoryORM.updated_at), func.coalesce(func.sum(MemoryORM.version), 0)
        ).where(visibility_clause(visibility), MemoryORM.status == MemoryStatus.ACTIVE.value)
        count, max_updated, version_sum = self._session.execute(stmt).one()
        return IndexSignature(count=int(count), max_updated_at=aware(max_updated), version_sum=int(version_sum))

    def counts(self, visibility: Visibility) -> list[tuple[str, str, str, str, int]]:
        stmt = (
            select(MemoryORM.status, MemoryORM.memory_type, MemoryORM.origin, MemoryORM.scope, func.count(MemoryORM.id))
            .where(visibility_clause(visibility))
            .group_by(MemoryORM.status, MemoryORM.memory_type, MemoryORM.origin, MemoryORM.scope)
        )
        return [(str(a), str(b), str(c), str(d), int(n)) for a, b, c, d, n in self._session.execute(stmt).all()]

    def find_slot(self, *, owner_id: str, team_id: Optional[str], scope: str, key: str) -> list[Memory]:
        stmt = select(MemoryORM).where(
            MemoryORM.slot_key == key,
            MemoryORM.scope == scope,
            MemoryORM.status == MemoryStatus.ACTIVE.value,
            _team_clause(team_id),
        )
        if team_id is None:
            stmt = stmt.where(MemoryORM.owner_id == owner_id)
        rows = self._session.execute(stmt.order_by(MemoryORM.updated_at.desc())).scalars().all()
        return [memory_to_domain(row) for row in rows]

    def find_by_fingerprint(
        self, *, owner_id: str, team_id: Optional[str], scope: str, fingerprint: str
    ) -> list[Memory]:
        stmt = select(MemoryORM).where(
            MemoryORM.fingerprint == fingerprint,
            MemoryORM.scope == scope,
            MemoryORM.status == MemoryStatus.ACTIVE.value,
            _team_clause(team_id),
        )
        if team_id is None:
            stmt = stmt.where(MemoryORM.owner_id == owner_id)
        rows = self._session.execute(stmt).scalars().all()
        return [memory_to_domain(row) for row in rows]

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
