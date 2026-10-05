"""`mnemosyne admin ...` and `mnemosyne db ...` — server administration.

These act on the database configured on *this* machine (the server's,
when run on the server), so they work before any API key exists: this is
how the first administrator and key of a team deployment are created.
"""
from __future__ import annotations

from typing import Optional

import click

from mnemosyne.cli.common import CliState, bold, dim, ok, pass_state, warn

SCOPE_CHOICES = {"read": "memories:read", "write": "memories:write", "admin": "admin"}


def _user_id(state: CliState, user: str) -> str:
    from mnemosyne.domain.exceptions import NotFoundError

    for candidate in state.container.identity.list_users():
        if user in (candidate.id, candidate.handle):
            return candidate.id
    raise NotFoundError(f"no user with id or handle {user!r}")


@click.group()
def admin() -> None:
    """Manage users, API keys and teams (team deployments)."""


# --- users ---------------------------------------------------------------
@admin.group()
def user() -> None:
    """Users."""


@user.command(name="create")
@click.argument("handle")
@click.option("--name", "display_name", default="")
@click.option("--admin", "is_admin", is_flag=True)
@click.option("--key/--no-key", "with_key", default=True, help="Also issue an API key (shown once).")
@pass_state
def user_create(state: CliState, handle: str, display_name: str, is_admin: bool, with_key: bool) -> None:
    """Create a user (and, by default, their first API key)."""
    identity = state.container.identity
    created = identity.create_user(handle, display_name=display_name, is_admin=is_admin)
    click.echo(f"{ok('Created')} {created.handle} ({created.id}){' — administrator' if is_admin else ''}")
    if with_key:
        scopes = ["memories:read", "memories:write"] + (["admin"] if is_admin else [])
        issued = identity.issue_api_key(created.id, name="initial", scopes=scopes)
        click.echo(f"API key (shown once): {bold(issued.plaintext)}")


@user.command(name="list")
@pass_state
def user_list(state: CliState) -> None:
    for u in state.container.identity.list_users():
        flags = ", ".join(f for f, on in (("admin", u.is_admin), ("disabled", u.disabled)) if on)
        click.echo(f"{u.id}  {u.handle}" + (dim(f"  ({flags})") if flags else ""))


@user.command(name="disable")
@click.argument("user")
@pass_state
def user_disable(state: CliState, user: str) -> None:
    """Disable a user: their keys stop working immediately."""
    state.container.identity.set_user_disabled(_user_id(state, user), True)
    click.echo(f"Disabled {user}")


@user.command(name="enable")
@click.argument("user")
@pass_state
def user_enable(state: CliState, user: str) -> None:
    state.container.identity.set_user_disabled(_user_id(state, user), False)
    click.echo(f"Enabled {user}")


# --- keys ----------------------------------------------------------------
@admin.group()
def key() -> None:
    """API keys."""


@key.command(name="issue")
@click.argument("user")
@click.option("--name", default="cli")
@click.option("--scope", "scopes", multiple=True, type=click.Choice(sorted(SCOPE_CHOICES)),
              help="Repeatable; default read+write.")
@click.option("--expires-in-days", type=int)
@pass_state
def key_issue(state: CliState, user: str, name: str, scopes: tuple[str, ...], expires_in_days: Optional[int]) -> None:
    issued = state.container.identity.issue_api_key(
        _user_id(state, user), name=name, scopes=[SCOPE_CHOICES[s] for s in scopes] or None,
        expires_in_days=expires_in_days,
    )
    click.echo(f"{issued.key.id}  scopes {', '.join(issued.key.scopes)}")
    click.echo(f"API key (shown once): {bold(issued.plaintext)}")


@key.command(name="list")
@click.argument("user")
@pass_state
def key_list(state: CliState, user: str) -> None:
    for k in state.container.identity.list_api_keys(_user_id(state, user)):
        if k.revoked_at:
            status = "revoked"
        else:
            status = f"expires {k.expires_at.date().isoformat()}" if k.expires_at else "active"
        used = k.last_used_at.isoformat()[:19] if k.last_used_at else "never"
        detail = f"{status} · last used {used} · " + ", ".join(k.scopes)
        click.echo(f"{k.id}  mn_{k.prefix}_…  {k.name}  " + dim(detail))


@key.command(name="revoke")
@click.argument("key_id")
@pass_state
def key_revoke(state: CliState, key_id: str) -> None:
    revoked = state.container.identity.revoke_api_key(key_id)
    click.echo(f"Revoked {key_id}" if revoked else warn(f"{key_id} was already revoked or does not exist"))


# --- teams ---------------------------------------------------------------
@admin.group()
def team() -> None:
    """Teams: shared memory for a group of developers."""


@team.command(name="create")
@click.argument("name")
@pass_state
def team_create(state: CliState, name: str) -> None:
    created = state.container.identity.create_team(name)
    click.echo(f"{ok('Created')} team {created.name} ({created.id})")


@team.command(name="list")
@pass_state
def team_list(state: CliState) -> None:
    identity = state.container.identity
    for t in identity.list_teams():
        members = identity.members(t.id)
        click.echo(f"{t.id}  {t.name}  " + dim(f"{len(members)} members"))


@team.command(name="add")
@click.argument("team_name")
@click.argument("user")
@click.option("--role", type=click.Choice(["member", "maintainer"]), default="member", show_default=True)
@pass_state
def team_add(state: CliState, team_name: str, user: str, role: str) -> None:
    """Add a user to a team (maintainers curate team memories)."""
    from mnemosyne.domain.enums import TeamRole

    identity = state.container.identity
    team_obj = identity.get_team(team_name)
    identity.add_member(team_obj.id, _user_id(state, user), TeamRole(role))
    click.echo(f"Added {user} to {team_obj.name} as {role}")


@team.command(name="remove")
@click.argument("team_name")
@click.argument("user")
@pass_state
def team_remove(state: CliState, team_name: str, user: str) -> None:
    identity = state.container.identity
    team_obj = identity.get_team(team_name)
    removed = identity.remove_member(team_obj.id, _user_id(state, user))
    click.echo(f"Removed {user} from {team_obj.name}" if removed else warn(f"{user} was not a member"))


# --- database ------------------------------------------------------------
@click.group()
def db() -> None:
    """Database schema management."""


@db.command()
@pass_state
def upgrade(state: CliState) -> None:
    """Apply pending migrations (run as an explicit release step when MNEMOSYNE_AUTO_MIGRATE=false)."""
    from mnemosyne.db import migrate
    from mnemosyne.db.session import create_engine_from_settings

    state.settings.ensure_home()
    engine = create_engine_from_settings(state.settings)
    try:
        before = migrate.current_revision(engine)
        migrate.upgrade(engine)
        after = migrate.current_revision(engine)
    finally:
        engine.dispose()
    click.echo(f"{before or 'empty'} -> {after}" if before != after else f"Already at {after}")


@db.command()
@pass_state
def current(state: CliState) -> None:
    """Show the schema revision."""
    from mnemosyne.db import migrate
    from mnemosyne.db.session import create_engine_from_settings

    engine = create_engine_from_settings(state.settings)
    try:
        revision, head = migrate.current_revision(engine), migrate.HEAD_REVISION
    finally:
        engine.dispose()
    behind = "" if revision == head else warn(f"  (head is {head}; run: mnemosyne db upgrade)")
    click.echo(f"{revision or 'empty'}{behind}")
