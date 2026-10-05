"""
Core domain entities.

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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from mnemosyne.domain.enums import (
    EvidenceKind,
    MemoryOrigin,
    MemoryStatus,
    MemoryType,
    TeamRole,
)


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

    # --- Ownership -----------------------------------------------------
    #: The user who owns (authored) this memory.
    owner_id: str = "local"
    #: When set, this is a *team* memory: visible to every member of the
    #: team, not just the owner.
    team_id: Optional[str] = None

    # --- Applicability & identity --------------------------------------
    #: Canonical scope string — see `domain.scope`.
    scope: str = "global"
    #: Optional slot identifier ("python.test_framework"). Active
    #: memories sharing (owner, team, scope, key) are mutually exclusive:
    #: a new value supersedes the old one instead of coexisting with it.
    key: Optional[str] = None
    #: The slot's value ("pytest"); compared normalized to detect
    #: reinforcement (same value) versus contradiction (different value).
    value: Optional[str] = None
    #: Normalized content hash used for exact-duplicate detection.
    fingerprint: str = ""

    # --- Learning dynamics ---------------------------------------------
    origin: MemoryOrigin = MemoryOrigin.EXPLICIT
    reinforcement_count: int = 1
    last_reinforced_at: Optional[datetime] = None
    #: Always included in compiled context when its scope applies.
    pinned: bool = False
    #: Set when a newer memory replaced this one (this one is archived).
    superseded_by: Optional[UUID] = None

    def is_visible(self, *, include_deleted: bool = False) -> bool:
        return include_deleted or self.status != MemoryStatus.DELETED

    @property
    def is_team_memory(self) -> bool:
        return self.team_id is not None

    @property
    def last_signal_at(self) -> datetime:
        """Most recent moment this memory was confirmed useful or true."""
        candidates = [self.updated_at]
        if self.last_reinforced_at is not None:
            candidates.append(self.last_reinforced_at)
        if self.last_accessed_at is not None:
            candidates.append(self.last_accessed_at)
        return max(candidates)


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
    scope: str = "global"
    key: Optional[str] = None
    value: Optional[str] = None
    origin: MemoryOrigin = MemoryOrigin.EXPLICIT
    pinned: bool = False
    superseded_by: Optional[UUID] = None


@dataclass(slots=True)
class Evidence:
    """One observation supporting a memory: the *why* behind a belief.

    `source_ref` identifies where the observation came from
    (`session:<id>#<hash>`, `repo:<id>@<sha>`, ...). It is unique per
    memory, which is what makes ingestion idempotent: replaying the same
    transcript or re-analysing the same commit never double-counts.
    """

    id: UUID
    memory_id: UUID
    kind: EvidenceKind
    source_ref: str
    excerpt: str
    weight: float
    observed_at: datetime


@dataclass(slots=True)
class User:
    id: str
    handle: str
    display_name: str
    is_admin: bool
    created_at: datetime
    disabled: bool = False


@dataclass(slots=True)
class Team:
    id: str
    name: str
    created_at: datetime


@dataclass(slots=True)
class TeamMembership:
    team_id: str
    user_id: str
    role: TeamRole
    joined_at: datetime


@dataclass(slots=True)
class ApiKey:
    id: str
    user_id: str
    name: str
    prefix: str
    key_hash: str
    scopes: list[str]
    created_at: datetime
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None

    def is_usable(self, now: datetime) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > now


@dataclass(slots=True)
class AuditEvent:
    id: UUID
    occurred_at: datetime
    actor_id: str
    action: str
    target_type: str
    target_id: Optional[str]
    request_id: Optional[str]
    details: dict[str, Any] = field(default_factory=dict)
