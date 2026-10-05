"""
Programmatic schema migrations.

`mnemosyne init`, the auto-migrate-on-startup path, and the test suite
all run Alembic through this module instead of shelling out to the
`alembic` CLI. The migration scripts ship *inside* the package
(`mnemosyne/migrations`), so this works from a `pip install` with no
source checkout and no `alembic.ini` on disk.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

logger = logging.getLogger("mnemosyne.db.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    cfg.attributes["configure_logging"] = False
    return cfg


def head_revision() -> str:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    head = script.get_current_head()
    assert head is not None, "migration chain has no head"
    return head


def current_revision(engine: Engine) -> Optional[str]:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def is_up_to_date(engine: Engine) -> bool:
    return current_revision(engine) == head_revision()


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Upgrade the database behind `engine` (no-op when already at head).

    The engine's own connection is reused, which matters for in-memory
    SQLite: a fresh connection would see a different, empty database."""
    cfg = alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)
    logger.info("schema_upgraded", extra={"revision": revision})


def downgrade(engine: Engine, revision: str) -> None:
    cfg = alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, revision)
