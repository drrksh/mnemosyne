"""
Shared pytest fixtures.

Most fixtures here build against a fresh in-memory SQLite database
(StaticPool-backed so the whole test session shares one connection),
created via `Base.metadata.create_all` rather than through Alembic —
that keeps the bulk of the suite fast. Alembic migrations themselves
are exercised separately in `tests/integration/test_migrations.py`
against a real temp-file database, so a migration file drifting from
the ORM models would still be caught.
"""
from __future__ import annotations

from typing import Callable

import pytest
from fastapi.testclient import TestClient

from mnemosyne.config import Settings
from mnemosyne.core.memory_engine import EngineLimits, MemoryEngine
from mnemosyne.db.base import Base
from mnemosyne.db.session import create_engine_from_settings, create_session_factory
from mnemosyne.db.unit_of_work import UnitOfWork
from mnemosyne.domain.dto import NewMemoryData
from mnemosyne.domain.enums import MemoryType
# Ensure ORM models are registered on Base.metadata before create_all.
from mnemosyne.repositories.sqlite import orm_models  # noqa: F401
from mnemosyne.services.memory_service import MemoryService


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        app_env="testing",
        log_format="console",
        sql_echo=False,
    )


@pytest.fixture
def engine(test_settings: Settings):
    eng = create_engine_from_settings(test_settings)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return create_session_factory(engine)


@pytest.fixture
def uow_factory(session_factory) -> Callable[[], UnitOfWork]:
    return lambda: UnitOfWork(session_factory)


@pytest.fixture
def memory_engine() -> MemoryEngine:
    return MemoryEngine(EngineLimits())


@pytest.fixture
def memory_service(uow_factory, memory_engine) -> MemoryService:
    return MemoryService(uow_factory=uow_factory, engine=memory_engine)


@pytest.fixture
def app(test_settings: Settings):
    from mnemosyne.api.app import create_app

    # create_app() builds its own engine internally (that is the point —
    # it is a self-contained factory). Tests still need that engine's
    # schema created, since we use Base.metadata.create_all rather than
    # Alembic for test speed (see module docstring).
    application = create_app(test_settings)
    Base.metadata.create_all(application.state.engine)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def make_new_memory(**overrides) -> NewMemoryData:
    defaults: dict = dict(
        title="Test memory",
        summary="A memory used in tests.",
        content="Full content of the test memory.",
        memory_type=MemoryType.FACT,
        confidence_score=0.9,
        importance_score=0.5,
        tags=["testing", "sample"],
        source="pytest",
        metadata={"origin": "unit-test"},
        notes=None,
    )
    defaults.update(overrides)
    return NewMemoryData(**defaults)


@pytest.fixture
def new_memory_factory() -> Callable[..., NewMemoryData]:
    return make_new_memory
