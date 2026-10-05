"""developer memory: tenancy, scope, slots, learning dynamics, evidence,
embeddings, identity, audit

Revision ID: 7c2f1a9b4d10
Revises: be06f3efd9d0
Create Date: 2026-10-05 09:00:00.000000

Every new column on an existing table carries a server default, so the
migration upgrades a populated Phase 1 database in place: existing
memories become personal, globally-scoped, explicit memories owned by
the local user, which is exactly what they were.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7c2f1a9b4d10"
down_revision: Union[str, Sequence[str], None] = "be06f3efd9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.String(length=64), nullable=False, server_default="local"))
        batch_op.add_column(sa.Column("team_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("scope", sa.String(length=255), nullable=False, server_default="global"))
        batch_op.add_column(sa.Column("slot_key", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("slot_value", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("fingerprint", sa.String(length=64), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("origin", sa.String(length=20), nullable=False, server_default="explicit"))
        batch_op.add_column(
            sa.Column("reinforcement_count", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(sa.Column("last_reinforced_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("superseded_by", sa.String(length=36), nullable=True))
        batch_op.create_check_constraint("ck_memories_reinforcement_positive", "reinforcement_count >= 1")
        batch_op.create_index("ix_memories_owner_team_status", ["owner_id", "team_id", "status"], unique=False)
        batch_op.create_index("ix_memories_team_status", ["team_id", "status"], unique=False)
        batch_op.create_index("ix_memories_owner_scope_slot", ["owner_id", "scope", "slot_key"], unique=False)
        batch_op.create_index("ix_memories_fingerprint", ["fingerprint"], unique=False)
        batch_op.create_index("ix_memories_scope", ["scope"], unique=False)

    with op.batch_alter_table("memory_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scope", sa.String(length=255), nullable=False, server_default="global"))
        batch_op.add_column(sa.Column("slot_key", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("slot_value", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("origin", sa.String(length=20), nullable=False, server_default="explicit"))
        batch_op.add_column(sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("superseded_by", sa.String(length=36), nullable=True))

    op.create_table(
        "memory_evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("memory_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("memory_id", "source_ref", name="uq_memory_evidence_memory_source"),
    )
    with op.batch_alter_table("memory_evidence", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_memory_evidence_memory_id"), ["memory_id"], unique=False)

    op.create_table(
        "memory_embeddings",
        sa.Column("memory_id", sa.String(length=36), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("text_hash", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("memory_id"),
    )
    with op.batch_alter_table("memory_embeddings", schema=None) as batch_op:
        batch_op.create_index("ix_memory_embeddings_model", ["model"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("handle", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("handle"),
    )

    op.create_table(
        "teams",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "team_members",
        sa.Column("team_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('member','maintainer')", name="ck_team_members_role_valid"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("team_id", "user_id"),
    )
    with op.batch_alter_table("team_members", schema=None) as batch_op:
        batch_op.create_index("ix_team_members_user_id", ["user_id"], unique=False)

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prefix"),
    )
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_api_keys_user_id"), ["user_id"], unique=False)

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.create_index("ix_audit_events_actor_occurred", ["actor_id", "occurred_at"], unique=False)
        batch_op.create_index("ix_audit_events_target", ["target_type", "target_id"], unique=False)
        batch_op.create_index("ix_audit_events_occurred_at", ["occurred_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_events_occurred_at")
        batch_op.drop_index("ix_audit_events_target")
        batch_op.drop_index("ix_audit_events_actor_occurred")
    op.drop_table("audit_events")

    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_keys_user_id"))
    op.drop_table("api_keys")

    with op.batch_alter_table("team_members", schema=None) as batch_op:
        batch_op.drop_index("ix_team_members_user_id")
    op.drop_table("team_members")
    op.drop_table("teams")
    op.drop_table("users")

    with op.batch_alter_table("memory_embeddings", schema=None) as batch_op:
        batch_op.drop_index("ix_memory_embeddings_model")
    op.drop_table("memory_embeddings")

    with op.batch_alter_table("memory_evidence", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_memory_evidence_memory_id"))
    op.drop_table("memory_evidence")

    with op.batch_alter_table("memory_versions", schema=None) as batch_op:
        batch_op.drop_column("superseded_by")
        batch_op.drop_column("pinned")
        batch_op.drop_column("origin")
        batch_op.drop_column("slot_value")
        batch_op.drop_column("slot_key")
        batch_op.drop_column("scope")

    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.drop_index("ix_memories_scope")
        batch_op.drop_index("ix_memories_fingerprint")
        batch_op.drop_index("ix_memories_owner_scope_slot")
        batch_op.drop_index("ix_memories_team_status")
        batch_op.drop_index("ix_memories_owner_team_status")
        batch_op.drop_constraint("ck_memories_reinforcement_positive", type_="check")
        batch_op.drop_column("superseded_by")
        batch_op.drop_column("pinned")
        batch_op.drop_column("last_reinforced_at")
        batch_op.drop_column("reinforcement_count")
        batch_op.drop_column("origin")
        batch_op.drop_column("fingerprint")
        batch_op.drop_column("slot_value")
        batch_op.drop_column("slot_key")
        batch_op.drop_column("scope")
        batch_op.drop_column("team_id")
        batch_op.drop_column("owner_id")
