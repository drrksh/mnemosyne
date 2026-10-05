"""HTTP round-trips for context, recall, learning, feedback, portability,
keys, audit, administration, and operational endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mnemosyne.api.routers.account import ERASE_CONFIRMATION
from mnemosyne.domain.principal import Permission


@pytest.fixture
def seeded_client(client):
    for payload in (
        {"title": "Never force-push to shared branches", "memory_type": "rule"},
        {"title": "Tests use pytest", "content": "pytest with fixtures in conftest.py", "memory_type": "tooling",
         "scope": "lang:python", "key": "python.test_framework", "value": "pytest"},
        {"title": "API uses uv", "memory_type": "tooling", "scope": "repo:github.com/acme/api",
         "key": "python.package_manager", "value": "uv"},
        {"title": "Web uses pnpm", "memory_type": "tooling", "scope": "repo:github.com/acme/web",
         "key": "javascript.package_manager", "value": "pnpm"},
    ):
        assert client.post("/api/v1/memories", json=payload).status_code == 201
    return client


CTX = {"repo_id": "github.com/acme/api", "languages": ["python"]}


def test_context_endpoint(seeded_client):
    response = seeded_client.post("/api/v1/context", json={"task": "write unit tests", "context": CTX,
                                                           "wrap": True, "explain": True})
    assert response.status_code == 200
    body = response.json()
    assert body["text"].startswith("<mnemosyne-context>")
    titles = [i["title"] for i in body["items"]]
    assert "Tests use pytest" in titles and "API uses uv" in titles and "Web uses pnpm" not in titles
    relevant = [i for i in body["items"] if i["reason"] == "relevant"]
    assert relevant and relevant[0]["breakdown"]["relevance"] > 0


def test_context_rejects_server_paths_in_api_key_mode(api_key_app):
    container = api_key_app.state.container
    user = container.identity.create_user("alice")
    key = container.identity.issue_api_key(user.id, name="t").plaintext
    response = TestClient(api_key_app).post(
        "/api/v1/context", json={"context": {"repo_path": "/etc"}}, headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 403


def test_recall_endpoint(seeded_client):
    body = seeded_client.post("/api/v1/recall", json={"query": "pytest fixtures", "context": CTX,
                                                      "explain": True}).json()
    assert body["items"][0]["memory"]["title"] == "Tests use pytest"
    assert "lexical" in body["items"][0]["breakdown"]
    assert body["index_size"] == 4


def test_profile_endpoint(seeded_client):
    body = seeded_client.get("/api/v1/profile", params={"repo_id": "github.com/acme/api",
                                                        "language": "python"}).json()
    assert {"Never force-push to shared branches", "API uses uv"} <= {i["title"] for i in body["items"]}


def test_learn_conversation_and_feedback(client):
    response = client.post("/api/v1/learn/conversation", json={
        "messages": [{"role": "user", "content": "Use pnpm instead of npm."},
                     {"role": "assistant", "content": "Sure."}],
        "session_id": "s1",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["counts"]["created"] == 1
    memory_id = body["outcomes"][0]["memory_id"]

    feedback = client.post(f"/api/v1/memories/{memory_id}/feedback", json={"signal": "confirm"})
    assert feedback.status_code == 200 and feedback.json()["origin"] == "explicit"
    evidence = client.get(f"/api/v1/memories/{memory_id}/evidence").json()
    assert {e["kind"] for e in evidence} == {"conversation", "feedback"}


def test_learn_conversation_requires_exactly_one_source(client):
    assert client.post("/api/v1/learn/conversation", json={}).status_code == 422
    assert client.post("/api/v1/learn/conversation", json={"transcript": "x", "messages": []}).status_code == 422


def test_learn_candidates(client):
    response = client.post("/api/v1/learn/candidates", json={"source": "ide-plugin", "candidates": [{
        "title": "Prefers early returns", "memory_type": "pattern", "origin": "inferred", "confidence": 0.8,
        "evidence": [{"kind": "code", "source_ref": "ide:file.py#1", "excerpt": "if not x: return"}],
    }]})
    assert response.json()["counts"]["created"] == 1


def test_learn_repository_in_local_mode(client, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n[tool.ruff]\nline-length = 99\n")
    body = client.post("/api/v1/learn/repository", json={"path": str(tmp_path)}).json()
    assert body["findings"] >= 1 and body["counts"]["created"] >= 1


def test_learn_repository_is_refused_in_api_key_mode(api_key_app, tmp_path):
    container = api_key_app.state.container
    user = container.identity.create_user("bob")
    key = container.identity.issue_api_key(user.id, name="t").plaintext
    response = TestClient(api_key_app).post("/api/v1/learn/repository", json={"path": str(tmp_path)},
                                            headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 403


def test_export_import_roundtrip_and_erasure(seeded_client, app):
    bundle = seeded_client.get("/api/v1/me/export").json()
    assert bundle["format"] == "mnemosyne-export" and len(bundle["memories"]) == 4

    wrong = seeded_client.delete("/api/v1/me/memories", params={"confirm": "yes"})
    assert wrong.status_code == 422
    erased = seeded_client.delete("/api/v1/me/memories", params={"confirm": ERASE_CONFIRMATION}).json()
    assert erased == {"erased": 4}
    assert seeded_client.get("/api/v1/memories").json()["total"] == 0

    restored = seeded_client.post("/api/v1/me/import", json=bundle).json()
    assert restored["counts"]["created"] == 4
    again = seeded_client.post("/api/v1/me/import", json=bundle).json()
    assert again["counts"]["created"] == 0  # idempotent


def test_import_rejects_foreign_bundles(client):
    assert client.post("/api/v1/me/import", json={"format": "something-else"}).status_code == 422


def test_me_keys_and_audit(api_key_app):
    container = api_key_app.state.container
    user = container.identity.create_user("carol")
    key = container.identity.issue_api_key(user.id, name="bootstrap").plaintext
    client = TestClient(api_key_app)
    headers = {"Authorization": f"Bearer {key}"}

    me = client.get("/api/v1/me", headers=headers).json()
    assert me["user_id"] == user.id and me["auth_mode"] == "api_key"

    created = client.post("/api/v1/me/keys", json={"name": "ci", "scopes": [Permission.READ]}, headers=headers)
    assert created.status_code == 201 and created.json()["key"].startswith("mn_")
    listed = client.get("/api/v1/me/keys", headers=headers).json()
    assert {k["name"] for k in listed} == {"bootstrap", "ci"} and all(k["key"] is None for k in listed)
    assert client.delete(f"/api/v1/me/keys/{created.json()['id']}", headers=headers).status_code == 204
    revoked = {"Authorization": f"Bearer {created.json()['key']}"}
    assert client.get("/api/v1/me", headers=revoked).status_code == 401

    client.post("/api/v1/memories", json={"title": "x"}, headers=headers)
    actions = [e["action"] for e in client.get("/api/v1/audit", headers=headers).json()]
    assert "memory.create" in actions and "api_key.issue" in actions


def test_admin_endpoints_require_admin(api_key_app):
    container = api_key_app.state.container
    admin = container.identity.create_user("root", is_admin=True)
    user = container.identity.create_user("dave")
    admin_key = container.identity.issue_api_key(admin.id, name="a", scopes=[Permission.ADMIN, Permission.READ,
                                                                               Permission.WRITE]).plaintext
    user_key = container.identity.issue_api_key(user.id, name="u").plaintext
    client = TestClient(api_key_app)
    as_admin = {"Authorization": f"Bearer {admin_key}"}

    assert client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {user_key}"}).status_code == 403
    created = client.post("/api/v1/admin/users", json={"handle": "erin"}, headers=as_admin)
    assert created.status_code == 201
    team = client.post("/api/v1/admin/teams", json={"name": "payments"}, headers=as_admin).json()
    assert client.put(f"/api/v1/admin/teams/{team['id']}/members/{user.id}", json={"role": "member"},
                      headers=as_admin).status_code == 204
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {user_key}"}).json()
    assert me["team_ids"] == [team["id"]]


def test_health_ready_and_metrics(client):
    assert client.get("/health").json()["status"] == "ok"
    client.get("/api/v1/memories")
    metrics = client.get("/metrics").text
    assert 'route="/api/v1/memories"' in metrics
    assert "mnemosyne_http_request_duration_seconds_bucket" in metrics


def test_ready_reports_schema_state(test_settings):
    from mnemosyne.api.app import create_app
    from mnemosyne.container import Mnemosyne

    migrated = TestClient(create_app(container=Mnemosyne(test_settings, schema="migrate")))
    assert migrated.get("/ready").json()["ready"] is True
    unmigrated = TestClient(create_app(container=Mnemosyne(test_settings, schema="create")))
    assert unmigrated.get("/ready").status_code == 503


def test_metrics_require_admin_in_api_key_mode(api_key_app):
    assert TestClient(api_key_app).get("/metrics").status_code == 401


def test_rate_limiting(api_key_app):
    from mnemosyne.api.observability import RateLimiter

    container = api_key_app.state.container
    user = container.identity.create_user("frank")
    key = container.identity.issue_api_key(user.id, name="t").plaintext
    api_key_app.state.rate_limiter = RateLimiter(per_minute=3)
    client = TestClient(api_key_app)
    statuses = [client.get("/api/v1/me", headers={"Authorization": f"Bearer {key}"}).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200] and statuses[3] == 429


def test_oversized_requests_are_rejected(client, app):
    response = client.post("/api/v1/memories", content=b"x" * 64,
                           headers={"Content-Type": "application/json", "Content-Length": str(50 * 1024 * 1024)})
    assert response.status_code == 413
