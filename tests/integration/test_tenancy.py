"""Tenancy isolation: one user's memories must never be visible to, or
modifiable by, another user — through any service operation, including
the retrieval index. Team memories are shared exactly with team members."""
from __future__ import annotations

import pytest
from tests.conftest import make_new_memory

from mnemosyne.domain.dto import MemoryUpdateData, SearchCriteria
from mnemosyne.domain.enums import TeamRole
from mnemosyne.domain.exceptions import NotFoundError, PermissionDeniedError
from mnemosyne.domain.principal import Permission, Principal
from mnemosyne.services.retrieval_service import RecallRequest

ALICE = Principal(user_id="alice")
BOB = Principal(user_id="bob")


def test_personal_memories_are_invisible_to_other_users(container):
    memory = container.memories.create_memory(make_new_memory(title="Alice secret"), principal=ALICE)

    assert container.memories.get_memory(memory.id, principal=ALICE).title == "Alice secret"
    with pytest.raises(NotFoundError):
        container.memories.get_memory(memory.id, principal=BOB)
    with pytest.raises(NotFoundError):
        container.memories.list_versions(memory.id, principal=BOB)
    with pytest.raises(NotFoundError):
        container.memories.list_evidence(memory.id, principal=BOB)
    with pytest.raises(NotFoundError):
        container.memories.get_version(memory.id, 1, principal=BOB)


def test_other_users_cannot_modify_or_delete(container):
    memory = container.memories.create_memory(make_new_memory(title="Mine"), principal=ALICE)
    with pytest.raises(NotFoundError):
        container.memories.update_memory(memory.id, MemoryUpdateData(title="Hijacked"), principal=BOB)
    with pytest.raises(NotFoundError):
        container.memories.archive_memory(memory.id, principal=BOB)
    with pytest.raises(NotFoundError):
        container.memories.soft_delete_memory(memory.id, principal=BOB)
    with pytest.raises(NotFoundError):
        container.memories.hard_delete_memory(memory.id, principal=BOB)
    assert container.memories.get_memory(memory.id, principal=ALICE).title == "Mine"


def test_search_is_scoped_to_principal_even_if_caller_sets_visibility(container):
    container.memories.create_memory(make_new_memory(title="alpha note"), principal=ALICE)
    container.memories.create_memory(make_new_memory(title="alpha other"), principal=BOB)

    criteria = SearchCriteria(query="alpha", visibility=ALICE.visibility)  # attempt to read Alice's data
    result = container.memories.search_memories(criteria, principal=BOB)
    assert [m.title for m in result.items] == ["alpha other"]


def test_retrieval_index_is_scoped_to_principal(container):
    container.memories.create_memory(make_new_memory(title="Use pnpm for packages"), principal=ALICE)
    container.memories.create_memory(make_new_memory(title="Use yarn for packages"), principal=BOB)

    alice = container.retrieval.recall(RecallRequest(query="packages"), principal=ALICE)
    bob = container.retrieval.recall(RecallRequest(query="packages"), principal=BOB)
    assert [s.memory.title for s in alice.items] == ["Use pnpm for packages"]
    assert [s.memory.title for s in bob.items] == ["Use yarn for packages"]


def _team_setup(container):
    container.identity.ensure_user("alice")
    container.identity.ensure_user("bob")
    container.identity.ensure_user("carol")
    team = container.identity.create_team("platform")
    container.identity.add_member(team.id, "alice", TeamRole.MAINTAINER)
    container.identity.add_member(team.id, "bob", TeamRole.MEMBER)
    return team


def test_team_memories_are_shared_with_members_only(container):
    team = _team_setup(container)
    alice = container.identity.principal_for("alice")
    bob = container.identity.principal_for("bob")
    carol = container.identity.principal_for("carol")

    memory = container.memories.create_memory(
        make_new_memory(title="Team convention: squash merge", team_id=team.id), principal=alice
    )
    assert memory.team_id == team.id
    assert container.memories.get_memory(memory.id, principal=bob).title.startswith("Team convention")
    with pytest.raises(NotFoundError):
        container.memories.get_memory(memory.id, principal=carol)

    updated = container.memories.update_memory(memory.id, MemoryUpdateData(summary="edited by bob"), principal=bob)
    assert updated.summary == "edited by bob"


def test_non_members_cannot_create_team_memories(container):
    team = _team_setup(container)
    carol = container.identity.principal_for("carol")
    with pytest.raises(NotFoundError):
        container.memories.create_memory(make_new_memory(team_id=team.id), principal=carol)


def test_only_maintainers_can_hard_delete_team_memories(container):
    team = _team_setup(container)
    alice = container.identity.principal_for("alice")
    bob = container.identity.principal_for("bob")
    memory = container.memories.create_memory(make_new_memory(team_id=team.id), principal=bob)

    with pytest.raises(PermissionDeniedError):
        container.memories.hard_delete_memory(memory.id, principal=bob)
    container.memories.hard_delete_memory(memory.id, principal=alice)
    with pytest.raises(NotFoundError):
        container.memories.get_memory(memory.id, principal=alice)


def test_read_only_principal_cannot_write(container):
    reader = Principal(user_id="alice", permissions=frozenset({Permission.READ}))
    with pytest.raises(PermissionDeniedError):
        container.memories.create_memory(make_new_memory(), principal=reader)
    memory = container.memories.create_memory(make_new_memory(), principal=ALICE)
    with pytest.raises(PermissionDeniedError):
        container.memories.update_memory(memory.id, MemoryUpdateData(title="x"), principal=reader)
    assert container.memories.get_memory(memory.id, principal=reader).id == memory.id


def test_erasure_removes_personal_memories_but_keeps_team_memories(container):
    team = _team_setup(container)
    alice = container.identity.principal_for("alice")
    personal = container.memories.create_memory(make_new_memory(title="personal"), principal=alice)
    shared = container.memories.create_memory(make_new_memory(title="shared", team_id=team.id), principal=alice)
    other = container.memories.create_memory(make_new_memory(title="bob's"), principal=BOB)

    assert container.memories.erase_personal_memories(principal=alice) == 1

    with pytest.raises(NotFoundError):
        container.memories.get_memory(personal.id, principal=alice)
    assert container.memories.get_memory(shared.id, principal=alice).title == "shared"
    assert container.memories.get_memory(other.id, principal=BOB).title == "bob's"
