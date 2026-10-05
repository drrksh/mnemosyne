"""Integration tests against a real (in-memory) SQLite database, exercising
the repository layer directly — below the service/UnitOfWork orchestration
tested in test_memory_service.py."""
from __future__ import annotations

from uuid import uuid4

import pytest

from mnemosyne.domain.dto import MemoryUpdateData, SearchCriteria
from mnemosyne.domain.enums import MemoryStatus, MemoryType
from mnemosyne.domain.exceptions import ConflictError, NotFoundError, ValidationError


def test_add_and_get_roundtrip(uow_factory, memory_engine, new_memory_factory):
    memory = memory_engine.build_new_memory(new_memory_factory(title="Roundtrip"))
    with uow_factory() as uow:
        uow.memories.add(memory)
        uow.commit()

    with uow_factory() as uow:
        fetched = uow.memories.get(memory.id)

    assert fetched is not None
    assert fetched.title == "Roundtrip"
    assert set(fetched.tags) == {"testing", "sample"}
    assert fetched.version == 1


def test_get_missing_returns_none(uow_factory):
    with uow_factory() as uow:
        assert uow.memories.get(uuid4()) is None


def test_add_duplicate_id_raises_conflict(uow_factory, memory_engine, new_memory_factory):
    memory = memory_engine.build_new_memory(new_memory_factory())
    with uow_factory() as uow:
        uow.memories.add(memory)
        uow.commit()

    with pytest.raises(ConflictError):
        with uow_factory() as uow:
            uow.memories.add(memory)
            uow.commit()


def test_update_bumps_version_and_persists_changes(uow_factory, memory_engine, new_memory_factory):
    memory = memory_engine.build_new_memory(new_memory_factory(title="Before"))
    with uow_factory() as uow:
        uow.memories.add(memory)
        uow.commit()

    with uow_factory() as uow:
        updated = uow.memories.update(memory.id, expected_version=1, changes={"title": "After"})
        uow.commit()

    assert updated.title == "After"
    assert updated.version == 2


def test_update_with_stale_expected_version_raises_conflict(uow_factory, memory_engine, new_memory_factory):
    memory = memory_engine.build_new_memory(new_memory_factory())
    with uow_factory() as uow:
        uow.memories.add(memory)
        uow.commit()

    with pytest.raises(ConflictError):
        with uow_factory() as uow:
            uow.memories.update(memory.id, expected_version=99, changes={"title": "x"})
            uow.commit()


def test_update_missing_memory_raises_not_found(uow_factory):
    with pytest.raises(NotFoundError):
        with uow_factory() as uow:
            uow.memories.update(uuid4(), expected_version=1, changes={"title": "x"})
            uow.commit()


def test_record_access_increments_count(uow_factory, memory_engine, new_memory_factory):
    memory = memory_engine.build_new_memory(new_memory_factory())
    with uow_factory() as uow:
        uow.memories.add(memory)
        uow.commit()

    with uow_factory() as uow:
        uow.memories.record_access(memory.id)
        uow.memories.record_access(memory.id)
        uow.commit()

    with uow_factory() as uow:
        fetched = uow.memories.get(memory.id)
    assert fetched.access_count == 2
    assert fetched.last_accessed_at is not None


def test_record_access_missing_memory_raises_not_found(uow_factory):
    with pytest.raises(NotFoundError):
        with uow_factory() as uow:
            uow.memories.record_access(uuid4())
            uow.commit()


def test_hard_delete_removes_memory_and_versions(uow_factory, memory_engine, new_memory_factory):
    memory = memory_engine.build_new_memory(new_memory_factory())
    with uow_factory() as uow:
        uow.memories.add(memory)
        uow.commit()

    with uow_factory() as uow:
        plan = memory_engine.apply_update(memory, MemoryUpdateData(title="v2"))
        uow.memories.add_version(plan.version_snapshot)
        uow.memories.update(memory.id, expected_version=1, changes=plan.changed_fields)
        uow.commit()

    with uow_factory() as uow:
        deleted = uow.memories.hard_delete(memory.id)
        uow.commit()
    assert deleted is True

    with uow_factory() as uow:
        assert uow.memories.get(memory.id) is None
        assert uow.memories.list_versions(memory.id) == []


def test_hard_delete_missing_returns_false(uow_factory):
    with uow_factory() as uow:
        assert uow.memories.hard_delete(uuid4()) is False


def test_search_keyword_is_case_insensitive(uow_factory, memory_engine, new_memory_factory):
    memory = memory_engine.build_new_memory(new_memory_factory(title="The Quick Brown Fox"))
    with uow_factory() as uow:
        uow.memories.add(memory)
        uow.commit()

    with uow_factory() as uow:
        result = uow.memories.search(SearchCriteria(query="quick brown"))
    assert result.total == 1
    assert result.items[0].id == memory.id


def test_search_excludes_deleted_by_default(uow_factory, memory_engine, new_memory_factory):
    memory = memory_engine.build_new_memory(new_memory_factory(title="Gone"))
    with uow_factory() as uow:
        uow.memories.add(memory)
        uow.commit()

    with uow_factory() as uow:
        plan = memory_engine.apply_status_transition(memory, MemoryStatus.DELETED, change_reason="soft_deleted")
        uow.memories.add_version(plan.version_snapshot)
        uow.memories.update(memory.id, expected_version=1, changes=plan.changed_fields)
        uow.commit()

    with uow_factory() as uow:
        result = uow.memories.search(SearchCriteria(query="Gone"))
    assert result.total == 0

    with uow_factory() as uow:
        result_incl = uow.memories.search(
            SearchCriteria(query="Gone", statuses=[MemoryStatus.DELETED])
        )
    assert result_incl.total == 1


def test_search_filters_by_memory_type(uow_factory, memory_engine, new_memory_factory):
    with uow_factory() as uow:
        for title, memory_type in (("A fact", MemoryType.FACT), ("A goal", MemoryType.GOAL)):
            uow.memories.add(memory_engine.build_new_memory(new_memory_factory(title=title, memory_type=memory_type)))
        uow.commit()

    with uow_factory() as uow:
        result = uow.memories.search(SearchCriteria(memory_types=[MemoryType.GOAL]))
    assert result.total == 1
    assert result.items[0].memory_type == MemoryType.GOAL


def test_search_filters_by_confidence_range(uow_factory, memory_engine, new_memory_factory):
    with uow_factory() as uow:
        uow.memories.add(memory_engine.build_new_memory(new_memory_factory(title="Low", confidence_score=0.1)))
        uow.memories.add(memory_engine.build_new_memory(new_memory_factory(title="High", confidence_score=0.9)))
        uow.commit()

    with uow_factory() as uow:
        result = uow.memories.search(SearchCriteria(min_confidence=0.5))
    assert result.total == 1
    assert result.items[0].title == "High"


def test_search_filters_by_importance_range(uow_factory, memory_engine, new_memory_factory):
    with uow_factory() as uow:
        uow.memories.add(memory_engine.build_new_memory(new_memory_factory(title="Low", importance_score=0.1)))
        uow.memories.add(memory_engine.build_new_memory(new_memory_factory(title="High", importance_score=0.9)))
        uow.commit()

    with uow_factory() as uow:
        result = uow.memories.search(SearchCriteria(max_importance=0.5))
    assert result.total == 1
    assert result.items[0].title == "Low"


def test_search_filters_by_confidence_upper_and_importance_lower_bounds(uow_factory, memory_engine, new_memory_factory):
    with uow_factory() as uow:
        uow.memories.add(
            memory_engine.build_new_memory(
                new_memory_factory(title="Mid", confidence_score=0.5, importance_score=0.5)
            )
        )
        uow.memories.add(
            memory_engine.build_new_memory(
                new_memory_factory(title="Extreme", confidence_score=0.99, importance_score=0.01)
            )
        )
        uow.commit()

    with uow_factory() as uow:
        by_confidence = uow.memories.search(SearchCriteria(max_confidence=0.6))
        by_importance = uow.memories.search(SearchCriteria(min_importance=0.4))

    assert by_confidence.total == 1
    assert by_confidence.items[0].title == "Mid"
    assert by_importance.total == 1
    assert by_importance.items[0].title == "Mid"


def test_search_filters_by_created_and_updated_date_ranges(uow_factory, memory_engine, new_memory_factory):
    from datetime import timedelta

    from mnemosyne.domain.dto import MemoryUpdateData
    from mnemosyne.utils.time import utcnow

    with uow_factory() as uow:
        uow.memories.add(memory_engine.build_new_memory(new_memory_factory(title="Fresh")))
        uow.commit()

    now = utcnow()
    far_future = now + timedelta(days=365)
    far_past = now - timedelta(days=365)

    with uow_factory() as uow:
        # created_after in the future must exclude everything that exists now.
        none_created = uow.memories.search(SearchCriteria(created_after=far_future))
        all_created = uow.memories.search(SearchCriteria(created_after=far_past, created_before=far_future))

    assert none_created.total == 0
    assert all_created.total == 1

    # Bump updated_at via a real update, then filter on the updated_* range.
    memory = all_created.items[0]
    with uow_factory() as uow:
        plan = memory_engine.apply_update(memory, MemoryUpdateData(title="Updated title"))
        uow.memories.add_version(plan.version_snapshot)
        uow.memories.update(memory.id, expected_version=1, changes=plan.changed_fields)
        uow.commit()

    with uow_factory() as uow:
        recently_updated = uow.memories.search(SearchCriteria(updated_after=now))
        not_updated_since_future = uow.memories.search(SearchCriteria(updated_after=far_future))

    assert recently_updated.total == 1
    assert not_updated_since_future.total == 0


def test_update_can_change_memory_type_and_metadata(uow_factory, memory_engine, new_memory_factory):
    from mnemosyne.domain.enums import MemoryType

    memory = memory_engine.build_new_memory(new_memory_factory(memory_type=MemoryType.FACT, metadata={"a": 1}))
    with uow_factory() as uow:
        uow.memories.add(memory)
        uow.commit()

    with uow_factory() as uow:
        updated = uow.memories.update(
            memory.id,
            expected_version=1,
            changes={"memory_type": MemoryType.PROCEDURE, "metadata": {"b": 2}},
        )
        uow.commit()

    assert updated.memory_type == MemoryType.PROCEDURE
    assert updated.metadata == {"b": 2}


def test_add_version_duplicate_version_number_raises_conflict(uow_factory, memory_engine, new_memory_factory):
    memory = memory_engine.build_new_memory(new_memory_factory())
    with uow_factory() as uow:
        uow.memories.add(memory)
        uow.commit()

    plan = memory_engine.apply_update(memory, MemoryUpdateData(title="v2"))

    with uow_factory() as uow:
        uow.memories.add_version(plan.version_snapshot)
        uow.commit()

    # Recording the *same* version_number for the same memory a second
    # time must be rejected, not silently accepted — version history is
    # append-only and each version number must be unique per memory.
    with pytest.raises(ConflictError):
        with uow_factory() as uow:
            uow.memories.add_version(plan.version_snapshot)
            uow.commit()


def test_search_tag_any_vs_all(uow_factory, memory_engine, new_memory_factory):
    with uow_factory() as uow:
        uow.memories.add(memory_engine.build_new_memory(new_memory_factory(title="Both", tags=["red", "blue"])))
        uow.memories.add(memory_engine.build_new_memory(new_memory_factory(title="RedOnly", tags=["red"])))
        uow.commit()

    with uow_factory() as uow:
        any_result = uow.memories.search(SearchCriteria(tags=["red", "blue"], match_all_tags=False))
        all_result = uow.memories.search(SearchCriteria(tags=["red", "blue"], match_all_tags=True))

    assert any_result.total == 2
    assert all_result.total == 1
    assert all_result.items[0].title == "Both"


def test_search_pagination_and_total_are_consistent(uow_factory, memory_engine, new_memory_factory):
    with uow_factory() as uow:
        for i in range(5):
            uow.memories.add(memory_engine.build_new_memory(new_memory_factory(title=f"Item {i}")))
        uow.commit()

    with uow_factory() as uow:
        page1 = uow.memories.search(SearchCriteria(page=1, page_size=2, sort_by="created_at", sort_desc=False))
        page2 = uow.memories.search(SearchCriteria(page=2, page_size=2, sort_by="created_at", sort_desc=False))
        page3 = uow.memories.search(SearchCriteria(page=3, page_size=2, sort_by="created_at", sort_desc=False))

    assert page1.total == 5
    assert len(page1.items) == 2
    assert len(page2.items) == 2
    assert len(page3.items) == 1
    assert page1.items[0].title == "Item 0"
    assert page2.items[0].title == "Item 2"
    assert page3.items[0].title == "Item 4"
    assert page1.has_next is True
    assert page3.has_next is False
    assert page1.total_pages == 3


def test_search_rejects_unsortable_field(uow_factory):
    with uow_factory() as uow:
        with pytest.raises(ValidationError):
            uow.memories.search(SearchCriteria(sort_by="not_a_real_field"))


def test_search_rejects_invalid_pagination(uow_factory):
    with uow_factory() as uow:
        with pytest.raises(ValidationError):
            uow.memories.search(SearchCriteria(page=0))
        with pytest.raises(ValidationError):
            uow.memories.search(SearchCriteria(page_size=0))


def test_exists(uow_factory, memory_engine, new_memory_factory):
    memory = memory_engine.build_new_memory(new_memory_factory())
    with uow_factory() as uow:
        assert uow.memories.exists(memory.id) is False
        uow.memories.add(memory)
        uow.commit()

    with uow_factory() as uow:
        assert uow.memories.exists(memory.id) is True
