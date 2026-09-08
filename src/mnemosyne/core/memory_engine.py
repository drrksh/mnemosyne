"""
MemoryEngine: pure business rules for constructing and mutating
memories. No I/O, no SQLAlchemy, no FastAPI — this class can be
unit-tested with nothing but Python objects, which is exactly the
point: the rules that decide whether a memory is *valid* should never
require a database connection to verify.

Configuration (limits, defaults) is received through `EngineLimits` at
construction time rather than importing `Settings` directly — that
keeps this layer decoupled from *how* configuration is sourced (env
vars today, remote config later).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional
from uuid import uuid4

from mnemosyne.domain.dto import FieldChanges, MemoryUpdateData, NewMemoryData
from mnemosyne.domain.entities import Memory, MemoryVersion
from mnemosyne.domain.enums import ALLOWED_STATUS_TRANSITIONS, MemoryStatus, MemoryType
from mnemosyne.domain.exceptions import ValidationError
from mnemosyne.domain.value_objects import UNSET, UnitScore
from mnemosyne.utils.time import utcnow

logger = logging.getLogger("mnemosyne.core.engine")


@dataclass(frozen=True)
class EngineLimits:
    max_title_length: int = 500
    max_summary_length: int = 2000
    max_tags_per_memory: int = 20
    max_tag_length: int = 50
    max_metadata_bytes: int = 65536


@dataclass(frozen=True)
class UpdatePlan:
    """Everything the service layer needs to persist an update or a
    status transition: which fields actually changed (and their new
    values), the version snapshot to archive, and whether this update
    is a no-op (in which case the service should skip touching the
    database entirely rather than generate a spurious version row)."""

    changed_fields: FieldChanges
    changed_field_names: list[str]
    version_snapshot: MemoryVersion
    is_noop: bool


class MemoryEngine:
    """Domain rule enforcement for memory lifecycle operations."""

    def __init__(self, limits: Optional[EngineLimits] = None) -> None:
        self._limits = limits or EngineLimits()

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def build_new_memory(self, data: NewMemoryData) -> Memory:
        title = self._validate_title(data.title)
        summary = self._validate_summary(data.summary)
        tags = self._validate_tags(data.tags)
        confidence = UnitScore.confidence(data.confidence_score).value
        importance = UnitScore.importance(data.importance_score).value
        memory_type = self._validate_memory_type(data.memory_type)
        metadata = self._sanitize_metadata(data.metadata)
        now = utcnow()

        return Memory(
            id=uuid4(),
            title=title,
            summary=summary,
            content=data.content or "",
            memory_type=memory_type,
            confidence_score=confidence,
            importance_score=importance,
            tags=tags,
            status=MemoryStatus.ACTIVE,
            version=1,
            source=data.source,
            metadata=metadata,
            notes=data.notes,
            created_at=now,
            updated_at=now,
            access_count=0,
            last_accessed_at=None,
            deleted_at=None,
        )

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    def apply_update(self, existing: Memory, changes: MemoryUpdateData) -> UpdatePlan:
        candidate: FieldChanges = {}

        if changes.title is not UNSET:
            candidate["title"] = self._validate_title(changes.title)
        if changes.summary is not UNSET:
            candidate["summary"] = self._validate_summary(changes.summary)
        if changes.content is not UNSET:
            candidate["content"] = changes.content or ""
        if changes.memory_type is not UNSET:
            candidate["memory_type"] = self._validate_memory_type(changes.memory_type)
        if changes.confidence_score is not UNSET:
            candidate["confidence_score"] = UnitScore.confidence(changes.confidence_score).value
        if changes.importance_score is not UNSET:
            candidate["importance_score"] = UnitScore.importance(changes.importance_score).value
        if changes.tags is not UNSET:
            candidate["tags"] = self._validate_tags(changes.tags)
        if changes.source is not UNSET:
            candidate["source"] = changes.source
        if changes.metadata is not UNSET:
            candidate["metadata"] = self._sanitize_metadata(changes.metadata)
        if changes.notes is not UNSET:
            candidate["notes"] = changes.notes

        changed_fields: FieldChanges = {
            key: value for key, value in candidate.items() if getattr(existing, key) != value
        }

        snapshot = self._snapshot(
            existing,
            changed_fields=sorted(changed_fields.keys()),
            change_reason=changes.change_reason or "update",
        )

        return UpdatePlan(
            changed_fields=changed_fields,
            changed_field_names=sorted(changed_fields.keys()),
            version_snapshot=snapshot,
            is_noop=not changed_fields,
        )

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------
    def validate_status_transition(self, current: MemoryStatus, target: MemoryStatus) -> None:
        if current == target:
            return
        allowed = ALLOWED_STATUS_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise ValidationError(
                f"Cannot transition memory from '{current.value}' to '{target.value}'"
            )

    def apply_status_transition(
        self, existing: Memory, target: MemoryStatus, *, change_reason: str
    ) -> UpdatePlan:
        self.validate_status_transition(existing.status, target)

        if existing.status == target:
            snapshot = self._snapshot(existing, changed_fields=[], change_reason=change_reason)
            return UpdatePlan(
                changed_fields={}, changed_field_names=[], version_snapshot=snapshot, is_noop=True
            )

        changed_fields: FieldChanges = {"status": target}
        if target == MemoryStatus.DELETED:
            changed_fields["deleted_at"] = utcnow()
        elif existing.status == MemoryStatus.DELETED:
            changed_fields["deleted_at"] = None

        snapshot = self._snapshot(existing, changed_fields=["status"], change_reason=change_reason)
        return UpdatePlan(
            changed_fields=changed_fields,
            changed_field_names=["status"],
            version_snapshot=snapshot,
            is_noop=False,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _validate_title(self, title: Any) -> str:
        title = (title or "").strip()
        if not title:
            raise ValidationError("title must not be empty")
        if len(title) > self._limits.max_title_length:
            raise ValidationError(
                f"title exceeds max length of {self._limits.max_title_length} characters"
            )
        return title

    def _validate_summary(self, summary: Any) -> str:
        summary = summary or ""
        if len(summary) > self._limits.max_summary_length:
            raise ValidationError(
                f"summary exceeds max length of {self._limits.max_summary_length} characters"
            )
        return summary

    def _validate_memory_type(self, memory_type: Any) -> MemoryType:
        if isinstance(memory_type, MemoryType):
            return memory_type
        try:
            return MemoryType(memory_type)
        except ValueError as exc:
            raise ValidationError(f"unknown memory_type: {memory_type!r}") from exc

    def _validate_tags(self, tags: Any) -> list[str]:
        tags = tags or []
        if len(tags) > self._limits.max_tags_per_memory:
            raise ValidationError(
                f"too many tags: max {self._limits.max_tags_per_memory} per memory"
            )
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in tags:
            tag = (raw or "").strip().lower()
            if not tag:
                continue
            if len(tag) > self._limits.max_tag_length:
                raise ValidationError(
                    f"tag '{tag}' exceeds max length of {self._limits.max_tag_length} characters"
                )
            if tag not in seen:
                seen.add(tag)
                normalized.append(tag)
        return normalized

    def _sanitize_metadata(self, metadata: Any) -> dict[str, Any]:
        """Best-effort metadata acceptance: malformed or oversized metadata
        is dropped with a warning rather than failing the whole operation
        — a memory's core content is more valuable than its metadata, so
        we degrade gracefully instead of rejecting the entire write."""
        if not metadata:
            return {}
        try:
            encoded = json.dumps(metadata)
        except (TypeError, ValueError):
            logger.warning("metadata_not_serializable_dropped")
            return {}
        if len(encoded.encode("utf-8")) > self._limits.max_metadata_bytes:
            logger.warning("metadata_too_large_dropped", extra={"size_bytes": len(encoded)})
            return {}
        return metadata

    def _snapshot(
        self, memory: Memory, *, changed_fields: list[str], change_reason: Optional[str]
    ) -> MemoryVersion:
        return MemoryVersion(
            id=uuid4(),
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
            changed_fields=changed_fields,
            change_reason=change_reason,
            recorded_at=utcnow(),
        )
