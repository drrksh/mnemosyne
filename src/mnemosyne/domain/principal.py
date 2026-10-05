"""
Principal: *who* is asking.

Every service call acts on behalf of a Principal. Visibility is a pure
function of the principal and the memory:

* a personal memory (`team_id is None`) is visible only to its owner;
* a team memory is visible to every member of that team.

Keeping this rule in the domain layer (rather than scattered across SQL
filters and route handlers) is what lets the repository, the retrieval
index, and the API all enforce exactly the same tenancy boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from mnemosyne.domain.entities import Memory


class Permission:
    READ = "memories:read"
    WRITE = "memories:write"
    ADMIN = "admin"

    ALL: frozenset[str] = frozenset({READ, WRITE, ADMIN})
    DEFAULT_USER: frozenset[str] = frozenset({READ, WRITE})


@dataclass(frozen=True)
class Principal:
    user_id: str
    team_ids: frozenset[str] = field(default_factory=frozenset)
    #: Teams in which this principal is a maintainer (may hard-delete).
    maintained_team_ids: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = Permission.DEFAULT_USER

    # ------------------------------------------------------------------
    @property
    def is_admin(self) -> bool:
        return Permission.ADMIN in self.permissions

    def has(self, permission: str) -> bool:
        return permission in self.permissions or self.is_admin

    def can_see(self, memory: Memory) -> bool:
        if memory.team_id is None:
            return memory.owner_id == self.user_id
        return memory.team_id in self.team_ids

    def can_write_to(self, team_id: Optional[str]) -> bool:
        if not self.has(Permission.WRITE):
            return False
        return team_id is None or team_id in self.team_ids

    def can_hard_delete(self, memory: Memory) -> bool:
        if not self.has(Permission.WRITE):
            return False
        if memory.team_id is None:
            return memory.owner_id == self.user_id
        return memory.team_id in self.maintained_team_ids or self.is_admin

    @property
    def visibility(self) -> Visibility:
        return Visibility(owner_id=self.user_id, team_ids=self.team_ids)


@dataclass(frozen=True)
class Visibility:
    """The storage-level projection of a Principal: the filter every
    memory query applies. Hashable, so it doubles as a cache key for the
    retrieval index."""

    owner_id: str
    team_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def cache_key(self) -> str:
        return self.owner_id + "|" + ",".join(sorted(self.team_ids))


#: The single-user principal used in local mode (CLI, MCP, hooks, and the
#: REST API when `auth_mode=local`). It owns every personal memory in a
#: local installation.
LOCAL_USER_ID = "local"
LOCAL_PRINCIPAL = Principal(user_id=LOCAL_USER_ID, permissions=Permission.ALL)


def local_principal(user_id: str = LOCAL_USER_ID) -> Principal:
    return Principal(user_id=user_id, permissions=Permission.ALL)
