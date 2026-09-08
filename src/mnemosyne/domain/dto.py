"""
Data-transfer objects that cross the boundary between callers (API
routers today; a future CLI or embedded caller tomorrow) and the
MemoryEngine / MemoryService.

`NewMemoryData` is a plain "everything required or defaulted" bag.
`MemoryUpdateData` uses the UNSET sentinel (see value_objects.py) so
PATCH semantics are unambiguous. `SearchCriteria` / `SearchResult` are
the contract that both today's SQL-based search and a future
vector/semantic search implementation must honor identically — see
`repositories.interfaces.MemoryRepository.search`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from mnemosyne.domain.entities import Memory
from mnemosyne.domain.enums import MemoryStatus, MemoryType
from mnemosyne.domain.value_objects import UNSET

#: Sparse map of {field_name: new_value} for fields that actually changed.
#: Intentionally a plain dict rather than a dataclass: it is produced and
#: consumed entirely within the engine/repository boundary and is never
#: serialized across a process boundary, so the flexibility of a dict
#: outweighs the benefit of static field names here.
FieldChanges = dict[str, Any]


@dataclass(slots=True)
class NewMemoryData:
    title: str
    summary: str = ""
    content: str = ""
    memory_type: MemoryType = MemoryType.UNKNOWN
    confidence_score: float = 1.0
    importance_score: float = 0.5
    tags: list[str] = field(default_factory=list)
    source: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    notes: Optional[str] = None


@dataclass(slots=True)
class MemoryUpdateData:
    title: Any = UNSET
    summary: Any = UNSET
    content: Any = UNSET
    memory_type: Any = UNSET
    confidence_score: Any = UNSET
    importance_score: Any = UNSET
    tags: Any = UNSET
    source: Any = UNSET
    metadata: Any = UNSET
    notes: Any = UNSET
    change_reason: Optional[str] = None


@dataclass(slots=True)
class BulkUpdateItem:
    memory_id: UUID
    changes: MemoryUpdateData
    expected_version: Optional[int] = None


@dataclass(slots=True)
class SearchCriteria:
    query: Optional[str] = None
    memory_types: Optional[list[MemoryType]] = None
    tags: Optional[list[str]] = None
    match_all_tags: bool = False
    statuses: Optional[list[MemoryStatus]] = None
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    updated_after: Optional[datetime] = None
    updated_before: Optional[datetime] = None
    min_confidence: Optional[float] = None
    max_confidence: Optional[float] = None
    min_importance: Optional[float] = None
    max_importance: Optional[float] = None
    sort_by: str = "updated_at"
    sort_desc: bool = True
    page: int = 1
    page_size: int = 20


@dataclass(slots=True)
class SearchResult:
    items: list[Memory]
    total: int
    page: int
    page_size: int

    @property
    def has_next(self) -> bool:
        return self.page * self.page_size < self.total

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size
