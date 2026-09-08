"""Integration tests for MemoryService: transactional orchestration end-to-end."""
from __future__ import annotations

from uuid import uuid4

import pytest

from mnemosyne.domain.dto import BulkUpdateItem, MemoryUpdateData, SearchCriteria
from mnemosyne.domain.enums import MemoryStatus
from mnemosyne.domain.exceptions import ConflictError, NotFoundError, ValidationError


def test_create_then_get(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory(title="Created"))
    fetched = memory_service.get_memory(memory.id)
    assert fetched.title == "Created"


def test_get_tracks_access_by_default(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory())
    memory_service.get_memory(memory.id)
    memory_service.get_memory(memory.id)
    fetched = memory_service.get_memory(memory.id, track_access=False)
    assert fetched.access_count == 2


def test_get_missing_raises_not_found(memory_service):
    with pytest.raises(NotFoundError):
        memory_service.get_memory(uuid4())


def test_update_creates_version_history(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory(title="v1"))
    memory_service.update_memory(memory.id, MemoryUpdateData(title="v2"))
    memory_service.update_memory(memory.id, MemoryUpdateData(title="v3"))

    versions = memory_service.list_versions(memory.id)
    assert [v.title for v in versions] == ["v1", "v2"]

    current = memory_service.get_memory(memory.id, track_access=False)
    assert current.title == "v3"
    assert current.version == 3


def test_update_noop_does_not_create_version(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory(title="Same"))
    memory_service.update_memory(memory.id, MemoryUpdateData(title="Same"))
    assert memory_service.list_versions(memory.id) == []


def test_update_with_wrong_expected_version_raises_conflict(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory())
    with pytest.raises(ConflictError):
        memory_service.update_memory(memory.id, MemoryUpdateData(title="x"), expected_version=999)


def test_update_rejects_invalid_change_without_partial_write(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory())
    with pytest.raises(ValidationError):
        memory_service.update_memory(memory.id, MemoryUpdateData(confidence_score=5.0))
    fetched = memory_service.get_memory(memory.id, track_access=False)
    assert fetched.version == 1  # the invalid update must not have partially applied


def test_update_missing_memory_raises_not_found(memory_service):
    with pytest.raises(NotFoundError):
        memory_service.update_memory(uuid4(), MemoryUpdateData(title="x"))


def test_archive_then_restore(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory())
    archived = memory_service.archive_memory(memory.id)
    assert archived.status == MemoryStatus.ARCHIVED

    restored = memory_service.restore_memory(memory.id)
    assert restored.status == MemoryStatus.ACTIVE


def test_soft_delete_hides_from_default_get(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory())
    memory_service.soft_delete_memory(memory.id)

    with pytest.raises(NotFoundError):
        memory_service.get_memory(memory.id)

    still_there = memory_service.get_memory(memory.id, include_deleted=True, track_access=False)
    assert still_there.status == MemoryStatus.DELETED
    assert still_there.deleted_at is not None


def test_restore_after_soft_delete_clears_deleted_at(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory())
    memory_service.soft_delete_memory(memory.id)
    restored = memory_service.restore_memory(memory.id)
    assert restored.status == MemoryStatus.ACTIVE
    assert restored.deleted_at is None


def test_hard_delete_is_permanent(memory_service, new_memory_factory):
    memory = memory_service.create_memory(new_memory_factory())
    memory_service.hard_delete_memory(memory.id)
    with pytest.raises(NotFoundError):
        memory_service.get_memory(memory.id, include_deleted=True)


def test_hard_delete_missing_raises_not_found(memory_service):
    with pytest.raises(NotFoundError):
        memory_service.hard_delete_memory(uuid4())


def test_batch_create(memory_service, new_memory_factory):
    items = [new_memory_factory(title=f"Batch {i}") for i in range(3)]
    created = memory_service.create_memories_batch(items)
    assert len(created) == 3
    result = memory_service.search_memories(SearchCriteria(query="Batch"))
    assert result.total == 3


def test_bulk_update_is_atomic(memory_service, new_memory_factory):
    m1 = memory_service.create_memory(new_memory_factory(title="Keep"))

    items = [
        BulkUpdateItem(memory_id=m1.id, changes=MemoryUpdateData(title="Updated")),
        BulkUpdateItem(memory_id=uuid4(), changes=MemoryUpdateData(title="Nope")),  # missing id
    ]
    with pytest.raises(NotFoundError):
        memory_service.bulk_update_memories(items)

    # Nothing should have been committed — m1's title must be unchanged.
    fetched = memory_service.get_memory(m1.id, track_access=False)
    assert fetched.title == "Keep"


def test_bulk_update_applies_all_on_success(memory_service, new_memory_factory):
    m1 = memory_service.create_memory(new_memory_factory(title="One"))
    m2 = memory_service.create_memory(new_memory_factory(title="Two"))

    items = [
        BulkUpdateItem(memory_id=m1.id, changes=MemoryUpdateData(title="One updated")),
        BulkUpdateItem(memory_id=m2.id, changes=MemoryUpdateData(importance_score=0.9)),
    ]
    results = memory_service.bulk_update_memories(items)
    assert results[0].title == "One updated"
    assert results[1].importance_score == 0.9
