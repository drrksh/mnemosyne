"""Unit tests for MemoryEngine — pure business rules, no database."""
from __future__ import annotations

import pytest

from mnemosyne.core.memory_engine import EngineLimits, MemoryEngine
from mnemosyne.domain.dto import MemoryUpdateData, NewMemoryData
from mnemosyne.domain.enums import MemoryStatus
from mnemosyne.domain.exceptions import ValidationError


@pytest.fixture
def engine() -> MemoryEngine:
    return MemoryEngine(EngineLimits(max_title_length=50, max_tags_per_memory=3, max_tag_length=10))


def test_build_new_memory_applies_defaults(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="Hello"))
    assert memory.title == "Hello"
    assert memory.version == 1
    assert memory.status == MemoryStatus.ACTIVE
    assert memory.access_count == 0
    assert memory.confidence_score == 1.0
    assert memory.importance_score == 0.5
    assert memory.last_accessed_at is None
    assert memory.deleted_at is None


def test_build_new_memory_rejects_empty_title(engine: MemoryEngine):
    with pytest.raises(ValidationError):
        engine.build_new_memory(NewMemoryData(title="   "))


def test_build_new_memory_rejects_title_too_long(engine: MemoryEngine):
    with pytest.raises(ValidationError):
        engine.build_new_memory(NewMemoryData(title="x" * 51))


def test_build_new_memory_rejects_out_of_range_confidence(engine: MemoryEngine):
    with pytest.raises(ValidationError):
        engine.build_new_memory(NewMemoryData(title="ok", confidence_score=1.5))


def test_build_new_memory_rejects_out_of_range_importance(engine: MemoryEngine):
    with pytest.raises(ValidationError):
        engine.build_new_memory(NewMemoryData(title="ok", importance_score=-0.1))


def test_build_new_memory_normalizes_and_dedupes_tags(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="ok", tags=["Foo", "foo", " Bar "]))
    assert memory.tags == ["foo", "bar"]


def test_build_new_memory_rejects_too_many_tags(engine: MemoryEngine):
    with pytest.raises(ValidationError):
        engine.build_new_memory(NewMemoryData(title="ok", tags=["a", "b", "c", "d"]))


def test_build_new_memory_rejects_tag_too_long(engine: MemoryEngine):
    with pytest.raises(ValidationError):
        engine.build_new_memory(NewMemoryData(title="ok", tags=["x" * 11]))


def test_build_new_memory_drops_unserializable_metadata(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="ok", metadata={"bad": {1, 2, 3}}))
    assert memory.metadata == {}


def test_build_new_memory_rejects_unknown_memory_type_string(engine: MemoryEngine):
    with pytest.raises(ValidationError):
        engine.build_new_memory(NewMemoryData(title="ok", memory_type="not-a-real-type"))  # type: ignore[arg-type]


def test_apply_update_bumps_changed_fields_and_snapshots_previous_state(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="Original", summary="S1"))
    plan = engine.apply_update(memory, MemoryUpdateData(title="Updated"))
    assert plan.changed_field_names == ["title"]
    assert plan.changed_fields == {"title": "Updated"}
    assert plan.version_snapshot.title == "Original"
    assert plan.version_snapshot.version_number == 1
    assert not plan.is_noop


def test_apply_update_only_touches_mentioned_fields(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="Original", summary="Keep me"))
    plan = engine.apply_update(memory, MemoryUpdateData(title="Updated"))
    assert "summary" not in plan.changed_fields


def test_apply_update_is_noop_when_nothing_actually_changes(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="Same"))
    plan = engine.apply_update(memory, MemoryUpdateData(title="Same"))
    assert plan.is_noop
    assert plan.changed_fields == {}


def test_apply_update_validates_new_values(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="ok"))
    with pytest.raises(ValidationError):
        engine.apply_update(memory, MemoryUpdateData(importance_score=2.0))


def test_apply_update_can_clear_nullable_field_to_none(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="ok", source="original-source"))
    plan = engine.apply_update(memory, MemoryUpdateData(source=None))
    assert plan.changed_fields == {"source": None}
    assert not plan.is_noop


@pytest.mark.parametrize(
    "current,target,should_raise",
    [
        (MemoryStatus.ACTIVE, MemoryStatus.ARCHIVED, False),
        (MemoryStatus.ACTIVE, MemoryStatus.DELETED, False),
        (MemoryStatus.ARCHIVED, MemoryStatus.ACTIVE, False),
        (MemoryStatus.ARCHIVED, MemoryStatus.DELETED, False),
        (MemoryStatus.DELETED, MemoryStatus.ACTIVE, False),
        (MemoryStatus.DELETED, MemoryStatus.ARCHIVED, True),
    ],
)
def test_status_transition_rules(engine: MemoryEngine, current, target, should_raise):
    if should_raise:
        with pytest.raises(ValidationError):
            engine.validate_status_transition(current, target)
    else:
        engine.validate_status_transition(current, target)  # should not raise


def test_apply_status_transition_produces_plan(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="ok"))
    plan = engine.apply_status_transition(memory, MemoryStatus.ARCHIVED, change_reason="archived")
    assert plan.changed_fields["status"] == MemoryStatus.ARCHIVED
    assert not plan.is_noop
    assert plan.version_snapshot.status == MemoryStatus.ACTIVE  # pre-transition state


def test_apply_status_transition_sets_deleted_at_on_delete(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="ok"))
    plan = engine.apply_status_transition(memory, MemoryStatus.DELETED, change_reason="soft_deleted")
    assert plan.changed_fields["deleted_at"] is not None


def test_apply_status_transition_clears_deleted_at_on_restore(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="ok"))
    memory.status = MemoryStatus.DELETED
    plan = engine.apply_status_transition(memory, MemoryStatus.ACTIVE, change_reason="restored")
    assert plan.changed_fields["deleted_at"] is None


def test_apply_status_transition_same_status_is_noop(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="ok"))
    plan = engine.apply_status_transition(memory, MemoryStatus.ACTIVE, change_reason="noop")
    assert plan.is_noop


def test_apply_status_transition_raises_on_illegal_transition(engine: MemoryEngine):
    memory = engine.build_new_memory(NewMemoryData(title="ok"))
    memory.status = MemoryStatus.DELETED
    with pytest.raises(ValidationError):
        engine.apply_status_transition(memory, MemoryStatus.ARCHIVED, change_reason="bad")
