"""
Unit of Work: owns a single transaction boundary around one or more
repository operations.

Every use-case in the service layer opens exactly one UnitOfWork,
performs its repository calls, and calls `commit()` once. If anything
raises before `commit()`, `__exit__` rolls back automatically — callers
never need to remember to clean up after themselves, which is what
guarantees we never persist a partial write.

The repository implementation is injected (defaulting to
`SqliteMemoryRepository`) rather than hardcoded, so a future storage
backend can be swapped in by constructing `UnitOfWork` with a different
`repository_factory` — nothing about the UnitOfWork's transactional
behavior needs to change.
"""
from __future__ import annotations

import logging
from types import TracebackType
from typing import Callable, Optional, Type

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from mnemosyne.db.session import SessionFactory
from mnemosyne.domain.exceptions import RepositoryError
from mnemosyne.repositories.interfaces import MemoryRepository
from mnemosyne.repositories.sqlite.memory_repository import SqliteMemoryRepository

logger = logging.getLogger("mnemosyne.db.uow")

RepositoryFactory = Callable[[Session], MemoryRepository]


class UnitOfWork:
    """Transactional context manager wrapping one SQLAlchemy session."""

    memories: MemoryRepository
    session: Session

    def __init__(
        self,
        session_factory: SessionFactory,
        repository_factory: RepositoryFactory = SqliteMemoryRepository,
    ) -> None:
        self._session_factory = session_factory
        self._repository_factory = repository_factory
        self._committed = False

    def __enter__(self) -> "UnitOfWork":
        self.session = self._session_factory()
        self.memories = self._repository_factory(self.session)
        self._committed = False
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        try:
            if exc_type is not None or not self._committed:
                self.session.rollback()
        finally:
            self.session.close()

    def commit(self) -> None:
        try:
            self.session.commit()
            self._committed = True
        except SQLAlchemyError as exc:
            self.session.rollback()
            logger.exception("transaction_commit_failed")
            raise RepositoryError("Failed to commit transaction") from exc

    def rollback(self) -> None:
        self.session.rollback()
