"""Audit trail atomicity and access-tracking semantics."""
from __future__ import annotations

import pytest
from tests.conftest import make_new_memory

from mnemosyne.domain.dto import MemoryUpdateData
from mnemosyne.domain.exceptions import ValidationError
from mnemosyne.domain.principal import Principal

ALICE = Principal(user_id="alice")


def _audit(container, **filters):
    with container.uow_factory() as uow:
        events = uow.audit.list(**filters)
        uow.commit()
    return events


def test_writes_are_audited_with_actor_and_target(container):
    memory = container.memories.create_memory(make_new_memory(), principal=ALICE)
    container.memories.update_memory(memory.id, MemoryUpdateData(title="New"), principal=ALICE)
    container.memories.archive_memory(memory.id, principal=ALICE)

    actions = [e.action for e in _audit(container, actor_id="alice")]
    assert actions == ["memory.archived", "memory.update", "memory.create"]
    events = _audit(container, target_id=str(memory.id))
    assert {e.actor_id for e in events} == {"alice"}
    update = next(e for e in events if e.action == "memory.update")
    assert update.details["changed_fields"] == ["title"]


def test_audit_never_contains_memory_content(container):
    container.memories.create_memory(make_new_memory(title="TOP SECRET TITLE", content="classified"), principal=ALICE)
    for event in _audit(container):
        assert "TOP SECRET" not in str(event.details)
        assert "classified" not in str(event.details)


def test_failed_write_leaves_no_audit_event(container):
    memory = container.memories.create_memory(make_new_memory(), principal=ALICE)
    before = len(_audit(container))
    with pytest.raises(ValidationError):
        container.memories.update_memory(memory.id, MemoryUpdateData(title=""), principal=ALICE)
    assert len(_audit(container)) == before


def test_reading_a_memory_does_not_bump_its_version(container):
    """Regression: in Phase 1, access tracking went through the ORM's
    version counter, so a plain GET created a new 'version' with no
    history row — version 1 then vanished from history, and concurrent
    readers caused spurious write conflicts."""
    memory = container.memories.create_memory(make_new_memory(title="v1 title"), principal=ALICE)
    read = container.memories.get_memory(memory.id, principal=ALICE)
    read_again = container.memories.get_memory(memory.id, principal=ALICE)

    assert read.version == 1 and read_again.version == 1
    assert read_again.access_count == 2

    container.memories.update_memory(memory.id, MemoryUpdateData(title="v2 title"), expected_version=1, principal=ALICE)
    assert container.memories.get_version(memory.id, 1, principal=ALICE).title == "v1 title"
    assert [v.version_number for v in container.memories.list_versions(memory.id, principal=ALICE)] == [1]


def test_memories_are_embedded_on_write_and_reembedded_on_content_change(container):
    memory = container.memories.create_memory(make_new_memory(title="Original wording"), principal=ALICE)
    with container.uow_factory() as uow:
        first = uow.embeddings.get_many([memory.id], model=container.indexer.model_id)[memory.id]
        uow.commit()

    container.memories.update_memory(memory.id, MemoryUpdateData(title="Completely different words"), principal=ALICE)
    with container.uow_factory() as uow:
        second = uow.embeddings.get_many([memory.id], model=container.indexer.model_id)[memory.id]
        uow.commit()
    assert first.text_hash != second.text_hash
    assert first.vector != second.vector


def test_hard_delete_cascades_to_embeddings_and_evidence(container):
    from uuid import uuid4

    from mnemosyne.domain.entities import Evidence
    from mnemosyne.domain.enums import EvidenceKind
    from mnemosyne.utils.time import utcnow

    memory = container.memories.create_memory(make_new_memory(), principal=ALICE)
    with container.uow_factory() as uow:
        uow.evidence.add(Evidence(uuid4(), memory.id, EvidenceKind.EXPLICIT, "test:1", "x", 1.0, utcnow()))
        uow.commit()
    container.memories.hard_delete_memory(memory.id, principal=ALICE)
    with container.uow_factory() as uow:
        assert uow.embeddings.get_many([memory.id], model=container.indexer.model_id) == {}
        assert uow.evidence.list_for_memory(memory.id) == []
        uow.commit()
