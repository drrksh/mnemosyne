"""
The same platform on PostgreSQL (team deployments). Runs when
`MNEMOSYNE_TEST_POSTGRES_URL` points at a database the tests may wipe,
e.g. postgresql+psycopg://mnemosyne:mnemosyne@127.0.0.1/mnemosyne_test;
CI provides one.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from mnemosyne.config import Settings

POSTGRES_URL = os.environ.get("MNEMOSYNE_TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not POSTGRES_URL, reason="set MNEMOSYNE_TEST_POSTGRES_URL to run PostgreSQL tests"),
]


@pytest.fixture
def pg_url():
    assert POSTGRES_URL
    engine = create_engine(POSTGRES_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return POSTGRES_URL


@pytest.fixture
def pg_container(pg_url, _isolated_home):
    from mnemosyne.container import Mnemosyne

    instance = Mnemosyne(Settings(database_url=pg_url, home=_isolated_home, app_env="testing"), schema="migrate")
    yield instance
    instance.close()


def test_migrations_match_models_and_downgrade_cleanly(pg_url):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from mnemosyne.db import migrate
    from mnemosyne.db.base import Base
    from mnemosyne.repositories.sqlite import orm_models  # noqa: F401

    engine = create_engine(pg_url)
    migrate.upgrade(engine)
    assert migrate.is_up_to_date(engine)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == [], f"migrations and ORM models disagree on PostgreSQL: {diff}"
    migrate.downgrade(engine, "base")
    assert migrate.current_revision(engine) is None
    engine.dispose()


def test_learning_retrieval_and_context(pg_container, tmp_path):
    from mnemosyne.client.backend import LocalBackend
    from mnemosyne.client.explicit import explicit_candidate

    backend = LocalBackend(pg_container)
    report = backend.ingest([explicit_candidate("use yarn instead of npm"),
                             explicit_candidate("Money is stored as integer cents", scope="repo:github.com/a/pay")],
                            source="t")
    assert report.counts["created"] == 2
    superseded = backend.ingest([explicit_candidate("Use pnpm, never npm")], source="t").outcomes[0]
    assert (superseded["action"], superseded["previous_value"]) == ("superseded", "yarn")

    memory_id = superseded["memory_id"]
    assert backend.feedback(memory_id, "confirm")["origin"] == "explicit"
    assert {e["kind"] for e in backend.evidence(memory_id)} == {"explicit", "feedback"}
    assert backend.recall("which package manager", cwd=None)[0]["memory"]["value"] == "pnpm"
    assert backend.stats()["active"] == 2

    bundle = backend.export()
    assert pg_container.memories.erase_personal_memories(principal=backend.principal) == 3
    assert backend.import_bundle(bundle).counts["created"] == 2


def test_tenancy_and_api_keys(pg_container):
    from mnemosyne.domain.dto import NewMemoryData
    from mnemosyne.domain.exceptions import NotFoundError

    identity = pg_container.identity
    alice, bob = identity.create_user("alice"), identity.create_user("bob")
    team = identity.create_team("payments")
    identity.add_member(team.id, alice.id)
    as_alice = identity.authenticate(identity.issue_api_key(alice.id, name="k").plaintext)
    as_bob = identity.authenticate(identity.issue_api_key(bob.id, name="k").plaintext)

    private = pg_container.memories.create_memory(NewMemoryData(title="alice only"), principal=as_alice)
    shared = pg_container.memories.create_memory(NewMemoryData(title="team rule", team_id=team.id),
                                                 principal=as_alice)
    with pytest.raises(NotFoundError):
        pg_container.memories.get_memory(private.id, principal=as_bob)
    with pytest.raises(NotFoundError):
        pg_container.memories.get_memory(shared.id, principal=as_bob)
    identity.add_member(team.id, bob.id)
    as_bob = identity.principal_for(bob.id)
    assert pg_container.memories.get_memory(shared.id, principal=as_bob).title == "team rule"
