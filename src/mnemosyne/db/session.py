"""
Engine and session factory construction.

This module is the *only* place that knows how to turn a
`DATABASE_URL` into a working SQLAlchemy Engine. Swapping SQLite for
PostgreSQL later means changing the connection-args/pragma logic here
— nothing in the repository or service layers needs to change.
"""
from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from mnemosyne.config import Settings

logger = logging.getLogger("mnemosyne.db")

SessionFactory = Callable[[], Session]


def create_engine_from_settings(settings: Settings) -> Engine:
    """Build a SQLAlchemy Engine appropriate for the configured database URL."""
    url = settings.database_url
    is_sqlite = url.startswith("sqlite")
    is_memory = "memory" in url

    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {"echo": settings.sql_echo, "future": True}

    if is_sqlite:
        connect_args["check_same_thread"] = False
        if is_memory:
            # A shared in-memory DB requires every connection in the pool
            # to be the SAME connection, or each new connection sees a
            # blank database. StaticPool guarantees that.
            engine_kwargs["poolclass"] = StaticPool

    engine = create_engine(url, connect_args=connect_args, **engine_kwargs)

    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if not is_memory:
                # WAL improves concurrent read/write behavior for a
                # file-backed database; it is meaningless for :memory:.
                cursor.execute("PRAGMA journal_mode=WAL")
            # Have SQLite itself retry for up to 5s on "database is
            # locked" before raising, rather than failing immediately.
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker:
    """Build a session factory bound to the given engine.

    `expire_on_commit=False` matters here: repository methods map ORM
    rows to domain dataclasses *before* returning them, but keeping
    attributes accessible post-commit avoids DetachedInstanceError
    surprises for any code that inspects the ORM object itself (e.g.
    in tests) after the surrounding `with` block has committed.
    """
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )
