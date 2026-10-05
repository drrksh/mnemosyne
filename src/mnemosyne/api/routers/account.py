"""
Account and administration endpoints: who am I, my API keys, data
portability and erasure, the audit trail, and (admins) users and teams.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, status
from pydantic import BaseModel, Field

from mnemosyne.api.dependencies import get_container, get_principal, require_admin
from mnemosyne.api.routers.learning import IngestResponse
from mnemosyne.container import Mnemosyne
from mnemosyne.domain.entities import ApiKey, User
from mnemosyne.domain.enums import TeamRole
from mnemosyne.domain.exceptions import ValidationError
from mnemosyne.domain.principal import Principal

router = APIRouter(prefix="/api/v1", tags=["account"])
admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

ERASE_CONFIRMATION = "ERASE-ALL-MY-PERSONAL-MEMORIES"


class MeResponse(BaseModel):
    user_id: str
    team_ids: list[str]
    maintained_team_ids: list[str]
    permissions: list[str]
    auth_mode: str


@router.get("/me", response_model=MeResponse)
def me(container: Mnemosyne = Depends(get_container), principal: Principal = Depends(get_principal)) -> MeResponse:
    return MeResponse(
        user_id=principal.user_id,
        team_ids=sorted(principal.team_ids),
        maintained_team_ids=sorted(principal.maintained_team_ids),
        permissions=sorted(principal.permissions),
        auth_mode=container.settings.auth_mode,
    )


@router.get("/me/export")
def export_memories(
    include_archived: bool = False,
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """Everything you can see, as a portable, re-importable bundle."""
    return container.portability.export(principal=principal, include_archived=include_archived)


@router.post("/me/import", response_model=IngestResponse)
def import_memories(
    bundle: dict[str, Any] = Body(...),
    team_id: Optional[str] = Query(default=None, description="Import into a team instead of personal memory."),
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> IngestResponse:
    return IngestResponse.from_report(container.portability.import_bundle(bundle, principal=principal,
                                                                          into_team=team_id))


@router.delete("/me/memories")
def erase_memories(
    confirm: str = Query(..., description=f"Must equal {ERASE_CONFIRMATION!r}."),
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> dict[str, int]:
    """Right to erasure: permanently delete every personal memory,
    including history, evidence and embeddings. Team memories remain."""
    if confirm != ERASE_CONFIRMATION:
        raise ValidationError(f"set confirm={ERASE_CONFIRMATION} to erase all personal memories")
    return {"erased": container.memories.erase_personal_memories(principal=principal)}


class KeyResponse(BaseModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: Optional[datetime]
    expires_at: Optional[datetime]
    revoked_at: Optional[datetime]
    key: Optional[str] = Field(default=None, description="The secret. Returned once, at creation only.")

    @classmethod
    def from_domain(cls, key: ApiKey, plaintext: Optional[str] = None) -> "KeyResponse":
        return cls(id=key.id, name=key.name, prefix=key.prefix, scopes=key.scopes, created_at=key.created_at,
                   last_used_at=key.last_used_at, expires_at=key.expires_at, revoked_at=key.revoked_at, key=plaintext)


class KeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    scopes: Optional[list[str]] = None
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


@router.get("/me/keys", response_model=list[KeyResponse])
def list_my_keys(container: Mnemosyne = Depends(get_container),
                 principal: Principal = Depends(get_principal)) -> list[KeyResponse]:
    return [KeyResponse.from_domain(k) for k in container.identity.list_api_keys(principal.user_id, actor=principal)]


@router.post("/me/keys", response_model=KeyResponse, status_code=status.HTTP_201_CREATED)
def create_my_key(payload: KeyRequest, container: Mnemosyne = Depends(get_container),
                  principal: Principal = Depends(get_principal)) -> KeyResponse:
    issued = container.identity.issue_api_key(
        principal.user_id, name=payload.name, scopes=payload.scopes, expires_in_days=payload.expires_in_days,
        actor=principal,
    )
    return KeyResponse.from_domain(issued.key, issued.plaintext)


@router.delete("/me/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_my_key(key_id: str, container: Mnemosyne = Depends(get_container),
                  principal: Principal = Depends(get_principal)) -> None:
    container.identity.revoke_api_key(key_id, actor=principal)


class AuditEventResponse(BaseModel):
    id: str
    occurred_at: datetime
    actor_id: str
    action: str
    target_type: str
    target_id: Optional[str]
    request_id: Optional[str]
    details: dict[str, Any]


@router.get("/audit", response_model=list[AuditEventResponse])
def audit_log(
    actor_id: Optional[str] = Query(default=None, description="Admins only: another user's events."),
    action_prefix: Optional[str] = None,
    target_id: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    container: Mnemosyne = Depends(get_container),
    principal: Principal = Depends(get_principal),
) -> list[AuditEventResponse]:
    """Your own audit trail; administrators may query any actor (or all)."""
    actor = actor_id if principal.is_admin else principal.user_id
    with container.uow_factory() as uow:
        events = uow.audit.list(actor_id=actor, action_prefix=action_prefix, target_id=target_id, since=since,
                                limit=limit)
        uow.commit()
    return [AuditEventResponse(id=str(e.id), occurred_at=e.occurred_at, actor_id=e.actor_id, action=e.action,
                               target_type=e.target_type, target_id=e.target_id, request_id=e.request_id,
                               details=e.details) for e in events]


# ----------------------------------------------------------------------
# Administration
# ----------------------------------------------------------------------
class UserRequest(BaseModel):
    handle: str
    display_name: str = ""
    is_admin: bool = False


class UserResponse(BaseModel):
    id: str
    handle: str
    display_name: str
    is_admin: bool
    disabled: bool
    created_at: datetime

    @classmethod
    def from_domain(cls, user: User) -> "UserResponse":
        return cls(id=user.id, handle=user.handle, display_name=user.display_name, is_admin=user.is_admin,
                   disabled=user.disabled, created_at=user.created_at)


@admin_router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserRequest, container: Mnemosyne = Depends(get_container),
                admin: Principal = Depends(require_admin)) -> UserResponse:
    return UserResponse.from_domain(container.identity.create_user(
        payload.handle, display_name=payload.display_name, is_admin=payload.is_admin, actor=admin))


@admin_router.get("/users", response_model=list[UserResponse])
def list_users(container: Mnemosyne = Depends(get_container),
               admin: Principal = Depends(require_admin)) -> list[UserResponse]:
    return [UserResponse.from_domain(u) for u in container.identity.list_users(actor=admin)]


@admin_router.post("/users/{user_id}/disable", response_model=UserResponse)
def disable_user(user_id: str, container: Mnemosyne = Depends(get_container),
                 admin: Principal = Depends(require_admin)) -> UserResponse:
    return UserResponse.from_domain(container.identity.set_user_disabled(user_id, True, actor=admin))


@admin_router.post("/users/{user_id}/enable", response_model=UserResponse)
def enable_user(user_id: str, container: Mnemosyne = Depends(get_container),
                admin: Principal = Depends(require_admin)) -> UserResponse:
    return UserResponse.from_domain(container.identity.set_user_disabled(user_id, False, actor=admin))


@admin_router.post("/users/{user_id}/keys", response_model=KeyResponse, status_code=status.HTTP_201_CREATED)
def issue_key(user_id: str, payload: KeyRequest, container: Mnemosyne = Depends(get_container),
              admin: Principal = Depends(require_admin)) -> KeyResponse:
    issued = container.identity.issue_api_key(user_id, name=payload.name, scopes=payload.scopes,
                                              expires_in_days=payload.expires_in_days, actor=admin)
    return KeyResponse.from_domain(issued.key, issued.plaintext)


class TeamRequest(BaseModel):
    name: str


class TeamResponse(BaseModel):
    id: str
    name: str
    created_at: datetime


@admin_router.post("/teams", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
def create_team(payload: TeamRequest, container: Mnemosyne = Depends(get_container),
                admin: Principal = Depends(require_admin)) -> TeamResponse:
    team = container.identity.create_team(payload.name, actor=admin)
    return TeamResponse(id=team.id, name=team.name, created_at=team.created_at)


@admin_router.get("/teams", response_model=list[TeamResponse])
def list_teams(container: Mnemosyne = Depends(get_container),
               admin: Principal = Depends(require_admin)) -> list[TeamResponse]:
    return [TeamResponse(id=t.id, name=t.name, created_at=t.created_at) for t in container.identity.list_teams()]


class MembershipRequest(BaseModel):
    role: TeamRole = TeamRole.MEMBER


@admin_router.put("/teams/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def add_member(team_id: str, user_id: str, payload: MembershipRequest, container: Mnemosyne = Depends(get_container),
               admin: Principal = Depends(require_admin)) -> None:
    container.identity.add_member(team_id, user_id, payload.role, actor=admin)


@admin_router.delete("/teams/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(team_id: str, user_id: str, container: Mnemosyne = Depends(get_container),
                  admin: Principal = Depends(require_admin)) -> None:
    container.identity.remove_member(team_id, user_id, actor=admin)
