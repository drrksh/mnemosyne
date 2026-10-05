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
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Engine, inspect, text

if TYPE_CHECKING:  # pragma: no cover
    from alembic.config import Config

logger = logging.getLogger("mnemosyne.db.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

#: The newest migration. Every process checks the schema against it when
#: it opens the database, and editor hooks start a process per event, so
#: the check must not import Alembic (~0.1 s). A test asserts this equals
#: the head Alembic computes from the migration files.
HEAD_REVISION = "7c2f1a9b4d10"


def alembic_config(database_url: str) -> Config:
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    cfg.attributes["configure_logging"] = False
    return cfg


def head_revision() -> str:
    """The head of the migration chain, computed by Alembic from the files."""
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()
    assert head is not None, "migration chain has no head"
    return head


def current_revision(engine: Engine) -> Optional[str]:
    with engine.connect() as connection:
        if not inspect(connection).has_table("alembic_version"):
            return None
        rows = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    return rows[0] if len(rows) == 1 else (",".join(sorted(rows)) or None)


def is_up_to_date(engine: Engine) -> bool:
    return current_revision(engine) == HEAD_REVISION


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Upgrade the database behind `engine` (no-op when already at head).

    The engine's own connection is reused, which matters for in-memory
    SQLite: a fresh connection would see a different, empty database."""
    from alembic import command

    cfg = alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)
    logger.info("schema_upgraded", extra={"revision": revision})


def downgrade(engine: Engine, revision: str) -> None:
    from alembic import command

    cfg = alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, revision)
