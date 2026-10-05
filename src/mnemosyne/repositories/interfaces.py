"""
Repository ports (interfaces) for persistence.

This is the seam between the domain/service layers and any concrete
storage technology. The SQLAlchemy implementations in `sqlite/` are the
only adapters today (and run on PostgreSQL as well as SQLite); a
distributed-storage adapter could be dropped in by implementing these
same contracts — nothing above this layer (services, API) would need to
change. See DECISIONS.md, "Repository + Unit of Work pattern".
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence
from uuid import UUID

from mnemosyne.domain.dto import FieldChanges, SearchCriteria, SearchResult
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
from mnemosyne.domain.enums import MemoryStatus, TeamRole
from mnemosyne.domain.principal import Visibility


@dataclass(frozen=True)
class IndexSignature:
    """Cheap fingerprint of "the set of active memories visible to X".
    Any create, update, status change or delete changes at least one
    component, so a retrieval index built against one signature can be
    reused for as long as the signature is unchanged — including across
    processes, since it is computed from the database."""

    count: int
    max_updated_at: Optional[datetime]
    version_sum: int


class MemoryRepository(ABC):
    """Persistence contract for memories and their version history."""

    @abstractmethod
    def add(self, memory: Memory) -> None:
        """Persist a brand-new memory. Raises ConflictError on duplicate id."""

    @abstractmethod
    def get(self, memory_id: UUID) -> Optional[Memory]:
        """Fetch a memory by id, or None if it does not exist (regardless
        of status or tenancy — visibility rules belong to the service layer)."""

    @abstractmethod
    def get_many(self, memory_ids: Sequence[UUID]) -> list[Memory]:
        """Fetch several memories by id (missing ids are skipped)."""

    @abstractmethod
    def update(self, memory_id: UUID, expected_version: int, changes: FieldChanges) -> Memory:
        """Apply `changes` to the memory, enforcing optimistic concurrency
        against `expected_version`. Raises NotFoundError / ConflictError."""

    @abstractmethod
    def update_dynamics(self, memory_id: UUID, changes: FieldChanges) -> Memory:
        """Update belief-strength fields only (confidence, importance,
        reinforcement count, last reinforced). These record how strongly a
        memory is held, not what it says, so — like access tracking — they
        do not create a new content version. Evidence rows document every
        change. Rejects any other field."""

    @abstractmethod
    def record_access(self, memory_id: UUID) -> Memory:
        """Increment access_count and update last_accessed_at, returning
        the updated memory. Access tracking is *not* a content change: it
        must never bump the memory's version."""

    @abstractmethod
    def record_access_many(self, memory_ids: Sequence[UUID]) -> None:
        """Bulk form of `record_access` used when compiling context."""

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
    def hard_delete_personal(self, owner_id: str) -> int:
        """Permanently remove every *personal* memory of `owner_id`
        (right to erasure). Team memories are left in place. Returns the
        number of memories removed."""

    @abstractmethod
    def search(self, criteria: SearchCriteria) -> SearchResult:
        """Search/list memories according to the given criteria."""

    @abstractmethod
    def exists(self, memory_id: UUID) -> bool:
        """Cheap existence check."""

    @abstractmethod
    def list_for_index(
        self, visibility: Visibility, statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,)
    ) -> list[Memory]:
        """Every memory visible under `visibility` with one of `statuses`,
        loaded as efficiently as the backend allows (retrieval hot path)."""

    @abstractmethod
    def index_signature(self, visibility: Visibility) -> IndexSignature:
        """See `IndexSignature`."""

    @abstractmethod
    def find_slot(
        self, *, owner_id: str, team_id: Optional[str], scope: str, key: str
    ) -> list[Memory]:
        """Active memories occupying the slot (owner/team, scope, key)."""

    @abstractmethod
    def find_by_fingerprint(
        self, *, owner_id: str, team_id: Optional[str], scope: str, fingerprint: str
    ) -> list[Memory]:
        """Active memories in the given owner/team and scope with this
        content fingerprint."""


class EvidenceRepository(ABC):
    @abstractmethod
    def add(self, evidence: Evidence) -> bool:
        """Attach evidence. Returns False (and stores nothing) when the
        memory already holds evidence with the same `source_ref`."""

    @abstractmethod
    def list_for_memory(self, memory_id: UUID, *, limit: int = 100) -> list[Evidence]:
        """Newest first."""

    @abstractmethod
    def existing_refs(self, memory_id: UUID, refs: Iterable[str]) -> set[str]:
        """Which of `refs` are already recorded against the memory."""

    @abstractmethod
    def trim(self, memory_id: UUID, keep: int) -> int:
        """Delete all but the newest `keep` evidence rows; returns how many
        were removed."""

    @abstractmethod
    def count_for_memory(self, memory_id: UUID) -> int:
        ...


@dataclass(frozen=True)
class StoredEmbedding:
    memory_id: UUID
    model: str
    dim: int
    vector: bytes
    text_hash: str


class EmbeddingRepository(ABC):
    @abstractmethod
    def upsert(self, embedding: StoredEmbedding) -> None:
        ...

    @abstractmethod
    def upsert_many(self, embeddings: Sequence[StoredEmbedding]) -> None:
        """Bulk form of `upsert` (one memory may appear at most once)."""

    @abstractmethod
    def get_many(self, memory_ids: Sequence[UUID], *, model: str) -> dict[UUID, StoredEmbedding]:
        ...

    @abstractmethod
    def text_hashes(self, *, model: str) -> dict[UUID, str]:
        """memory_id -> text hash for every stored embedding of `model`
        (used to find embeddings that are missing or stale)."""

    @abstractmethod
    def delete_other_models(self, *, keep_model: str) -> int:
        ...


class IdentityRepository(ABC):
    @abstractmethod
    def add_user(self, user: User) -> None: ...

    @abstractmethod
    def get_user(self, user_id: str) -> Optional[User]: ...

    @abstractmethod
    def get_user_by_handle(self, handle: str) -> Optional[User]: ...

    @abstractmethod
    def list_users(self) -> list[User]: ...

    @abstractmethod
    def set_user_disabled(self, user_id: str, disabled: bool) -> None: ...

    @abstractmethod
    def add_team(self, team: Team) -> None: ...

    @abstractmethod
    def get_team(self, team_id: str) -> Optional[Team]: ...

    @abstractmethod
    def get_team_by_name(self, name: str) -> Optional[Team]: ...

    @abstractmethod
    def list_teams(self) -> list[Team]: ...

    @abstractmethod
    def upsert_membership(self, membership: TeamMembership) -> None: ...

    @abstractmethod
    def remove_membership(self, team_id: str, user_id: str) -> bool: ...

    @abstractmethod
    def memberships_for_user(self, user_id: str) -> list[TeamMembership]: ...

    @abstractmethod
    def members_of_team(self, team_id: str) -> list[TeamMembership]: ...

    @abstractmethod
    def add_api_key(self, key: ApiKey) -> None: ...

    @abstractmethod
    def get_api_key_by_prefix(self, prefix: str) -> Optional[ApiKey]: ...

    @abstractmethod
    def get_api_key(self, key_id: str) -> Optional[ApiKey]: ...

    @abstractmethod
    def list_api_keys(self, user_id: str) -> list[ApiKey]: ...

    @abstractmethod
    def touch_api_key(self, key_id: str, when: datetime) -> None: ...

    @abstractmethod
    def revoke_api_key(self, key_id: str, when: datetime) -> bool: ...


class AuditRepository(ABC):
    @abstractmethod
    def add(self, event: AuditEvent) -> None: ...

    @abstractmethod
    def list(
        self,
        *,
        actor_id: Optional[str] = None,
        action_prefix: Optional[str] = None,
        target_id: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Newest first."""


__all__ = [
    "AuditRepository",
    "EmbeddingRepository",
    "EvidenceRepository",
    "IdentityRepository",
    "IndexSignature",
    "MemoryRepository",
    "StoredEmbedding",
    "TeamRole",
]
