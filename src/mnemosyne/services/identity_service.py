"""
IdentityService: users, teams, API keys, and turning a credential into
a Principal.

API keys look like `mn_<prefix>_<secret>`. Only a SHA-256 hash of the
full key is stored; the plaintext is shown exactly once, at creation.
The prefix is stored in clear so a key can be found (and identified in
logs and UIs) without ever handling the secret. Because the secret is
256 bits of CSPRNG output, a fast hash is appropriate here — there is no
low-entropy password to protect against offline guessing.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from mnemosyne.db.unit_of_work import UnitOfWork
from mnemosyne.domain.entities import ApiKey, Team, TeamMembership, User
from mnemosyne.domain.enums import TeamRole
from mnemosyne.domain.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from mnemosyne.domain.principal import Permission, Principal
from mnemosyne.services import audit
from mnemosyne.utils.time import utcnow

logger = logging.getLogger("mnemosyne.services.identity")

UowFactory = Callable[[], UnitOfWork]

KEY_PREFIX = "mn"
_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_TEAM_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_KEY_RE = re.compile(r"^mn_([a-f0-9]{12})_([A-Za-z0-9_-]{20,})$")
#: Avoid a write on every authenticated request.
_TOUCH_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class IssuedKey:
    key: ApiKey
    #: Shown once; never stored.
    plaintext: str


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class IdentityService:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    # ------------------------------------------------------------------
    # Principals
    # ------------------------------------------------------------------
    def principal_for(self, user_id: str, permissions: Optional[frozenset[str]] = None) -> Principal:
        """Principal for a known user id. Without explicit `permissions`
        the user gets ordinary read/write, plus `admin` only if the user
        record is an administrator."""
        with self._uow_factory() as uow:
            memberships = uow.identity.memberships_for_user(user_id)
            user = uow.identity.get_user(user_id) if permissions is None else None
            uow.commit()
        if permissions is None:
            permissions = Permission.DEFAULT_USER | ({Permission.ADMIN} if user and user.is_admin else set())
        return Principal(
            user_id=user_id,
            team_ids=frozenset(m.team_id for m in memberships),
            maintained_team_ids=frozenset(m.team_id for m in memberships if m.role == TeamRole.MAINTAINER),
            permissions=permissions,
        )

    def authenticate(self, raw_key: str) -> Principal:
        match = _KEY_RE.match((raw_key or "").strip())
        if not match:
            raise AuthenticationError("invalid API key")
        now = utcnow()
        with self._uow_factory() as uow:
            key = uow.identity.get_api_key_by_prefix(match.group(1))
            # Compare against a dummy hash when the prefix is unknown so the
            # response time does not reveal which prefixes exist.
            expected = key.key_hash if key else _hash_key("mn_000000000000_dummy-dummy-dummy-dummy")
            valid = hmac.compare_digest(expected, _hash_key(raw_key.strip()))
            if key is None or not valid or not key.is_usable(now):
                raise AuthenticationError("invalid API key")
            user = uow.identity.get_user(key.user_id)
            if user is None or user.disabled:
                raise AuthenticationError("invalid API key")
            if key.last_used_at is None or now - key.last_used_at > _TOUCH_INTERVAL:
                uow.identity.touch_api_key(key.id, now)
            memberships = uow.identity.memberships_for_user(user.id)
            uow.commit()

        permissions = set(key.scopes) & set(Permission.ALL)
        if Permission.ADMIN in permissions and not user.is_admin:
            permissions.discard(Permission.ADMIN)
        return Principal(
            user_id=user.id,
            team_ids=frozenset(m.team_id for m in memberships),
            maintained_team_ids=frozenset(m.team_id for m in memberships if m.role == TeamRole.MAINTAINER),
            permissions=frozenset(permissions),
        )

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    def ensure_user(self, user_id: str, *, handle: Optional[str] = None, is_admin: bool = False) -> User:
        """Idempotently create a user with a fixed id (used for the local
        single-user principal)."""
        with self._uow_factory() as uow:
            user = uow.identity.get_user(user_id)
            if user is None:
                user = User(
                    id=user_id,
                    handle=handle or user_id,
                    display_name=handle or user_id,
                    is_admin=is_admin,
                    created_at=utcnow(),
                )
                uow.identity.add_user(user)
            uow.commit()
        return user

    def create_user(self, handle: str, *, display_name: str = "", is_admin: bool = False,
                    actor: Optional[Principal] = None) -> User:
        self._require_admin(actor)
        handle = handle.strip().lower()
        if not _HANDLE_RE.match(handle):
            raise ValidationError("handle must be 2-64 chars of lowercase letters, digits, '.', '_' or '-'")
        user = User(
            id="usr_" + secrets.token_hex(8),
            handle=handle,
            display_name=display_name or handle,
            is_admin=is_admin,
            created_at=utcnow(),
        )
        with self._uow_factory() as uow:
            if uow.identity.get_user_by_handle(handle):
                raise ConflictError(f"user {handle!r} already exists")
            uow.identity.add_user(user)
            if actor:
                audit.record(uow, actor, "user.create", target_type="user", target_id=user.id, is_admin=is_admin)
            uow.commit()
        return user

    def get_user(self, user_id: str) -> User:
        with self._uow_factory() as uow:
            user = uow.identity.get_user(user_id) or uow.identity.get_user_by_handle(user_id)
            uow.commit()
        if user is None:
            raise NotFoundError(f"user {user_id!r} not found")
        return user

    def list_users(self, *, actor: Optional[Principal] = None) -> list[User]:
        self._require_admin(actor)
        with self._uow_factory() as uow:
            users = uow.identity.list_users()
            uow.commit()
        return users

    def set_user_disabled(self, user_id: str, disabled: bool, *, actor: Optional[Principal] = None) -> User:
        self._require_admin(actor)
        user = self.get_user(user_id)
        with self._uow_factory() as uow:
            uow.identity.set_user_disabled(user.id, disabled)
            if actor:
                audit.record(uow, actor, "user.disable" if disabled else "user.enable", target_type="user",
                             target_id=user.id)
            uow.commit()
        return self.get_user(user.id)

    # ------------------------------------------------------------------
    # Teams
    # ------------------------------------------------------------------
    def create_team(self, name: str, *, actor: Optional[Principal] = None) -> Team:
        self._require_admin(actor)
        name = name.strip().lower()
        if not _TEAM_RE.match(name):
            raise ValidationError("team name must be 2-64 chars of lowercase letters, digits, '.', '_' or '-'")
        team = Team(id="team_" + secrets.token_hex(8), name=name, created_at=utcnow())
        with self._uow_factory() as uow:
            if uow.identity.get_team_by_name(name):
                raise ConflictError(f"team {name!r} already exists")
            uow.identity.add_team(team)
            if actor:
                audit.record(uow, actor, "team.create", target_type="team", target_id=team.id)
            uow.commit()
        return team

    def get_team(self, team_id_or_name: str) -> Team:
        with self._uow_factory() as uow:
            team = uow.identity.get_team(team_id_or_name) or uow.identity.get_team_by_name(team_id_or_name)
            uow.commit()
        if team is None:
            raise NotFoundError(f"team {team_id_or_name!r} not found")
        return team

    def list_teams(self) -> list[Team]:
        with self._uow_factory() as uow:
            teams = uow.identity.list_teams()
            uow.commit()
        return teams

    def add_member(self, team_id: str, user_id: str, role: TeamRole = TeamRole.MEMBER, *,
                   actor: Optional[Principal] = None) -> TeamMembership:
        self._require_admin(actor)
        team = self.get_team(team_id)
        user = self.get_user(user_id)
        membership = TeamMembership(team_id=team.id, user_id=user.id, role=role, joined_at=utcnow())
        with self._uow_factory() as uow:
            uow.identity.upsert_membership(membership)
            if actor:
                audit.record(uow, actor, "team.add_member", target_type="team", target_id=team.id,
                             user_id=user.id, role=role.value)
            uow.commit()
        return membership

    def remove_member(self, team_id: str, user_id: str, *, actor: Optional[Principal] = None) -> bool:
        self._require_admin(actor)
        team = self.get_team(team_id)
        user = self.get_user(user_id)
        with self._uow_factory() as uow:
            removed = uow.identity.remove_membership(team.id, user.id)
            if actor and removed:
                audit.record(uow, actor, "team.remove_member", target_type="team", target_id=team.id, user_id=user.id)
            uow.commit()
        return removed

    def members(self, team_id: str) -> list[TeamMembership]:
        team = self.get_team(team_id)
        with self._uow_factory() as uow:
            members = uow.identity.members_of_team(team.id)
            uow.commit()
        return members

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------
    def issue_api_key(
        self,
        user_id: str,
        *,
        name: str,
        scopes: Optional[list[str]] = None,
        expires_in_days: Optional[int] = None,
        actor: Optional[Principal] = None,
    ) -> IssuedKey:
        user = self.get_user(user_id)
        if actor is not None and actor.user_id != user.id:
            self._require_admin(actor)
        requested = set(scopes or Permission.DEFAULT_USER)
        unknown = requested - set(Permission.ALL)
        if unknown:
            raise ValidationError(f"unknown scopes: {sorted(unknown)}")
        if Permission.ADMIN in requested and not user.is_admin:
            raise ValidationError("only admin users can hold the 'admin' scope")
        if actor is not None and not actor.is_admin and not requested <= set(actor.permissions):
            raise PermissionDeniedError("cannot issue a key with more permissions than your own")

        prefix = secrets.token_hex(6)
        plaintext = f"{KEY_PREFIX}_{prefix}_{secrets.token_urlsafe(32)}"
        now = utcnow()
        key = ApiKey(
            id="key_" + secrets.token_hex(8),
            user_id=user.id,
            name=(name or "default").strip()[:100],
            prefix=prefix,
            key_hash=_hash_key(plaintext),
            scopes=sorted(requested),
            created_at=now,
            expires_at=now + timedelta(days=expires_in_days) if expires_in_days else None,
        )
        with self._uow_factory() as uow:
            uow.identity.add_api_key(key)
            if actor:
                audit.record(uow, actor, "api_key.issue", target_type="api_key", target_id=key.id,
                             user_id=user.id, scopes=key.scopes)
            uow.commit()
        return IssuedKey(key=key, plaintext=plaintext)

    def list_api_keys(self, user_id: str, *, actor: Optional[Principal] = None) -> list[ApiKey]:
        user = self.get_user(user_id)
        if actor is not None and actor.user_id != user.id:
            self._require_admin(actor)
        with self._uow_factory() as uow:
            keys = uow.identity.list_api_keys(user.id)
            uow.commit()
        return keys

    def revoke_api_key(self, key_id: str, *, actor: Optional[Principal] = None) -> bool:
        with self._uow_factory() as uow:
            key = uow.identity.get_api_key(key_id)
            if key is None:
                raise NotFoundError(f"API key {key_id!r} not found")
            if actor is not None and actor.user_id != key.user_id and not actor.is_admin:
                raise NotFoundError(f"API key {key_id!r} not found")
            revoked = uow.identity.revoke_api_key(key_id, utcnow())
            if actor:
                audit.record(uow, actor, "api_key.revoke", target_type="api_key", target_id=key_id)
            uow.commit()
        return revoked

    # ------------------------------------------------------------------
    @staticmethod
    def _require_admin(actor: Optional[Principal]) -> None:
        """`actor=None` means a trusted local caller (the CLI acting
        directly on the database), which is allowed everything."""
        if actor is not None and not actor.is_admin:
            raise PermissionDeniedError("administrator permission required")


def key_expired(key: ApiKey, now: Optional[datetime] = None) -> bool:
    return not key.is_usable(now or utcnow())
