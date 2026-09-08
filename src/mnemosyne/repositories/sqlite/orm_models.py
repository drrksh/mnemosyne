"""
SQLAlchemy ORM models for the SQLite-backed memory repository.

These are intentionally kept separate from `domain.entities` — the ORM
models describe *how rows are shaped in this particular database*,
while the domain entities describe *what a memory means to the rest of
the system*. `mappers.py` is the only code allowed to know about both.

Tags are normalized into their own table with a many-to-many join
table (rather than a JSON array on `memories`) so tag search can use a
real index instead of scanning/parsing JSON per row — see
DECISIONS.md, "Tag storage: normalized vs JSON array".
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
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

    tags: Mapped[list[TagORM]] = relationship(secondary=memory_tags_table, lazy="selectin")
    versions: Mapped[list["MemoryVersionORM"]] = relationship(
        back_populates="memory",
        cascade="all, delete-orphan",
        order_by="MemoryVersionORM.version_number",
    )

    # SQLAlchemy's built-in optimistic-concurrency support: every UPDATE
    # includes `WHERE version = <the value we loaded>` and bumps it by 1
    # atomically; if zero rows match (because someone else already wrote
    # a newer version), SQLAlchemy raises StaleDataError for us. See
    # DECISIONS.md, "Optimistic concurrency".
    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (
        CheckConstraint("confidence_score >= 0.0 AND confidence_score <= 1.0", name="ck_memories_confidence_range"),
        CheckConstraint("importance_score >= 0.0 AND importance_score <= 1.0", name="ck_memories_importance_range"),
        CheckConstraint("status IN ('active','archived','deleted')", name="ck_memories_status_valid"),
        Index("ix_memories_created_at", "created_at"),
        Index("ix_memories_updated_at", "updated_at"),
        Index("ix_memories_importance_score", "importance_score"),
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

    memory: Mapped["MemoryORM"] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint("memory_id", "version_number", name="uq_memory_versions_memory_id_version_number"),
        Index("ix_memory_versions_memory_id_version_number", "memory_id", "version_number"),
    )
