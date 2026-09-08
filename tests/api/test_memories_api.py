"""End-to-end tests through the HTTP API (FastAPI TestClient, no live server
process — see conftest.py's `client` fixture)."""
from __future__ import annotations

import uuid


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_and_get_memory(client):
    payload = {
        "title": "API test memory",
        "summary": "Created through the API",
        "memory_type": "fact",
        "confidence_score": 0.8,
        "tags": ["api", "test"],
    }
    create_resp = client.post("/api/v1/memories", json=payload)
    assert create_resp.status_code == 201
    body = create_resp.json()
    assert body["title"] == "API test memory"
    assert body["version"] == 1
    assert body["access_count"] == 0
    memory_id = body["id"]

    get_resp = client.get(f"/api/v1/memories/{memory_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["access_count"] == 1  # GET tracks access


def test_create_rejects_invalid_confidence(client):
    response = client.post("/api/v1/memories", json={"title": "bad", "confidence_score": 3.0})
    assert response.status_code == 422


def test_create_rejects_missing_title(client):
    response = client.post("/api/v1/memories", json={})
    assert response.status_code == 422


def test_get_unknown_memory_returns_404(client):
    response = client.get(f"/api/v1/memories/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"] == "NotFoundError"
    assert "request_id" in response.json()


def test_update_memory_and_conflict_on_stale_version(client):
    create_resp = client.post("/api/v1/memories", json={"title": "Original"})
    memory_id = create_resp.json()["id"]

    update_resp = client.patch(
        f"/api/v1/memories/{memory_id}", json={"title": "Updated", "expected_version": 1}
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["title"] == "Updated"
    assert update_resp.json()["version"] == 2

    conflict_resp = client.patch(
        f"/api/v1/memories/{memory_id}", json={"title": "Again", "expected_version": 1}
    )
    assert conflict_resp.status_code == 409
    assert conflict_resp.json()["error"] == "ConflictError"


def test_update_rejects_unknown_fields(client):
    create_resp = client.post("/api/v1/memories", json={"title": "Original"})
    memory_id = create_resp.json()["id"]
    response = client.patch(f"/api/v1/memories/{memory_id}", json={"totally_made_up_field": 1})
    assert response.status_code == 422


def test_archive_restore_and_soft_delete_flow(client):
    memory_id = client.post("/api/v1/memories", json={"title": "Lifecycle"}).json()["id"]

    archive_resp = client.post(f"/api/v1/memories/{memory_id}/archive")
    assert archive_resp.status_code == 200
    assert archive_resp.json()["status"] == "archived"

    restore_resp = client.post(f"/api/v1/memories/{memory_id}/restore")
    assert restore_resp.json()["status"] == "active"

    delete_resp = client.delete(f"/api/v1/memories/{memory_id}")
    assert delete_resp.status_code == 204

    get_resp = client.get(f"/api/v1/memories/{memory_id}")
    assert get_resp.status_code == 404


def test_permanent_delete_removes_version_history_too(client):
    memory_id = client.post("/api/v1/memories", json={"title": "Doomed"}).json()["id"]
    client.patch(f"/api/v1/memories/{memory_id}", json={"title": "Doomed v2"})

    resp = client.delete(f"/api/v1/memories/{memory_id}?permanent=true")
    assert resp.status_code == 204

    versions_resp = client.get(f"/api/v1/memories/{memory_id}/versions")
    assert versions_resp.status_code == 404


def test_search_with_tag_filter_and_pagination(client):
    for i in range(3):
        client.post(
            "/api/v1/memories",
            json={"title": f"Searchable {i}", "memory_type": "fact", "tags": ["search-test"]},
        )
    response = client.get("/api/v1/memories", params={"tag": "search-test", "page_size": 2, "page": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["has_next"] is True
    assert body["total_pages"] == 2


def test_search_rejects_invalid_sort_field(client):
    response = client.get("/api/v1/memories", params={"sort_by": "not_a_field"})
    assert response.status_code == 422  # FastAPI Literal validation


def test_version_history_endpoints(client):
    memory_id = client.post("/api/v1/memories", json={"title": "v1"}).json()["id"]
    client.patch(f"/api/v1/memories/{memory_id}", json={"title": "v2"})

    versions_resp = client.get(f"/api/v1/memories/{memory_id}/versions")
    assert versions_resp.status_code == 200
    assert len(versions_resp.json()) == 1
    assert versions_resp.json()[0]["title"] == "v1"

    version_resp = client.get(f"/api/v1/memories/{memory_id}/versions/1")
    assert version_resp.status_code == 200
    assert version_resp.json()["title"] == "v1"

    missing_version_resp = client.get(f"/api/v1/memories/{memory_id}/versions/999")
    assert missing_version_resp.status_code == 404


def test_batch_create_and_bulk_update(client):
    batch_resp = client.post(
        "/api/v1/memories/batch",
        json={"items": [{"title": "Batch A"}, {"title": "Batch B"}]},
    )
    assert batch_resp.status_code == 201
    items = batch_resp.json()
    assert len(items) == 2

    bulk_resp = client.patch(
        "/api/v1/memories/batch",
        json={
            "items": [
                {"memory_id": items[0]["id"], "changes": {"title": "Batch A updated"}},
                {"memory_id": items[1]["id"], "changes": {"importance_score": 0.9}},
            ]
        },
    )
    assert bulk_resp.status_code == 200
    updated = bulk_resp.json()
    assert updated[0]["title"] == "Batch A updated"
    assert updated[1]["importance_score"] == 0.9


def test_response_includes_request_id_header(client):
    response = client.get("/health")
    assert "x-request-id" in {k.lower() for k in response.headers.keys()}
