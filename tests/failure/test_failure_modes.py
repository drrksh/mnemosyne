"""
Explicit tests for the graceful-degradation behaviors called out in the
project's failure-handling requirements: malformed metadata, invalid
identifiers, concurrent writes, and duplicate-key protection must never
crash the process or corrupt existing data.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from mnemosyne.domain.dto import MemoryUpdateData, NewMemoryData
from mnemosyne.domain.exceptions import ConflictError, NotFoundError


def test_malformed_metadata_is_dropped_not_fatal(memory_engine):
    memory = memory_engine.build_new_memory(NewMemoryData(title="ok", metadata={"set": {1, 2}}))
    assert memory.metadata == {}
    assert memory.title == "ok"  # the rest of the memory is unaffected


def test_oversized_metadata_is_dropped(memory_engine):
    huge = {"blob": "x" * 200_000}
    memory = memory_engine.build_new_memory(NewMemoryData(title="ok", metadata=huge))
    assert memory.metadata == {}


def test_invalid_memory_id_format_returns_422_not_500(client):
    response = client.get("/api/v1/memories/not-a-uuid")
    assert response.status_code == 422


def test_missing_title_returns_422_not_500(client):
    response = client.post("/api/v1/memories", json={})
    assert response.status_code == 422


def test_malformed_json_body_returns_422_not_500(client):
    response = client.patch(
        "/api/v1/memories/00000000-0000-0000-0000-000000000000",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_duplicate_add_does_not_corrupt_existing_row(uow_factory, memory_engine, new_memory_factory):
    memory = memory_engine.build_new_memory(new_memory_factory(title="Guarded"))
    with uow_factory() as uow:
        uow.memories.add(memory)
        uow.commit()

    with pytest.raises(ConflictError):
        with uow_factory() as uow:
            uow.memories.add(memory)
            uow.commit()

    with uow_factory() as uow:
        fetched = uow.memories.get(memory.id)
    assert fetched.title == "Guarded"  # original row untouched by the failed insert


def test_update_of_nonexistent_memory_raises_not_found_not_500(memory_service):
    with pytest.raises(NotFoundError):
        memory_service.update_memory(uuid4(), MemoryUpdateData(title="x"))


def test_concurrent_style_conflicting_updates_second_write_rejected(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory(title="Race"))
    # Simulate two clients that both read version 1 before either writes.
    memory_service.update_memory(memory.id, MemoryUpdateData(title="First writer wins"), expected_version=1)
    with pytest.raises(ConflictError):
        memory_service.update_memory(memory.id, MemoryUpdateData(title="Second writer"), expected_version=1)

    # The first writer's change must have stuck, unharmed by the conflict.
    current = memory_service.get_memory(memory.id, track_access=False)
    assert current.title == "First writer wins"


def test_hard_delete_of_nonexistent_memory_is_not_found_not_500(client):
    response = client.delete(f"/api/v1/memories/{uuid4()}?permanent=true")
    assert response.status_code == 404


def test_unexpected_exception_is_never_exposed_to_client(app, monkeypatch):
    """If something genuinely unexpected happens deep in the stack, a real
    HTTP client must still get a clean 500 with no internal details — never
    a raw traceback or exception message.

    This uses `raise_server_exceptions=False` because that is what an
    actual deployed server does: TestClient's default of re-raising
    server exceptions is a debugging convenience for the *test author*,
    not a reflection of what a real client over the network would see.
    """
    from starlette.testclient import TestClient

    from mnemosyne.services.memory_service import MemoryService

    def _boom(self, *args, **kwargs):
        raise RuntimeError("simulated infrastructure failure with secret path /etc/shadow")

    monkeypatch.setattr(MemoryService, "get_memory", _boom)

    real_world_client = TestClient(app, raise_server_exceptions=False)
    response = real_world_client.get(f"/api/v1/memories/{uuid4()}")
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "internal_error"
    assert "secret" not in body["detail"]
    assert "/etc/shadow" not in body["detail"]
