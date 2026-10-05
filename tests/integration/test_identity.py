"""Users, teams, and API-key authentication."""
from __future__ import annotations

from datetime import timedelta

import pytest

from mnemosyne.domain.exceptions import AuthenticationError, ConflictError, PermissionDeniedError, ValidationError
from mnemosyne.domain.principal import Permission, Principal
from mnemosyne.utils.time import utcnow


def test_issued_key_authenticates_to_its_user(container):
    user = container.identity.create_user("dana")
    issued = container.identity.issue_api_key(user.id, name="laptop")

    assert issued.plaintext.startswith("mn_")
    principal = container.identity.authenticate(issued.plaintext)
    assert principal.user_id == user.id
    assert principal.permissions == Permission.DEFAULT_USER


def test_key_secret_is_never_stored(container):
    user = container.identity.create_user("erin")
    issued = container.identity.issue_api_key(user.id, name="ci")
    secret = issued.plaintext.split("_", 2)[2]
    keys = container.identity.list_api_keys(user.id)
    assert all(secret not in k.key_hash and secret not in k.prefix for k in keys)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda key: key[:-1] + ("A" if key[-1] != "A" else "B"),  # wrong secret
        lambda key: "mn_ffffffffffff_" + key.split("_", 2)[2],  # unknown prefix
        lambda key: "not-a-key",
        lambda key: "",
    ],
)
def test_invalid_keys_are_rejected(container, mutate):
    user = container.identity.create_user("frank")
    issued = container.identity.issue_api_key(user.id, name="x")
    with pytest.raises(AuthenticationError):
        container.identity.authenticate(mutate(issued.plaintext))


def test_revoked_and_expired_keys_are_rejected(container):
    user = container.identity.create_user("gina")
    revoked = container.identity.issue_api_key(user.id, name="old")
    container.identity.revoke_api_key(revoked.key.id)
    with pytest.raises(AuthenticationError):
        container.identity.authenticate(revoked.plaintext)

    expiring = container.identity.issue_api_key(user.id, name="short", expires_in_days=1)
    with container.uow_factory() as uow:
        from sqlalchemy import update

        from mnemosyne.repositories.sqlite.orm_models import ApiKeyORM

        uow.session.execute(
            update(ApiKeyORM).where(ApiKeyORM.id == expiring.key.id).values(expires_at=utcnow() - timedelta(seconds=1))
        )
        uow.commit()
    with pytest.raises(AuthenticationError):
        container.identity.authenticate(expiring.plaintext)


def test_disabled_users_cannot_authenticate(container):
    user = container.identity.create_user("hank")
    issued = container.identity.issue_api_key(user.id, name="x")
    container.identity.set_user_disabled(user.id, True)
    with pytest.raises(AuthenticationError):
        container.identity.authenticate(issued.plaintext)


def test_admin_scope_requires_admin_user(container):
    user = container.identity.create_user("ivan")
    with pytest.raises(ValidationError):
        container.identity.issue_api_key(user.id, name="x", scopes=[Permission.ADMIN])
    admin = container.identity.create_user("root", is_admin=True)
    issued = container.identity.issue_api_key(admin.id, name="admin", scopes=[Permission.ADMIN, Permission.READ])
    assert container.identity.authenticate(issued.plaintext).is_admin


def test_read_only_scope_is_enforced_in_principal(container):
    user = container.identity.create_user("jill")
    issued = container.identity.issue_api_key(user.id, name="ro", scopes=[Permission.READ])
    principal = container.identity.authenticate(issued.plaintext)
    assert principal.has(Permission.READ)
    assert not principal.has(Permission.WRITE)


def test_non_admins_cannot_manage_users_or_teams(container):
    actor = Principal(user_id="nobody")
    with pytest.raises(PermissionDeniedError):
        container.identity.create_user("x1", actor=actor)
    with pytest.raises(PermissionDeniedError):
        container.identity.create_team("t1", actor=actor)


def test_users_cannot_escalate_via_self_issued_keys(container):
    user = container.identity.create_user("kate")
    reader = Principal(user_id=user.id, permissions=frozenset({Permission.READ}))
    with pytest.raises(PermissionDeniedError):
        container.identity.issue_api_key(user.id, name="x", scopes=[Permission.READ, Permission.WRITE], actor=reader)


def test_duplicate_handles_and_team_names_conflict(container):
    container.identity.create_user("lee")
    with pytest.raises(ConflictError):
        container.identity.create_user("lee")
    container.identity.create_team("core")
    with pytest.raises(ConflictError):
        container.identity.create_team("core")


def test_principal_includes_team_memberships(container):
    from mnemosyne.domain.enums import TeamRole

    container.identity.ensure_user("mia")
    team = container.identity.create_team("web")
    container.identity.add_member(team.id, "mia", TeamRole.MAINTAINER)
    principal = container.identity.principal_for("mia")
    assert principal.team_ids == {team.id}
    assert principal.maintained_team_ids == {team.id}
