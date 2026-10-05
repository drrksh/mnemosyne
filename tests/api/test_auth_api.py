"""HTTP authentication and tenancy in `api_key` mode."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mnemosyne.api.security import check_bind_safety, is_loopback
from mnemosyne.config import Settings
from mnemosyne.domain.exceptions import ConfigurationError
from mnemosyne.domain.principal import Permission


@pytest.fixture
def setup(api_key_app):
    container = api_key_app.state.container
    alice = container.identity.create_user("alice")
    bob = container.identity.create_user("bob")
    keys = {
        "alice": container.identity.issue_api_key(alice.id, name="t").plaintext,
        "bob": container.identity.issue_api_key(bob.id, name="t").plaintext,
        "alice_ro": container.identity.issue_api_key(alice.id, name="ro", scopes=[Permission.READ]).plaintext,
    }
    return TestClient(api_key_app), keys


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_requests_without_a_key_are_rejected(setup):
    client, _ = setup
    response = client.get("/api/v1/memories")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert client.get("/api/v1/memories", headers=_h("mn_000000000000_wrongwrongwrongwrongwrong")).status_code == 401


def test_health_does_not_require_auth(setup):
    client, _ = setup
    assert client.get("/health").status_code == 200


def test_users_only_see_their_own_memories(setup):
    client, keys = setup
    created = client.post("/api/v1/memories", json={"title": "alice private"}, headers=_h(keys["alice"]))
    assert created.status_code == 201
    memory_id = created.json()["id"]
    assert created.json()["owner_id"] != "local"

    assert client.get(f"/api/v1/memories/{memory_id}", headers=_h(keys["bob"])).status_code == 404
    assert client.get("/api/v1/memories", headers=_h(keys["bob"])).json()["total"] == 0
    assert client.get("/api/v1/memories", headers=_h(keys["alice"])).json()["total"] == 1
    patched = client.patch(f"/api/v1/memories/{memory_id}", json={"title": "x"}, headers=_h(keys["bob"]))
    assert patched.status_code == 404


def test_x_api_key_header_is_accepted(setup):
    client, keys = setup
    assert client.get("/api/v1/memories", headers={"X-API-Key": keys["alice"]}).status_code == 200


def test_read_only_key_cannot_write(setup):
    client, keys = setup
    response = client.post("/api/v1/memories", json={"title": "nope"}, headers=_h(keys["alice_ro"]))
    assert response.status_code == 403
    assert client.get("/api/v1/memories", headers=_h(keys["alice_ro"])).status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_loopback_hosts_are_recognized(host):
    assert is_loopback(host)


def test_local_mode_refuses_network_binding():
    settings = Settings(auth_mode="local", database_url="sqlite:///:memory:")
    with pytest.raises(ConfigurationError):
        check_bind_safety(settings, "0.0.0.0")
    check_bind_safety(settings, "127.0.0.1")
    check_bind_safety(Settings(auth_mode="api_key", database_url="sqlite:///:memory:"), "0.0.0.0")
    check_bind_safety(
        Settings(auth_mode="local", allow_insecure_remote=True, database_url="sqlite:///:memory:"), "0.0.0.0"
    )
