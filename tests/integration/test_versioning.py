"""Focused tests for memory version-history correctness — the "never
overwrite information" requirement."""
from __future__ import annotations

import pytest

from mnemosyne.domain.dto import MemoryUpdateData
from mnemosyne.domain.exceptions import NotFoundError


def test_version_snapshot_matches_pre_update_state(memory_service, new_memory_factory):
    memory = memory_service.create_memory(
        new_memory_factory(title="Original title", summary="Original summary", tags=["a"])
    )
    memory_service.update_memory(memory.id, MemoryUpdateData(title="New title", tags=["a", "b"]))

    versions = memory_service.list_versions(memory.id)
    assert len(versions) == 1
    snapshot = versions[0]
    assert snapshot.version_number == 1
    assert snapshot.title == "Original title"
    assert snapshot.summary == "Original summary"
    assert snapshot.tags == ["a"]
    assert set(snapshot.changed_fields) == {"title", "tags"}


def test_get_version_returns_specific_historical_snapshot(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory(title="v1"))
    memory_service.update_memory(memory.id, MemoryUpdateData(title="v2"))
    memory_service.update_memory(memory.id, MemoryUpdateData(title="v3"))

    v1 = memory_service.get_version(memory.id, 1)
    v2 = memory_service.get_version(memory.id, 2)
    assert v1.title == "v1"
    assert v2.title == "v2"


def test_get_version_falls_back_to_current_state_for_never_updated_memory(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory(title="only version"))
    current_as_version = memory_service.get_version(memory.id, 1)
    assert current_as_version.title == "only version"
    assert current_as_version.change_reason == "current"
    assert current_as_version.changed_fields == []


def test_get_version_unknown_number_raises_not_found(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory())
    with pytest.raises(NotFoundError):
        memory_service.get_version(memory.id, 42)


def test_get_version_for_missing_memory_raises_not_found(memory_service):
    from uuid import uuid4

    with pytest.raises(NotFoundError):
        memory_service.get_version(uuid4(), 1)


def test_list_versions_for_missing_memory_raises_not_found(memory_service):
    from uuid import uuid4

    with pytest.raises(NotFoundError):
        memory_service.list_versions(uuid4())


def test_status_transitions_are_versioned(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory())
    memory_service.archive_memory(memory.id)
    memory_service.restore_memory(memory.id)

    versions = memory_service.list_versions(memory.id)
    assert len(versions) == 2
    assert versions[0].status.value == "active"  # state before archiving
    assert versions[1].status.value == "archived"  # state before restoring


def test_content_is_never_overwritten_it_is_versioned(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory(content="Original content"))
    memory_service.update_memory(memory.id, MemoryUpdateData(content="New content"))

    # The live row shows the new content...
    current = memory_service.get_memory(memory.id, track_access=False)
    assert current.content == "New content"

    # ...but the original is still fully recoverable.
    v1 = memory_service.get_version(memory.id, 1)
    assert v1.content == "Original content"
