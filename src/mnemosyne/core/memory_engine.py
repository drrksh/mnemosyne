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
import re
from dataclasses import dataclass
from typing import Any, Optional
from uuid import uuid4

from mnemosyne.core.text import fingerprint as compute_fingerprint
from mnemosyne.domain.dto import FieldChanges, MemoryUpdateData, NewMemoryData
from mnemosyne.domain.entities import Memory, MemoryVersion
from mnemosyne.domain.enums import ALLOWED_STATUS_TRANSITIONS, MemoryOrigin, MemoryStatus, MemoryType
from mnemosyne.domain.exceptions import ValidationError
from mnemosyne.domain.scope import canonical_scope
from mnemosyne.domain.value_objects import UNSET, UnitScore
from mnemosyne.utils.time import utcnow

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:+#-]{0,199}$")

#: Fields the learning pipeline may change through `plan_changes` in
#: addition to the user-editable ones. Anything else is rejected, so a
#: bug upstream can never rewrite ownership or identity columns.
_SYSTEM_MUTABLE_FIELDS = frozenset(
    {
        "title", "summary", "content", "memory_type", "confidence_score", "importance_score", "tags",
        "source", "metadata", "notes", "scope", "key", "value", "pinned", "origin",
        "reinforcement_count", "last_reinforced_at", "superseded_by", "status", "deleted_at",
    }
)

logger = logging.getLogger("mnemosyne.core.engine")


@dataclass(frozen=True)
class EngineLimits:
    max_title_length: int = 500
    max_summary_length: int = 2000
    max_tags_per_memory: int = 20
    max_tag_length: int = 50
    max_metadata_bytes: int = 65536
    max_content_length: int = 50000
    max_value_length: int = 500


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
    #: Columns derived from the changed fields (e.g. the content
    #: fingerprint). Persisted alongside `changed_fields` but never
    #: reported as a change in their own right.
    derived_changes: Optional[FieldChanges] = None

    @property
    def persist_changes(self) -> FieldChanges:
        """Everything the repository must write for this plan."""
        return {**self.changed_fields, **(self.derived_changes or {})}


class MemoryEngine:
    """Domain rule enforcement for memory lifecycle operations."""

    def __init__(self, limits: Optional[EngineLimits] = None) -> None:
        self._limits = limits or EngineLimits()

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def build_new_memory(self, data: NewMemoryData, *, owner_id: str = "local") -> Memory:
        title = self._validate_title(data.title)
        summary = self._validate_summary(data.summary)
        tags = self._validate_tags(data.tags)
        confidence = UnitScore.confidence(data.confidence_score).value
        importance = UnitScore.importance(data.importance_score).value
        memory_type = self._validate_memory_type(data.memory_type)
        metadata = self._sanitize_metadata(data.metadata)
        content = self._validate_content(data.content)
        scope = self._validate_scope(data.scope)
        key = self._validate_key(data.key)
        value = self._validate_value(data.value)
        origin = self._validate_origin(data.origin)
        now = utcnow()

        return Memory(
            id=uuid4(),
            title=title,
            summary=summary,
            content=content,
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
            owner_id=owner_id,
            team_id=data.team_id,
            scope=scope,
            key=key,
            value=value,
            fingerprint=compute_fingerprint(title, content),
            origin=origin,
            reinforcement_count=1,
            last_reinforced_at=now,
            pinned=bool(data.pinned),
            superseded_by=None,
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
            candidate["content"] = self._validate_content(changes.content)
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
        if changes.scope is not UNSET:
            candidate["scope"] = self._validate_scope(changes.scope)
        if changes.key is not UNSET:
            candidate["key"] = self._validate_key(changes.key)
        if changes.value is not UNSET:
            candidate["value"] = self._validate_value(changes.value)
        if changes.pinned is not UNSET:
            candidate["pinned"] = bool(changes.pinned)

        changed_fields: FieldChanges = {
            key: value for key, value in candidate.items() if getattr(existing, key) != value
        }
        derived = self._derived_changes(existing, changed_fields)
        names = sorted(changed_fields.keys())

        snapshot = self._snapshot(
            existing,
            changed_fields=names,
            change_reason=changes.change_reason or "update",
        )

        return UpdatePlan(
            changed_fields=changed_fields,
            changed_field_names=names,
            version_snapshot=snapshot,
            is_noop=not changed_fields,
            derived_changes=derived,
        )

    # ------------------------------------------------------------------
    # System-driven changes (learning pipeline, feedback)
    # ------------------------------------------------------------------
    def plan_changes(self, existing: Memory, changes: FieldChanges, *, change_reason: str) -> UpdatePlan:
        """Validate and plan an arbitrary set of field changes produced by
        Mnemosyne itself (reinforcement, refresh, supersession, feedback).

        Unlike `apply_update` this accepts internal fields such as
        `reinforcement_count` and `superseded_by`, but it still runs every
        user-visible field through the same validators, and it refuses
        identity/ownership fields outright."""
        unknown = set(changes) - _SYSTEM_MUTABLE_FIELDS
        if unknown:
            raise ValidationError(f"fields not mutable by the system: {sorted(unknown)}")

        validated: FieldChanges = {}
        for key, value in changes.items():
            if key == "title":
                value = self._validate_title(value)
            elif key == "summary":
                value = self._validate_summary(value)
            elif key == "content":
                value = self._validate_content(value)
            elif key == "memory_type":
                value = self._validate_memory_type(value)
            elif key == "confidence_score":
                value = UnitScore.confidence(value).value
            elif key == "importance_score":
                value = UnitScore.importance(value).value
            elif key == "tags":
                value = self._validate_tags(value)
            elif key == "metadata":
                value = self._sanitize_metadata(value)
            elif key == "scope":
                value = self._validate_scope(value)
            elif key == "key":
                value = self._validate_key(value)
            elif key == "value":
                value = self._validate_value(value)
            elif key == "origin":
                value = self._validate_origin(value)
            elif key == "status":
                value = MemoryStatus(value)
                self.validate_status_transition(existing.status, value)
            elif key == "reinforcement_count":
                value = max(1, int(value))
            validated[key] = value

        changed_fields: FieldChanges = {
            key: value for key, value in validated.items() if getattr(existing, key) != value
        }
        if changed_fields.get("status") == MemoryStatus.DELETED and "deleted_at" not in changed_fields:
            changed_fields["deleted_at"] = utcnow()
        derived = self._derived_changes(existing, changed_fields)

        names = sorted(changed_fields.keys())
        return UpdatePlan(
            changed_fields=changed_fields,
            changed_field_names=names,
            version_snapshot=self._snapshot(existing, changed_fields=names, change_reason=change_reason),
            is_noop=not changed_fields,
            derived_changes=derived,
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

    def _validate_content(self, content: Any) -> str:
        content = content or ""
        if not isinstance(content, str):
            raise ValidationError("content must be a string")
        if len(content) > self._limits.max_content_length:
            raise ValidationError(
                f"content exceeds max length of {self._limits.max_content_length} characters"
            )
        return content

    def _validate_scope(self, scope: Any) -> str:
        return canonical_scope(scope)

    def _validate_key(self, key: Any) -> Optional[str]:
        if key is None:
            return None
        normalized = str(key).strip().lower()
        if not normalized:
            return None
        if not _KEY_RE.match(normalized):
            raise ValidationError(
                f"invalid key {key!r}: use lowercase dotted identifiers like 'python.test_framework'"
            )
        return normalized

    def _validate_value(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if len(text) > self._limits.max_value_length:
            raise ValidationError(f"value exceeds max length of {self._limits.max_value_length} characters")
        return text

    def _validate_origin(self, origin: Any) -> MemoryOrigin:
        if isinstance(origin, MemoryOrigin):
            return origin
        try:
            return MemoryOrigin(origin)
        except ValueError as exc:
            raise ValidationError(f"unknown origin: {origin!r}") from exc

    def _derived_changes(self, existing: Memory, changed_fields: FieldChanges) -> FieldChanges:
        derived: FieldChanges = {}
        if "title" in changed_fields or "content" in changed_fields:
            title = changed_fields.get("title", existing.title)
            content = changed_fields.get("content", existing.content)
            new_fp = compute_fingerprint(title, content)
            if new_fp != existing.fingerprint:
                derived["fingerprint"] = new_fp
        return derived

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
            scope=memory.scope,
            key=memory.key,
            value=memory.value,
            origin=memory.origin,
            pinned=memory.pinned,
            superseded_by=memory.superseded_by,
        )
