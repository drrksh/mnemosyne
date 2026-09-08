"""
Repository port (interface) for memory persistence.

This is the seam between the domain/service layers and any concrete
storage technology. `SqliteMemoryRepository` is the only implementation
today; a `PostgresMemoryRepository` or a future distributed-storage
adapter could be dropped in by implementing this same contract —
nothing above this layer (services, API) would need to change. See
DECISIONS.md, "Repository + Unit of Work pattern".
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional
from uuid import UUID

from mnemosyne.domain.dto import FieldChanges, SearchCriteria, SearchResult
from mnemosyne.domain.entities import Memory, MemoryVersion


class MemoryRepository(ABC):
    """Persistence contract for memories and their version history."""

    @abstractmethod
    def add(self, memory: Memory) -> None:
        """Persist a brand-new memory. Raises ConflictError on duplicate id."""

    @abstractmethod
    def get(self, memory_id: UUID) -> Optional[Memory]:
        """Fetch a memory by id, or None if it does not exist (regardless
        of status — visibility rules belong to the service layer)."""

    @abstractmethod
    def update(self, memory_id: UUID, expected_version: int, changes: FieldChanges) -> Memory:
        """Apply `changes` to the memory, enforcing optimistic concurrency
        against `expected_version`. Raises NotFoundError / ConflictError."""

    @abstractmethod
    def record_access(self, memory_id: UUID) -> Memory:
        """Increment access_count and update last_accessed_at, returning
        the updated memory so callers do not need a second fetch to see
        the new count."""

    @abstractmethod
    def add_version(self, version: MemoryVersion) -> None:
        """Append an immutable version snapshot."""

    @abstractmethod
    def list_versions(self, memory_id: UUID) -> list[MemoryVersion]:
        """Return all superseded version snapshots for a memory, oldest first."""

    @abstractmethod
    def get_version(self, memory_id: UUID, version_number: int) -> Optional[MemoryVersion]:
        """Fetch a specific historical version snapshot, if one was recorded."""

    @abstractmethod
    def hard_delete(self, memory_id: UUID) -> bool:
        """Permanently remove a memory and its version history. Returns
        False (not an exception) if the memory did not exist."""

    @abstractmethod
    def search(self, criteria: SearchCriteria) -> SearchResult:
        """Search/list memories according to the given criteria."""

    @abstractmethod
    def exists(self, memory_id: UUID) -> bool:
        """Cheap existence check."""
