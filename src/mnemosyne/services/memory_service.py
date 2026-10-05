"""
MemoryService: the application/orchestration layer for memory CRUD.
See the package docstring in `services/__init__.py` for the layer's
responsibilities.

Every method acts on behalf of a `Principal`. Omitting it means "the
service's default principal" — the local user in single-user mode.
Multi-tenant callers (the REST API in `api_key` mode) always pass the
authenticated principal explicitly.

Tenancy rule: a memory the principal cannot see is reported as *not
found*, never as *forbidden*, so the API does not leak which ids exist.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Callable, Optional
from uuid import UUID

from mnemosyne.core.memory_engine import MemoryEngine, UpdatePlan
from mnemosyne.db.unit_of_work import UnitOfWork
from mnemosyne.domain.dto import (
    BulkUpdateItem,
    MemoryUpdateData,
    NewMemoryData,
    SearchCriteria,
    SearchResult,
)
from mnemosyne.domain.entities import Evidence, Memory, MemoryVersion
from mnemosyne.domain.enums import MemoryStatus
from mnemosyne.domain.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from mnemosyne.domain.principal import LOCAL_PRINCIPAL, Permission, Principal
from mnemosyne.retrieval.indexer import MemoryIndexer
from mnemosyne.services import audit

logger = logging.getLogger("mnemosyne.services.memory")

UowFactory = Callable[[], UnitOfWork]


class MemoryService:
    def __init__(
        self,
        uow_factory: UowFactory,
        engine: MemoryEngine,
        *,
        indexer: Optional[MemoryIndexer] = None,
        default_principal: Principal = LOCAL_PRINCIPAL,
    ) -> None:
        self._uow_factory = uow_factory
        self._engine = engine
        self._indexer = indexer
        self._default_principal = default_principal

    @property
    def engine(self) -> MemoryEngine:
        return self._engine

    def _who(self, principal: Optional[Principal]) -> Principal:
        return principal or self._default_principal

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------
    @staticmethod
    def _require(principal: Principal, permission: str) -> None:
        if not principal.has(permission):
            raise PermissionDeniedError(f"this credential lacks the '{permission}' permission")

    @staticmethod
    def _visible(memory: Optional[Memory], principal: Principal, memory_id: UUID) -> Memory:
        if memory is None or not principal.can_see(memory):
            raise NotFoundError(f"Memory {memory_id} not found")
        return memory

    def _writable(self, memory: Optional[Memory], principal: Principal, memory_id: UUID) -> Memory:
        memory = self._visible(memory, principal, memory_id)
        self._require(principal, Permission.WRITE)
        if not principal.can_write_to(memory.team_id):
            raise NotFoundError(f"Memory {memory_id} not found")
        return memory

    def _index(self, uow: UnitOfWork, memories: list[Memory]) -> None:
        if self._indexer is not None:
            self._indexer.index_many(uow, memories)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    def create_memory(self, data: NewMemoryData, *, principal: Optional[Principal] = None) -> Memory:
        who = self._who(principal)
        self._require(who, Permission.WRITE)
        if not who.can_write_to(data.team_id):
            raise NotFoundError(f"Team {data.team_id} not found")
        memory = self._engine.build_new_memory(data, owner_id=who.user_id)
        with self._uow_factory() as uow:
            uow.memories.add(memory)
            self._index(uow, [memory])
            audit.record(uow, who, "memory.create", target_type="memory", target_id=str(memory.id),
                         memory_type=memory.memory_type.value, scope=memory.scope, team_id=memory.team_id)
            uow.commit()
        logger.info(
            "memory_created",
            extra={"memory_id": str(memory.id), "memory_type": memory.memory_type.value},
        )
        return memory

    def create_memories_batch(
        self, items: list[NewMemoryData], *, principal: Optional[Principal] = None
    ) -> list[Memory]:
        who = self._who(principal)
        self._require(who, Permission.WRITE)
        for item in items:
            if not who.can_write_to(item.team_id):
                raise NotFoundError(f"Team {item.team_id} not found")
        built = [self._engine.build_new_memory(item, owner_id=who.user_id) for item in items]
        with self._uow_factory() as uow:
            for memory in built:
                uow.memories.add(memory)
            self._index(uow, built)
            audit.record(uow, who, "memory.create_batch", target_type="memory", count=len(built))
            uow.commit()
        logger.info("memories_batch_created", extra={"count": len(built)})
        return built

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get_memory(
        self,
        memory_id: UUID,
        *,
        include_deleted: bool = False,
        track_access: bool = True,
        principal: Optional[Principal] = None,
    ) -> Memory:
        who = self._who(principal)
        self._require(who, Permission.READ)
        with self._uow_factory() as uow:
            memory = self._visible(uow.memories.get(memory_id), who, memory_id)
            if not memory.is_visible(include_deleted=include_deleted):
                raise NotFoundError(f"Memory {memory_id} not found")
            if track_access:
                memory = uow.memories.record_access(memory_id)
            uow.commit()
        return memory

    def search_memories(self, criteria: SearchCriteria, *, principal: Optional[Principal] = None) -> SearchResult:
        who = self._who(principal)
        self._require(who, Permission.READ)
        # The tenancy filter always comes from the principal, never from
        # the caller-supplied criteria.
        criteria.visibility = who.visibility
        with self._uow_factory() as uow:
            result = uow.memories.search(criteria)
            uow.commit()
        return result

    def list_evidence(
        self, memory_id: UUID, *, limit: int = 100, principal: Optional[Principal] = None
    ) -> list[Evidence]:
        who = self._who(principal)
        self._require(who, Permission.READ)
        with self._uow_factory() as uow:
            self._visible(uow.memories.get(memory_id), who, memory_id)
            evidence = uow.evidence.list_for_memory(memory_id, limit=limit)
            uow.commit()
        return evidence

    def stats(self, *, principal: Optional[Principal] = None) -> dict[str, Any]:
        """What the principal's memory holds, by status, type, origin and
        scope (repositories and languages are listed individually)."""
        who = self._who(principal)
        self._require(who, Permission.READ)
        with self._uow_factory() as uow:
            groups = uow.memories.counts(who.visibility)
            uow.commit()
        by_status: Counter[str] = Counter()
        by_type: Counter[str] = Counter()
        by_origin: Counter[str] = Counter()
        by_scope: Counter[str] = Counter()
        for status, memory_type, origin, scope, count in groups:
            by_status[status] += count
            if status == MemoryStatus.ACTIVE.value:
                by_type[memory_type] += count
                by_origin[origin] += count
                by_scope[scope] += count
        return {
            "active": by_status.get(MemoryStatus.ACTIVE.value, 0),
            "by_status": dict(by_status.most_common()),
            "by_type": dict(by_type.most_common()),
            "by_origin": dict(by_origin.most_common()),
            "by_scope": dict(by_scope.most_common()),
        }

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    def update_memory(
        self,
        memory_id: UUID,
        changes: MemoryUpdateData,
        *,
        expected_version: int | None = None,
        principal: Optional[Principal] = None,
    ) -> Memory:
        who = self._who(principal)
        with self._uow_factory() as uow:
            existing = self._writable(uow.memories.get(memory_id), who, memory_id)
            if existing.status == MemoryStatus.DELETED:
                raise NotFoundError(f"Memory {memory_id} not found")
            if expected_version is not None and existing.version != expected_version:
                raise ConflictError(
                    f"Memory {memory_id} has version {existing.version}, "
                    f"but update expected version {expected_version}"
                )

            plan = self._engine.apply_update(existing, changes)
            if plan.is_noop:
                uow.commit()
                return existing

            updated = self._persist_plan(uow, existing, plan)
            audit.record(uow, who, "memory.update", target_type="memory", target_id=str(memory_id),
                         changed_fields=plan.changed_field_names)
            uow.commit()

        logger.info(
            "memory_updated",
            extra={"memory_id": str(memory_id), "changed_fields": plan.changed_field_names},
        )
        return updated

    def bulk_update_memories(
        self, items: list[BulkUpdateItem], *, principal: Optional[Principal] = None
    ) -> list[Memory]:
        who = self._who(principal)
        results: list[Memory] = []
        with self._uow_factory() as uow:
            for item in items:
                existing = self._writable(uow.memories.get(item.memory_id), who, item.memory_id)
                if existing.status == MemoryStatus.DELETED:
                    raise NotFoundError(f"Memory {item.memory_id} not found")
                if item.expected_version is not None and existing.version != item.expected_version:
                    raise ConflictError(f"Memory {item.memory_id} version mismatch")

                plan = self._engine.apply_update(existing, item.changes)
                if plan.is_noop:
                    results.append(existing)
                    continue
                results.append(self._persist_plan(uow, existing, plan))
            audit.record(uow, who, "memory.update_batch", target_type="memory", count=len(items))
            uow.commit()
        logger.info("memories_bulk_updated", extra={"count": len(results)})
        return results

    def _persist_plan(self, uow: UnitOfWork, existing: Memory, plan: UpdatePlan) -> Memory:
        uow.memories.add_version(plan.version_snapshot)
        updated = uow.memories.update(existing.id, existing.version, plan.persist_changes)
        if {"title", "summary", "content", "tags", "key", "value"} & set(plan.changed_fields):
            self._index(uow, [updated])
        return updated

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------
    def _transition_status(
        self, memory_id: UUID, target: MemoryStatus, *, change_reason: str, principal: Optional[Principal]
    ) -> Memory:
        who = self._who(principal)
        with self._uow_factory() as uow:
            existing = self._writable(uow.memories.get(memory_id), who, memory_id)

            plan = self._engine.apply_status_transition(existing, target, change_reason=change_reason)
            if plan.is_noop:
                uow.commit()
                return existing

            uow.memories.add_version(plan.version_snapshot)
            updated = uow.memories.update(memory_id, existing.version, plan.persist_changes)
            audit.record(uow, who, f"memory.{change_reason}", target_type="memory", target_id=str(memory_id))
            uow.commit()

        logger.info(
            "memory_status_changed", extra={"memory_id": str(memory_id), "target_status": target.value}
        )
        return updated

    def archive_memory(self, memory_id: UUID, *, principal: Optional[Principal] = None) -> Memory:
        return self._transition_status(memory_id, MemoryStatus.ARCHIVED, change_reason="archived", principal=principal)

    def restore_memory(self, memory_id: UUID, *, principal: Optional[Principal] = None) -> Memory:
        return self._transition_status(memory_id, MemoryStatus.ACTIVE, change_reason="restored", principal=principal)

    def soft_delete_memory(self, memory_id: UUID, *, principal: Optional[Principal] = None) -> Memory:
        return self._transition_status(
            memory_id, MemoryStatus.DELETED, change_reason="soft_deleted", principal=principal
        )

    def hard_delete_memory(self, memory_id: UUID, *, principal: Optional[Principal] = None) -> None:
        who = self._who(principal)
        with self._uow_factory() as uow:
            existing = self._visible(uow.memories.get(memory_id), who, memory_id)
            if not who.can_hard_delete(existing):
                raise PermissionDeniedError("only the owner (or a team maintainer) may permanently delete this memory")
            deleted = uow.memories.hard_delete(memory_id)
            audit.record(uow, who, "memory.hard_delete", target_type="memory", target_id=str(memory_id))
            uow.commit()
        if not deleted:  # pragma: no cover - raced with another delete
            raise NotFoundError(f"Memory {memory_id} not found")
        logger.info("memory_hard_deleted", extra={"memory_id": str(memory_id)})

    def erase_personal_memories(self, *, principal: Optional[Principal] = None) -> int:
        """Right to erasure: permanently delete every personal memory of the
        principal, including history, evidence and embeddings. Team
        memories they contributed to remain, as they belong to the team."""
        who = self._who(principal)
        self._require(who, Permission.WRITE)
        with self._uow_factory() as uow:
            count = uow.memories.hard_delete_personal(who.user_id)
            audit.record(uow, who, "memory.erase_all", target_type="user", target_id=who.user_id, count=count)
            uow.commit()
        logger.info("personal_memories_erased", extra={"count": count})
        return count

    # ------------------------------------------------------------------
    # Version history
    # ------------------------------------------------------------------
    def list_versions(self, memory_id: UUID, *, principal: Optional[Principal] = None) -> list[MemoryVersion]:
        who = self._who(principal)
        self._require(who, Permission.READ)
        with self._uow_factory() as uow:
            self._visible(uow.memories.get(memory_id), who, memory_id)
            versions = uow.memories.list_versions(memory_id)
            uow.commit()
        return versions

    def get_version(
        self, memory_id: UUID, version_number: int, *, principal: Optional[Principal] = None
    ) -> MemoryVersion:
        who = self._who(principal)
        self._require(who, Permission.READ)
        with self._uow_factory() as uow:
            memory = self._visible(uow.memories.get(memory_id), who, memory_id)
            version = uow.memories.get_version(memory_id, version_number)
            uow.commit()

        if version is not None:
            return version
        if memory.version != version_number:
            raise NotFoundError(f"Memory {memory_id} has no version {version_number}")

        # The requested version number is the CURRENT (not yet superseded)
        # state, which never gets its own row in memory_versions — we
        # synthesize an equivalent view from the live memory instead of
        # duplicating that data at write time.
        return MemoryVersion(
            id=memory.id,
            memory_id=memory.id,
            version_number=memory.version,
            title=memory.title,
            summary=memory.summary,
            content=memory.content,
            memory_type=memory.memory_type,
            confidence_score=memory.confidence_score,
            importance_score=memory.importance_score,
            tags=list(memory.tags),
            status=memory.status,
            source=memory.source,
            metadata=dict(memory.metadata),
            notes=memory.notes,
            changed_fields=[],
            change_reason="current",
            recorded_at=memory.updated_at,
            scope=memory.scope,
            key=memory.key,
            value=memory.value,
            origin=memory.origin,
            pinned=memory.pinned,
            superseded_by=memory.superseded_by,
        )
