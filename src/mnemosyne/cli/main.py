"""
`mnemosyne` — the command-line interface.

Everyday commands work against whichever backend is configured (the
local database by default, a team server when `MNEMOSYNE_SERVER_URL` is
set), so the same muscle memory works solo and in a company deployment.
Administration, migrations and evaluation act on the local database.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import click

from mnemosyne import __version__
from mnemosyne.cli.common import (
    CliState,
    MnemosyneGroup,
    bold,
    configure_cli_logging,
    cwd_option,
    dim,
    echo_json,
    memory_line,
    ok,
    pass_state,
    print_report,
    resolve_memory_id,
    resolved_cwd,
    warn,
)

MEMORY_TYPES = [
    "rule", "preference", "convention", "pattern", "anti_pattern", "tooling", "decision", "architecture",
    "procedure", "failure", "fact", "skill", "goal", "observation", "relationship",
]
FEEDBACK_SIGNALS = ["helpful", "not_helpful", "confirm", "wrong", "outdated"]


@click.group(cls=MnemosyneGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="mnemosyne")
@click.option("--home", type=click.Path(file_okay=False, path_type=Path), envvar="MNEMOSYNE_HOME",
              help="Mnemosyne home directory (default ~/.mnemosyne).")
@click.option("-v", "--verbose", is_flag=True, help="Show diagnostic logging on stderr.")
@click.pass_context
def cli(ctx: click.Context, home: Optional[Path], verbose: bool) -> None:
    """Mnemosyne: a memory of how you build software, for every AI assistant you use.

    \b
    Get started:
      mnemosyne init                    create your memory
      mnemosyne learn repo              learn this repository's conventions
      mnemosyne install claude-code     inject your context into every Claude Code session
      mnemosyne context "add an endpoint"   see what an assistant would be told
    """
    if home is not None:
        os.environ["MNEMOSYNE_HOME"] = str(home.expanduser())
    configure_cli_logging(verbose, quiet=ctx.invoked_subcommand in ("hook", "mcp"))
    state = ctx.ensure_object(CliState)
    state.verbose = verbose
    ctx.call_on_close(state.close)


# ----------------------------------------------------------------------
# Setup and status
# ----------------------------------------------------------------------
@cli.command()
@pass_state
def init(state: CliState) -> None:
    """Create the memory database (idempotent) and show next steps."""
    settings = state.settings
    if settings.server_url:
        info = state.backend.whoami()
        click.echo(f"{ok('Connected')} to {info['server']} as {info['user_id']}.")
        return
    container = state.container
    container.local_principal()
    from mnemosyne.db import migrate

    click.echo(f"{ok('Ready.')} Memory home: {settings.home}")
    revision = migrate.current_revision(container.db_engine)
    click.echo(f"Database: {settings.database_url}  " + dim(f"(schema {revision})"))
    click.echo("")
    click.echo(bold("Next steps"))
    click.echo("  mnemosyne learn repo [PATH]          learn conventions from a repository you work on")
    click.echo("  mnemosyne learn sessions             learn from your past Claude Code sessions")
    click.echo("  mnemosyne install claude-code        make it automatic in every Claude Code session")
    click.echo("  mnemosyne remember \"...\"             tell it something directly")


@cli.command()
@click.option("--json", "as_json", is_flag=True)
@pass_state
def status(state: CliState, as_json: bool) -> None:
    """Who you are, where your memory lives, and what it holds."""
    info = state.backend.whoami()
    stats = state.backend.stats()
    if as_json:
        echo_json({**info, "stats": stats})
        return
    where = info.get("server") or info.get("database")
    click.echo(f"{bold('Mnemosyne')} {__version__}  ·  {info['mode']} mode  ·  user {info['user_id']}")
    click.echo(f"{dim('store:')} {where}")
    if info.get("team_ids"):
        click.echo(f"{dim('teams:')} {', '.join(info['team_ids'])}")
    click.echo(f"\n{bold(str(stats['active']))} active memories")
    if stats["by_type"]:
        click.echo("  " + ", ".join(f"{n} {t}" for t, n in stats["by_type"].items()))
        click.echo(dim("  by origin: " + ", ".join(f"{n} {o}" for o, n in stats["by_origin"].items())))
        repos = {s: n for s, n in stats["by_scope"].items() if s.startswith("repo:")}
        if repos:
            click.echo(dim(f"  across {len(repos)} repositories"))


@cli.command()
@pass_state
def doctor(state: CliState) -> None:
    """Check the installation and every integration."""
    from mnemosyne.integrations import claude_code

    problems = 0

    def line(good: bool, label: str, detail: str = "", *, optional: bool = False) -> None:
        nonlocal problems
        mark = ok("✓") if good else (warn("•") if optional else click.style("✗", fg="red"))
        problems += 0 if good or optional else 1
        click.echo(f" {mark} {label}" + (dim(f"  {detail}") if detail else ""))

    settings = state.settings
    click.echo(bold("Mnemosyne doctor"))
    line(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}")
    try:
        info = state.backend.whoami()
        line(True, f"{info['mode']} store reachable", str(info.get("server") or info.get("database")))
    except Exception as exc:  # report, don't crash: this command exists to diagnose
        line(False, "store reachable", str(exc))
        info = {}
    if not settings.server_url and info:
        from mnemosyne.db import migrate

        current, head = migrate.current_revision(state.container.db_engine), migrate.HEAD_REVISION
        line(current == head, "database schema up to date", f"{current} (expected {head})")
        mode = settings.home.stat().st_mode & 0o777 if settings.home.exists() else 0
        line(mode & 0o077 == 0, "home directory private", f"{settings.home} ({oct(mode)})", optional=True)

    command = claude_code.self_command()
    line(True, "hook command", " ".join(command))
    installed = []
    for scope in ("user", "project", "local"):
        path = claude_code.settings_path(scope)
        try:
            data = claude_code._load_json(path)
        except Exception:
            line(False, f"Claude Code {scope} settings readable", str(path))
            continue
        events = [e for e, groups in data.get("hooks", {}).items()
                  if any(claude_code._is_ours(h) for g in groups for h in g.get("hooks", []))]
        if events:
            installed.append(f"{scope}: {', '.join(events)}")
    line(bool(installed), "Claude Code hooks installed", "; ".join(installed) or "run: mnemosyne install claude-code",
         optional=True)
    import shutil

    line(shutil.which("claude") is not None, "claude CLI on PATH", "needed for user-scope MCP registration",
         optional=True)
    if settings.llm_extraction:
        try:
            import anthropic  # noqa: F401

            line(True, "LLM extraction enabled", settings.llm_model)
        except ImportError:
            line(False, "LLM extraction enabled but the 'anthropic' package is missing",
                 "pip install 'mnemosyne[llm]'")
    else:
        line(True, "LLM extraction", "off (heuristics only; set MNEMOSYNE_LLM_EXTRACTION=true)", optional=True)
    hooks_log = settings.logs_dir / "hooks.log"
    if hooks_log.exists():
        tail = hooks_log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        failures = [entry for entry in tail if " failed" in entry]
        line(not failures, "recent hook runs", f"{len(failures)} failures in the last {len(tail)} log lines "
             f"(see {hooks_log})" if failures else f"{len(tail)} log lines, no failures")
    click.echo("")
    click.echo(ok("All good.") if problems == 0 else click.style(f"{problems} problem(s) found.", fg="red"))
    if problems:
        sys.exit(1)


# ----------------------------------------------------------------------
# Everyday memory
# ----------------------------------------------------------------------
@cli.command()
@click.argument("text", nargs=-1, required=True)
@click.option("-t", "--type", "memory_type", type=click.Choice(MEMORY_TYPES), help="Memory type (default: inferred).")
@click.option("--here", is_flag=True, help="Scope to the current repository.")
@click.option("--lang", help="Scope to a language, e.g. python.")
@click.option("--scope", help="Explicit scope: global, lang:<language> or repo:<id>.")
@click.option("--key", help="Slot key, e.g. python.formatter (a new value replaces the old one).")
@click.option("--value", help="Slot value, e.g. ruff-format.")
@click.option("--importance", type=click.FloatRange(0, 1))
@click.option("--pin", is_flag=True, help="Always include in context.")
@cwd_option
@pass_state
def remember(state: CliState, text: tuple[str, ...], memory_type: Optional[str], here: bool, lang: Optional[str],
             scope: Optional[str], key: Optional[str], value: Optional[str], importance: Optional[float],
             pin: bool, cwd: Optional[Path]) -> None:
    """Tell Mnemosyne something directly.

    \b
      mnemosyne remember "Use pnpm, never npm"
      mnemosyne remember --here "Money is stored as integer cents"
      mnemosyne remember -t decision "Chose Postgres over Mongo for transactions"
    """
    from mnemosyne.client.explicit import ALREADY_RECORDED, explicit_candidate
    from mnemosyne.domain.enums import MemoryType
    from mnemosyne.domain.exceptions import ValidationError
    from mnemosyne.learning.workspace import detect_working_context

    if sum(bool(x) for x in (here, lang, scope)) > 1:
        raise ValidationError("use only one of --here, --lang and --scope")
    if bool(key) != bool(value):
        raise ValidationError("--key and --value go together")
    context = detect_working_context(resolved_cwd(cwd))
    if here:
        if not context.repo_id:
            raise ValidationError("--here needs a git repository (none found from this directory)")
        scope = f"repo:{context.repo_id}"
    elif lang:
        scope = f"lang:{lang}"
    candidate = explicit_candidate(
        " ".join(text), context=context, memory_type=MemoryType(memory_type) if memory_type else None, scope=scope,
        key=key, value=value, importance=importance, pinned=pin, source="cli",
    )
    report = state.backend.ingest([candidate], source="cli:remember")
    outcome = report.outcomes[0] if report.outcomes else None
    if outcome is not None and outcome["action"] == "skipped" and outcome["reason"] == ALREADY_RECORDED:
        click.echo(f"{ok('Already remembered')} today: {outcome['title']}")
        return
    if outcome is None or outcome["action"] == "skipped":
        raise click.ClickException(f"not stored: {outcome['reason'] if outcome else 'no outcome'}")
    verb = {"created": "Remembered", "superseded": "Updated", "reinforced": "Already knew that; reinforced",
            "refreshed": "Already knew that; refreshed", "kept_existing": "Kept the existing memory"}.get(
        outcome["action"], outcome["action"])
    detail = [f"[{candidate.memory_type.value}]", candidate.scope]
    if candidate.key:
        detail.append(f"{candidate.key}={candidate.value}")
    click.echo(f"{ok(verb)}: {outcome['title']}  {dim(' · '.join(detail))}")
    if outcome.get("previous_value"):
        click.echo(dim(f"  replaces {outcome['key']}={outcome['previous_value']}"))
    if outcome.get("memory_id"):
        click.echo(dim(f"  id {outcome['memory_id']}"))


@cli.command()
@click.argument("query", nargs=-1, required=True)
@click.option("-n", "--limit", default=8, show_default=True)
@click.option("--explain", is_flag=True, help="Show how each score was computed.")
@click.option("--json", "as_json", is_flag=True)
@cwd_option
@pass_state
def recall(state: CliState, query: tuple[str, ...], limit: int, explain: bool, as_json: bool,
           cwd: Optional[Path]) -> None:
    """Search memory, ranked for the current repository."""
    items = state.backend.recall(" ".join(query), cwd=resolved_cwd(cwd), limit=limit, explain=explain)
    if as_json:
        echo_json(items)
        return
    if not items:
        click.echo(dim("Nothing relevant."))
        return
    for item in items:
        click.echo(memory_line(item["memory"], score=item["score"]))
        if explain and item.get("breakdown"):
            parts = ", ".join(f"{k} {v:.2f}" for k, v in item["breakdown"].items() if k != "total")
            click.echo(dim(f"    {parts}"))


@cli.command()
@click.argument("task", nargs=-1)
@click.option("--max-tokens", default=1500, show_default=True)
@click.option("-f", "--format", "fmt", type=click.Choice(["markdown", "xml", "json"]), default="markdown",
              show_default=True)
@click.option("--file", "file_path", help="The file being worked on (sharpens language detection).")
@click.option("--no-profile", is_flag=True, help="Only memories relevant to the task.")
@click.option("--wrap", is_flag=True, help="Wrap in <mnemosyne-context> tags.")
@cwd_option
@pass_state
def context(state: CliState, task: tuple[str, ...], max_tokens: int, fmt: str, file_path: Optional[str],
            no_profile: bool, wrap: bool, cwd: Optional[Path]) -> None:
    """Print the briefing an assistant gets for a task (or the profile, with no task)."""
    result = state.backend.context(
        task=" ".join(task) or None, cwd=resolved_cwd(cwd), file_path=file_path, max_tokens=max_tokens, fmt=fmt,
        wrap=wrap, include_profile=not no_profile, record_access=False,
    )
    if not result.text.strip() or not result.items:
        click.echo(dim("Nothing to say yet — teach me with `mnemosyne learn repo` or `mnemosyne remember`."), err=True)
        return
    click.echo(result.text)
    if sys.stdout.isatty():
        click.echo(dim(f"\n~{result.estimated_tokens} tokens · {len(result.items)} memories · "
                       f"{result.relevant_count} task-relevant"), err=True)


@cli.command()
@click.option("-f", "--format", "fmt", type=click.Choice(["markdown", "xml", "json"]), default="markdown")
@click.option("--max-tokens", default=4000, show_default=True)
@cwd_option
@pass_state
def profile(state: CliState, fmt: str, max_tokens: int, cwd: Optional[Path]) -> None:
    """Your standing profile for the current repository."""
    result = state.backend.context(task=None, cwd=resolved_cwd(cwd), max_tokens=max_tokens, fmt=fmt,
                                   include_profile=True, record_access=False)
    click.echo(result.text if result.items else dim("No profile yet."))


@cli.command(name="list")
@click.option("-t", "--type", "types", multiple=True, type=click.Choice(MEMORY_TYPES))
@click.option("--scope", "scopes", multiple=True, help="global, lang:<language> or repo:<id> (repeatable).")
@click.option("-n", "--limit", default=50, show_default=True)
@click.option("--archived", is_flag=True, help="Include archived memories.")
@click.option("--json", "as_json", is_flag=True)
@pass_state
def list_memories(state: CliState, types: tuple[str, ...], scopes: tuple[str, ...], limit: int, archived: bool,
                  as_json: bool) -> None:
    """List memories, most important first."""
    items = state.backend.list(memory_types=list(types) or None, scopes=list(scopes) or None, limit=limit,
                               include_archived=archived)
    if as_json:
        echo_json(items)
        return
    for memory in items:
        click.echo(memory_line(memory))
    click.echo(dim(f"{len(items)} shown"), err=True)


@cli.command()
@click.argument("memory_id")
@click.option("--json", "as_json", is_flag=True)
@pass_state
def show(state: CliState, memory_id: str, as_json: bool) -> None:
    """A memory with its evidence and history (accepts an 8-character id prefix)."""
    backend = state.backend
    full = resolve_memory_id(backend, memory_id)
    memory, evidence, history = backend.get(full), backend.evidence(full), backend.history(full)
    if as_json:
        echo_json({"memory": memory, "evidence": evidence, "history": history})
        return
    click.echo(bold(memory["title"]))
    click.echo(dim(f"{memory['id']} · {memory['memory_type']} · {memory['status']} · {memory['scope']} · "
                   f"origin {memory['origin']} · confidence {memory['confidence_score']:.2f} · "
                   f"importance {memory['importance_score']:.2f} · seen {memory['reinforcement_count']}x"))
    if memory.get("key"):
        click.echo(f"{memory['key']} = {memory.get('value')}")
    if memory.get("content") and memory["content"] != memory["title"]:
        click.echo(f"\n{memory['content']}")
    if evidence:
        click.echo(bold(f"\nEvidence ({len(evidence)})"))
        for e in evidence[:15]:
            excerpt = (e.get("excerpt") or "").replace("\n", " ")[:120]
            click.echo(f"  {dim(e['kind'])} {e['source_ref']}" + (dim(f"  “{excerpt}”") if excerpt else ""))
    if len(history) > 1:
        click.echo(bold(f"\nHistory ({len(history)} versions)"))
        for v in history[-10:]:
            reason = f" — {v['change_reason']}" if v.get("change_reason") else ""
            click.echo(f"  v{v['version']} {dim(v['recorded_at'][:19])} {', '.join(v['changed_fields'])}{reason}")


@cli.command()
@click.argument("memory_id")
@click.option("--permanent", is_flag=True, help="Delete permanently instead of archiving.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_state
def forget(state: CliState, memory_id: str, permanent: bool, yes: bool) -> None:
    """Archive a memory (or delete it permanently)."""
    backend = state.backend
    full = resolve_memory_id(backend, memory_id)
    memory = backend.get(full)
    if permanent and not yes:
        click.confirm(f"Permanently delete “{memory['title']}”?", abort=True)
    backend.forget(full, permanent=permanent)
    click.echo(f"{'Deleted' if permanent else 'Archived'}: {memory['title']}")


@cli.command()
@click.argument("memory_id")
@click.argument("signal", type=click.Choice(FEEDBACK_SIGNALS))
@click.option("--note")
@pass_state
def feedback(state: CliState, memory_id: str, signal: str, note: Optional[str]) -> None:
    """Confirm, correct or rate a memory: helpful, not_helpful, confirm, wrong, outdated."""
    backend = state.backend
    memory = backend.feedback(resolve_memory_id(backend, memory_id), signal, note)
    summary = f"confidence {memory['confidence_score']:.2f} · {memory['status']}"
    click.echo(f"{memory['title']}  {dim(summary)}")


# ----------------------------------------------------------------------
# Rule files, portability
# ----------------------------------------------------------------------
@cli.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=".")
@click.option("--target", "targets", multiple=True,
              type=click.Choice(["claude", "agents", "copilot", "gemini", "cursor"]),
              help="Files to write (default: claude and agents).")
@click.option("--all", "all_targets", is_flag=True, help="Write every supported rule file.")
@click.option("--audience", type=click.Choice(["repo", "personal"]), default="repo", show_default=True,
              help="repo: only repository and team knowledge (safe to commit). personal: everything.")
@click.option("--dry-run", is_flag=True)
@pass_state
def sync(state: CliState, path: Path, targets: tuple[str, ...], all_targets: bool, audience: str,
         dry_run: bool) -> None:
    """Write what Mnemosyne knows into CLAUDE.md, AGENTS.md, Cursor/Copilot/Gemini rule files.

    Mnemosyne owns a marked block in each file and never touches the rest.
    """
    from mnemosyne.integrations.exporters import TARGETS, repository_root, write_rule_files

    chosen = list(TARGETS) if all_targets else (list(targets) or ["claude", "agents"])
    root = repository_root(path)
    body, count = state.backend.rules(str(root), audience=audience)
    if count == 0:
        click.echo(warn("Nothing to write yet for this repository.") + " Try: mnemosyne learn repo " + str(root))
        return
    for result in write_rule_files(root, body, count, targets=chosen, dry_run=dry_run):
        click.echo(f"  {result.action:<9} {result.path.relative_to(root)}  {dim(f'{result.items} memories')}")
    if dry_run:
        click.echo(dim("(dry run: nothing written)"))


@cli.command(name="export")
@click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path), help="File (default: stdout).")
@pass_state
def export_cmd(state: CliState, output: Optional[Path]) -> None:
    """Export your memories as a portable JSON bundle."""
    bundle = state.backend.export()
    text = json.dumps(bundle, indent=2, ensure_ascii=False)
    if output:
        output.write_text(text + "\n", encoding="utf-8")
        click.echo(f"Exported {len(bundle['memories'])} memories to {output}", err=True)
    else:
        click.echo(text)


@cli.command(name="import")
@click.argument("source", type=click.File("r", encoding="utf-8"))
@pass_state
def import_cmd(state: CliState, source) -> None:
    """Import a bundle produced by `mnemosyne export` (idempotent)."""
    print_report(state.backend.import_bundle(json.load(source)), verbose=state.verbose)


# ----------------------------------------------------------------------
# Servers and hooks
# ----------------------------------------------------------------------
@cli.command()
@click.option("--host", help="Bind address (default from MNEMOSYNE_API_HOST, 127.0.0.1).")
@click.option("--port", type=int, help="Port (default from MNEMOSYNE_API_PORT, 8420).")
@click.option("--workers", type=int, default=1, show_default=True)
@pass_state
def serve(state: CliState, host: Optional[str], port: Optional[int], workers: int) -> None:
    """Run the REST API server."""
    import uvicorn

    from mnemosyne.api.security import check_bind_safety

    settings = state.settings
    bind = host or settings.api_host
    check_bind_safety(settings, bind)
    settings.ensure_home()
    uvicorn.run("mnemosyne.api.app:create_app", factory=True, host=bind, port=port or settings.api_port,
                workers=workers, log_config=None)


@cli.command()
@pass_state
def mcp(state: CliState) -> None:
    """Run the MCP server on stdio (for Claude Code, Cursor, and other MCP clients)."""
    from mnemosyne.mcp_server import run_stdio

    run_stdio(state.settings)


@cli.command(hidden=True)
@click.argument("name", type=click.Choice(["session-start", "user-prompt", "pre-compact", "session-end"]))
@pass_state
def hook(state: CliState, name: str) -> None:
    """Claude Code hook entry point (reads the event JSON on stdin)."""
    from mnemosyne.integrations.claude_code import run_hook

    try:
        stdin_text = sys.stdin.read()
    except Exception:
        stdin_text = ""

    def factory():
        return state.backend

    output = run_hook(name, stdin_text, settings=state.settings, backend_factory=factory)  # type: ignore[arg-type]
    if output.stdout:
        click.echo(output.stdout)
    sys.exit(output.exit_code)


# ----------------------------------------------------------------------
# Sub-groups live in their own modules.
# ----------------------------------------------------------------------
def _register() -> None:
    from mnemosyne.cli import admin, install, lab, learn

    cli.add_command(learn.learn)
    cli.add_command(install.install)
    cli.add_command(install.uninstall)
    cli.add_command(admin.admin)
    cli.add_command(admin.db)
    cli.add_command(lab.demo)
    cli.add_command(lab.evaluate)


_register()


def main() -> None:  # pragma: no cover - console entry point
    cli(prog_name="mnemosyne")


if __name__ == "__main__":  # pragma: no cover
    main()
