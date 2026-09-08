"""
Core domain entities: Memory and MemoryVersion.

These are deliberately plain dataclasses with no SQLAlchemy, no
Pydantic, and no FastAPI in sight. The rest of the system (ORM models,
API schemas) maps INTO and OUT OF these — the entities themselves never
know that a database or an HTTP framework exists. That is what lets the
domain/core layers be unit-tested with zero infrastructure.

`slots=True` is used deliberately for memory efficiency and to catch
attribute-name typos early; it does mean these objects have no
`__dict__`, so code that needs a dict view should use
`dataclasses.asdict()`/`dataclasses.fields()` rather than `.__dict__`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from mnemosyne.domain.enums import MemoryStatus, MemoryType


@dataclass(slots=True)
class Memory:
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
    deleted_at: Optional[datetime] = None

    def is_visible(self, *, include_deleted: bool = False) -> bool:
        return include_deleted or self.status != MemoryStatus.DELETED


@dataclass(slots=True)
class MemoryVersion:
    """An immutable, historical snapshot of a memory as it looked *before*
    the update identified by `changed_fields` was applied. The live,
    current state of a memory always lives in the `memories` table /
    `Memory` entity — this table only ever holds superseded states."""

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
