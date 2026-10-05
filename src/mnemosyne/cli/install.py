"""`mnemosyne install ...` — wire Mnemosyne into AI coding tools."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from mnemosyne.cli.common import bold, dim, ok, warn

SCOPE_HELP = (
    "user: every project on this machine (~/.claude/settings.json). "
    "project: this repository, shared with the team (.claude/settings.json). "
    "local: this repository, just you (.claude/settings.local.json)."
)


def _describe(result, dry_run: bool) -> str:
    if not result.changed:
        return dim("already up to date")
    if dry_run:
        return warn("would update")
    return ok("updated") + (dim(f" (backup: {result.backup.name})") if result.backup else "")


@click.group()
def install() -> None:
    """Connect Mnemosyne to an AI coding tool."""


@install.command(name="claude-code")
@click.option("--scope", type=click.Choice(["user", "project", "local"]), default="user", show_default=True,
              help=SCOPE_HELP)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Repository root for project/local scope (default: current directory).")
@click.option("--no-hooks", is_flag=True, help="Only register the MCP server.")
@click.option("--no-mcp", is_flag=True, help="Only install hooks.")
@click.option("--event", "events", multiple=True,
              type=click.Choice(["SessionStart", "UserPromptSubmit", "PreCompact", "SessionEnd"]),
              help="Install only these hook events (default: all).")
@click.option("--dry-run", is_flag=True)
def claude_code_cmd(scope: str, project_dir: Optional[Path], no_hooks: bool, no_mcp: bool, events: tuple[str, ...],
                    dry_run: bool) -> None:
    """Make Claude Code use your memory automatically.

    \b
    Hooks: your profile at session start, relevant memories with each
    prompt, and learning from every session when it ends or compacts.
    MCP: tools Claude can call to recall, remember, and give feedback.
    Existing settings and other hooks are preserved; a backup is kept.
    """
    from mnemosyne.integrations import claude_code

    click.echo(bold("Claude Code"))
    if not no_hooks:
        path = claude_code.settings_path(scope, project_dir)  # type: ignore[arg-type]
        try:
            result = claude_code.install_hooks(path, events=list(events) or None, dry_run=dry_run)
        except ValueError as exc:
            raise click.ClickException(f"{path} is not valid JSON; fix or move it, then retry ({exc})") from exc
        click.echo(f"  hooks  {path}  {_describe(result, dry_run)}")
        click.echo(dim(f"         {', '.join(result.events)}"))
    if not no_mcp:
        if scope == "user":
            registered, command = claude_code.install_user_mcp(dry_run=dry_run)
            if registered:
                click.echo(f"  mcp    user scope  {ok('registered')}")
            else:
                click.echo(f"  mcp    run this to register the MCP server for every project:\n         {command}")
        else:
            result = claude_code.install_project_mcp(project_dir or Path.cwd(), dry_run=dry_run)
            click.echo(f"  mcp    {result.path}  {_describe(result, dry_run)}")
            if scope == "project":
                click.echo(dim("         teammates need Mnemosyne installed; Claude Code asks each of them to "
                               "approve it"))
    if not dry_run:
        click.echo("\nStart a new Claude Code session; Mnemosyne briefs it automatically. "
                   + dim("Check with: mnemosyne doctor"))


@install.command(name="cursor")
@click.option("--scope", type=click.Choice(["user", "project"]), default="user", show_default=True)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--rules/--no-rules", default=True, help="Also write .cursor/rules/mnemosyne.mdc (project scope).")
@click.option("--dry-run", is_flag=True)
@click.pass_context
def cursor_cmd(ctx: click.Context, scope: str, project_dir: Optional[Path], rules: bool, dry_run: bool) -> None:
    """Register the MCP server with Cursor (and, per project, a rules file)."""
    from mnemosyne.integrations import claude_code

    result = claude_code.install_cursor_mcp(scope, project_dir, dry_run=dry_run)  # type: ignore[arg-type]
    click.echo(f"{bold('Cursor')}\n  mcp    {result.path}  {_describe(result, dry_run)}")
    if scope == "project" and rules:
        from mnemosyne.cli.main import sync

        ctx.invoke(sync, path=(project_dir or Path.cwd()), targets=("cursor",), all_targets=False, audience="repo",
                   dry_run=dry_run)


@click.group()
def uninstall() -> None:
    """Disconnect Mnemosyne from an AI coding tool (your memory is kept)."""


@uninstall.command(name="claude-code")
@click.option("--scope", type=click.Choice(["user", "project", "local"]), default="user", show_default=True)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--dry-run", is_flag=True)
def uninstall_claude_code(scope: str, project_dir: Optional[Path], dry_run: bool) -> None:
    """Remove Mnemosyne's hooks (other hooks and settings are untouched)."""
    from mnemosyne.integrations import claude_code

    path = claude_code.settings_path(scope, project_dir)  # type: ignore[arg-type]
    result = claude_code.uninstall_hooks(path, dry_run=dry_run)
    click.echo(f"hooks  {path}  {_describe(result, dry_run) if result.changed else dim('none installed')}")
    if scope == "user":
        click.echo(dim("To remove the MCP server too: claude mcp remove --scope user mnemosyne"))
