"""
Verifies that the Alembic migration chain actually creates the expected
schema (and can be fully reversed), independent of
`Base.metadata.create_all` which every other test fixture relies on for
speed. This is the one place that would catch a hand-edited migration
file drifting from the ORM models — `test_migrated_schema_matches_orm_models`
checks that property directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from mnemosyne.db import migrate
from mnemosyne.db.base import Base
from mnemosyne.repositories.sqlite import orm_models  # noqa: F401

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {
    "memories",
    "memory_versions",
    "tags",
    "memory_tags",
    "memory_evidence",
    "memory_embeddings",
    "users",
    "teams",
    "team_members",
    "api_keys",
    "audit_events",
    "alembic_version",
}


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "src" / "mnemosyne" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


@pytest.mark.slow
def test_alembic_upgrade_creates_expected_tables(tmp_path):
    db_path = tmp_path / "migration_up.db"
    database_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(database_url), "head")

    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert EXPECTED_TABLES.issubset(tables)


@pytest.mark.slow
def test_alembic_upgrade_creates_expected_constraints(tmp_path):
    db_path = tmp_path / "migration_constraints.db"
    database_url = f"sqlite:///{db_path}"
    command.upgrade(_alembic_config(database_url), "head")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    memory_fks = inspector.get_foreign_keys("memory_tags")
    evidence_fks = inspector.get_foreign_keys("memory_evidence")
    engine.dispose()

    assert len(memory_fks) == 2
    assert all(fk.get("options", {}).get("ondelete") == "CASCADE" for fk in memory_fks)
    assert evidence_fks and evidence_fks[0].get("options", {}).get("ondelete") == "CASCADE"


@pytest.mark.slow
def test_alembic_downgrade_removes_all_tables(tmp_path):
    db_path = tmp_path / "migration_down.db"
    database_url = f"sqlite:///{db_path}"
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()

    assert tables == set()


@pytest.mark.slow
def test_alembic_upgrade_is_idempotent_reapplication_is_a_noop(tmp_path):
    """Running `upgrade head` twice in a row must not error or duplicate
    schema objects — this is what a deploy script that always runs
    migrations on startup depends on."""
    db_path = tmp_path / "migration_idempotent.db"
    database_url = f"sqlite:///{db_path}"
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # should be a no-op, not an error

    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert EXPECTED_TABLES.issubset(tables)


@pytest.mark.slow
def test_migrated_schema_matches_orm_models(tmp_path):
    """The migration chain and the ORM models must describe the same
    schema. Any difference here means either a migration is missing or a
    model was edited without one."""
    engine = create_engine(f"sqlite:///{tmp_path / 'drift.db'}")
    migrate.upgrade(engine)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)
    engine.dispose()
    assert diff == []


@pytest.mark.slow
def test_programmatic_upgrade_reports_head(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'programmatic.db'}")
    assert not migrate.is_up_to_date(engine)
    migrate.upgrade(engine)
    assert migrate.is_up_to_date(engine)
    assert migrate.current_revision(engine) == migrate.head_revision()
    engine.dispose()


@pytest.mark.slow
def test_phase1_database_upgrades_in_place_preserving_memories(tmp_path):
    """A database created by the Phase 1 schema, already holding data,
    must upgrade without losing rows, and existing memories must come out
    as personal, global, explicit memories of the local user."""
    engine = create_engine(f"sqlite:///{tmp_path / 'phase1.db'}")
    migrate.upgrade(engine, "be06f3efd9d0")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO memories (id, title, summary, content, memory_type, confidence_score, "
                "importance_score, status, version, metadata, created_at, updated_at, access_count) "
                "VALUES ('11111111-1111-1111-1111-111111111111', 'Old memory', '', 'body', 'fact', "
                "0.9, 0.5, 'active', 1, '{}', '2026-01-01 00:00:00', '2026-01-01 00:00:00', 0)"
            )
        )

    migrate.upgrade(engine)

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT title, owner_id, team_id, scope, origin, reinforcement_count, pinned FROM memories")
        ).one()
    engine.dispose()
    assert row == ("Old memory", "local", None, "global", "explicit", 1, 0)


def test_pinned_head_revision_matches_migration_files():
    """`HEAD_REVISION` lets every process check the schema without importing
    Alembic; adding a migration must update it."""
    assert migrate.head_revision() == migrate.HEAD_REVISION
