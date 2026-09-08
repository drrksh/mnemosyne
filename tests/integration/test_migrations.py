"""
Verifies that the Alembic migration chain actually creates the expected
schema (and can be fully reversed), independent of
`Base.metadata.create_all` which every other test fixture relies on for
speed. This is the one place that would catch a hand-edited migration
file drifting from the ORM models.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {"memories", "memory_versions", "tags", "memory_tags", "alembic_version"}


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
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
    engine.dispose()

    assert len(memory_fks) == 2
    assert all(fk.get("options", {}).get("ondelete") == "CASCADE" for fk in memory_fks)


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
