"""
MemoryService: the application/orchestration layer. See the package
docstring in `services/__init__.py` for the layer's responsibilities.
"""
from __future__ import annotations

import logging
from typing import Callable
from uuid import UUID

from mnemosyne.core.memory_engine import MemoryEngine
from mnemosyne.db.unit_of_work import UnitOfWork
from mnemosyne.domain.dto import (
    BulkUpdateItem,
    MemoryUpdateData,
    NewMemoryData,
    SearchCriteria,
    SearchResult,
)
from mnemosyne.domain.entities import Memory, MemoryVersion
from mnemosyne.domain.enums import MemoryStatus
from mnemosyne.domain.exceptions import ConflictError, NotFoundError

logger = logging.getLogger("mnemosyne.services.memory")

UowFactory = Callable[[], UnitOfWork]


class MemoryService:
    def __init__(self, uow_factory: UowFactory, engine: MemoryEngine) -> None:
        self._uow_factory = uow_factory
        self._engine = engine

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    def create_memory(self, data: NewMemoryData) -> Memory:
        memory = self._engine.build_new_memory(data)
        with self._uow_factory() as uow:
            uow.memories.add(memory)
            uow.commit()
        logger.info(
            "memory_created",
            extra={"memory_id": str(memory.id), "memory_type": memory.memory_type.value},
        )
        return memory

    def create_memories_batch(self, items: list[NewMemoryData]) -> list[Memory]:
        built = [self._engine.build_new_memory(item) for item in items]
        with self._uow_factory() as uow:
            for memory in built:
                uow.memories.add(memory)
            uow.commit()
        logger.info("memories_batch_created", extra={"count": len(built)})
        return built

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get_memory(
        self, memory_id: UUID, *, include_deleted: bool = False, track_access: bool = True
    ) -> Memory:
        with self._uow_factory() as uow:
            memory = uow.memories.get(memory_id)
            if memory is None or not memory.is_visible(include_deleted=include_deleted):
                raise NotFoundError(f"Memory {memory_id} not found")
            if track_access:
                memory = uow.memories.record_access(memory_id)
            uow.commit()
        return memory

    def search_memories(self, criteria: SearchCriteria) -> SearchResult:
        with self._uow_factory() as uow:
            result = uow.memories.search(criteria)
            uow.commit()
        return result

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    def update_memory(
        self, memory_id: UUID, changes: MemoryUpdateData, *, expected_version: int | None = None
    ) -> Memory:
        with self._uow_factory() as uow:
            existing = uow.memories.get(memory_id)
            if existing is None or existing.status == MemoryStatus.DELETED:
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

            uow.memories.add_version(plan.version_snapshot)
            updated = uow.memories.update(memory_id, existing.version, plan.changed_fields)
            uow.commit()

        logger.info(
            "memory_updated",
            extra={"memory_id": str(memory_id), "changed_fields": plan.changed_field_names},
        )
        return updated

    def bulk_update_memories(self, items: list[BulkUpdateItem]) -> list[Memory]:
        results: list[Memory] = []
        with self._uow_factory() as uow:
            for item in items:
                existing = uow.memories.get(item.memory_id)
                if existing is None or existing.status == MemoryStatus.DELETED:
                    raise NotFoundError(f"Memory {item.memory_id} not found")
                if item.expected_version is not None and existing.version != item.expected_version:
                    raise ConflictError(f"Memory {item.memory_id} version mismatch")

                plan = self._engine.apply_update(existing, item.changes)
                if plan.is_noop:
                    results.append(existing)
                    continue

                uow.memories.add_version(plan.version_snapshot)
                updated = uow.memories.update(item.memory_id, existing.version, plan.changed_fields)
                results.append(updated)
            uow.commit()
        logger.info("memories_bulk_updated", extra={"count": len(results)})
        return results

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------
    def _transition_status(self, memory_id: UUID, target: MemoryStatus, *, change_reason: str) -> Memory:
        with self._uow_factory() as uow:
            existing = uow.memories.get(memory_id)
            if existing is None:
                raise NotFoundError(f"Memory {memory_id} not found")

            plan = self._engine.apply_status_transition(existing, target, change_reason=change_reason)
            if plan.is_noop:
                uow.commit()
                return existing

            uow.memories.add_version(plan.version_snapshot)
            updated = uow.memories.update(memory_id, existing.version, plan.changed_fields)
            uow.commit()

        logger.info(
            "memory_status_changed", extra={"memory_id": str(memory_id), "target_status": target.value}
        )
        return updated

    def archive_memory(self, memory_id: UUID) -> Memory:
        return self._transition_status(memory_id, MemoryStatus.ARCHIVED, change_reason="archived")

    def restore_memory(self, memory_id: UUID) -> Memory:
        return self._transition_status(memory_id, MemoryStatus.ACTIVE, change_reason="restored")

    def soft_delete_memory(self, memory_id: UUID) -> Memory:
        return self._transition_status(memory_id, MemoryStatus.DELETED, change_reason="soft_deleted")

    def hard_delete_memory(self, memory_id: UUID) -> None:
        with self._uow_factory() as uow:
            deleted = uow.memories.hard_delete(memory_id)
            uow.commit()
        if not deleted:
            raise NotFoundError(f"Memory {memory_id} not found")
        logger.info("memory_hard_deleted", extra={"memory_id": str(memory_id)})

    # ------------------------------------------------------------------
    # Version history
    # ------------------------------------------------------------------
    def list_versions(self, memory_id: UUID) -> list[MemoryVersion]:
        with self._uow_factory() as uow:
            if not uow.memories.exists(memory_id):
                raise NotFoundError(f"Memory {memory_id} not found")
            versions = uow.memories.list_versions(memory_id)
            uow.commit()
        return versions

    def get_version(self, memory_id: UUID, version_number: int) -> MemoryVersion:
        with self._uow_factory() as uow:
            version = uow.memories.get_version(memory_id, version_number)
            memory = None if version is not None else uow.memories.get(memory_id)
            uow.commit()

        if version is not None:
            return version
        if memory is None:
            raise NotFoundError(f"Memory {memory_id} not found")
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
        )
