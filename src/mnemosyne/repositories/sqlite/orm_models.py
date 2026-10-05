"""
SQLAlchemy ORM models.

These are intentionally kept separate from `domain.entities` — the ORM
models describe *how rows are shaped in the database*, while the domain
entities describe *what a memory means to the rest of the system*.
`mappers.py` is the only code allowed to know about both.

Despite the package name (kept for continuity with Phase 1), nothing in
this module is SQLite-specific: every type used here is portable
SQLAlchemy, and the same models run unchanged on PostgreSQL.

Tags are normalized into their own table with a many-to-many join
table (rather than a JSON array on `memories`) so tag search can use a
real index instead of scanning/parsing JSON per row — see
DECISIONS.md, "Tag storage: normalized vs JSON array".

The slot columns are named `slot_key` / `slot_value` rather than `key`
/ `value` because both are reserved words in some SQL dialects (MySQL
reserves KEY); the domain layer still calls them `key` and `value`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mnemosyne.db.base import Base

memory_tags_table = Table(
    "memory_tags",
    Base.metadata,
    Column("memory_id", String(36), ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class TagORM(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)


class MemoryORM(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    memory_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    importance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Python attribute renamed to `extra_metadata` to avoid colliding with
    # SQLAlchemy's own `Base.metadata` class attribute; the DB column
    # itself is still named "metadata".
    extra_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_accessed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Developer-memory columns (migration 7c2f1a9b4d10) -------------
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, default="local", server_default="local")
    team_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    scope: Mapped[str] = mapped_column(String(255), nullable=False, default="global", server_default="global")
    slot_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    slot_value: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="explicit", server_default="explicit")
    reinforcement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    last_reinforced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    superseded_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    tags: Mapped[list[TagORM]] = relationship(secondary=memory_tags_table, lazy="selectin")
    versions: Mapped[list[MemoryVersionORM]] = relationship(
        back_populates="memory",
        cascade="all, delete-orphan",
        order_by="MemoryVersionORM.version_number",
        passive_deletes=True,
    )

    # SQLAlchemy's built-in optimistic-concurrency support: every UPDATE
    # includes `WHERE version = <the value we loaded>` and bumps it by 1
    # atomically; if zero rows match (because someone else already wrote
    # a newer version), SQLAlchemy raises StaleDataError for us. See
    # DECISIONS.md, "Optimistic concurrency".
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012 - SQLAlchemy declarative idiom

    __table_args__ = (
        CheckConstraint("confidence_score >= 0.0 AND confidence_score <= 1.0", name="ck_memories_confidence_range"),
        CheckConstraint("importance_score >= 0.0 AND importance_score <= 1.0", name="ck_memories_importance_range"),
        CheckConstraint("status IN ('active','archived','deleted')", name="ck_memories_status_valid"),
        CheckConstraint("reinforcement_count >= 1", name="ck_memories_reinforcement_positive"),
        Index("ix_memories_created_at", "created_at"),
        Index("ix_memories_updated_at", "updated_at"),
        Index("ix_memories_importance_score", "importance_score"),
        # Tenancy: "my personal memories" and "my teams' memories".
        Index("ix_memories_owner_team_status", "owner_id", "team_id", "status"),
        Index("ix_memories_team_status", "team_id", "status"),
        # Slot lookups during consolidation.
        Index("ix_memories_owner_scope_slot", "owner_id", "scope", "slot_key"),
        Index("ix_memories_fingerprint", "fingerprint"),
        Index("ix_memories_scope", "scope"),
    )


class MemoryVersionORM(Base):
    __tablename__ = "memory_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    memory_type: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    importance_score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    tags_snapshot: Mapped[list] = mapped_column("tags", JSON, nullable=False, default=list)
    metadata_snapshot: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    changed_fields: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    change_reason: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(255), nullable=False, default="global", server_default="global")
    slot_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    slot_value: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="explicit", server_default="explicit")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    superseded_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    memory: Mapped[MemoryORM] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint("memory_id", "version_number", name="uq_memory_versions_memory_id_version_number"),
        Index("ix_memory_versions_memory_id_version_number", "memory_id", "version_number"),
    )


class MemoryEvidenceORM(Base):
    __tablename__ = "memory_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("memory_id", "source_ref", name="uq_memory_evidence_memory_source"),
    )


class MemoryEmbeddingORM(Base):
    __tablename__ = "memory_embeddings"

    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_memory_embeddings_model", "model"),)


class UserORM(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    handle: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TeamORM(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TeamMemberORM(Base):
    __tablename__ = "team_members"

    team_id: Mapped[str] = mapped_column(String(64), ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("role IN ('member','maintainer')", name="ck_team_members_role_valid"),
        Index("ix_team_members_user_id", "user_id"),
    )


class ApiKeyORM(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEventORM(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_audit_events_actor_occurred", "actor_id", "occurred_at"),
        Index("ix_audit_events_target", "target_type", "target_id"),
        Index("ix_audit_events_occurred_at", "occurred_at"),
    )
