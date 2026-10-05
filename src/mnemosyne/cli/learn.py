"""`mnemosyne learn ...` — teach Mnemosyne from code, sessions and text."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

import click

from mnemosyne.cli.common import CliState, bold, cwd_option, dim, echo_json, ok, pass_state, print_report, resolved_cwd

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"


@click.group()
def learn() -> None:
    """Learn from repositories, AI sessions, or text."""


@learn.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--author", help="Your git email (default: user.email from git config).")
@click.option("--dry-run", is_flag=True, help="Show what would be learned without storing anything.")
@click.option("--no-generalize", is_flag=True, help="Skip cross-repository generalization.")
@click.option("--json", "as_json", is_flag=True)
@pass_state
def repo(state: CliState, paths: tuple[Path, ...], author: Optional[str], dry_run: bool, no_generalize: bool,
         as_json: bool) -> None:
    """Learn conventions, tooling and patterns from repositories (default: the current one).

    Code is analysed on this machine; only the findings are stored (or sent,
    in team mode).
    """
    results = []
    targets = paths or (Path.cwd(),)
    for index, path in enumerate(targets):
        last = index == len(targets) - 1
        result = state.backend.learn_repository(str(path.resolve()), author_email=author, dry_run=dry_run,
                                                generalize=not no_generalize and last)
        results.append(result)
        if as_json:
            continue
        languages = ", ".join(f"{lang} ({n})" for lang, n in sorted(result.languages.items(), key=lambda kv: -kv[1]))
        timing = f"{result.duration_ms:.0f} ms · {languages or 'no code found'}"
        click.echo(f"{bold(result.repo_id)}  {dim(timing)}")
        if dry_run or state.verbose:
            for finding in result.findings:
                value = f" = {finding['value']}" if finding.get("value") else ""
                detail = f"{value}  ({finding['confidence']:.2f}, {finding['analyzer']})"
                click.echo(f"  {finding['title']}" + dim(detail))
        if result.report:
            print_report(result.report, verbose=state.verbose)
        if result.generalization and result.generalization.counts.get("created", 0) + \
                result.generalization.counts.get("superseded", 0):
            print_report(result.generalization, verbose=state.verbose, header="Across your repositories")
    if as_json:
        echo_json([{"repo_id": r.repo_id, "languages": r.languages, "findings": r.findings,
                    "duration_ms": r.duration_ms,
                    "report": r.report.__dict__ if r.report else None,
                    "generalization": r.generalization.__dict__ if r.generalization else None} for r in results])
    elif dry_run:
        click.echo(dim("(dry run: nothing stored)"))


@learn.command()
@click.argument("transcript", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--session-id")
@cwd_option
@click.option("--llm", "use_llm", is_flag=True, help="Also use the LLM extractor (needs MNEMOSYNE_LLM_EXTRACTION).")
@click.option("--quiet", is_flag=True)
@pass_state
def session(state: CliState, transcript: Path, session_id: Optional[str], cwd: Optional[Path], use_llm: bool,
            quiet: bool) -> None:
    """Learn from one session transcript (Claude Code JSONL, chat JSON, or plain text)."""
    from mnemosyne.learning.conversation.transcript import load_transcript

    parsed = load_transcript(transcript, session_id=session_id)
    if session_id:
        parsed.session_id = session_id
    report = state.backend.learn_conversation(parsed, cwd=str(cwd) if cwd else parsed.cwd, use_llm=use_llm)
    _SessionLedger(state).mark(transcript, save=True)
    if not quiet:
        print_report(report, verbose=state.verbose)


@learn.command()
@click.option("--projects-dir", type=click.Path(file_okay=False, path_type=Path), default=DEFAULT_PROJECTS_DIR,
              show_default=True, help="Where Claude Code keeps session transcripts.")
@click.option("--since", "since_days", type=int, default=90, show_default=True,
              help="Only sessions from the last N days.")
@click.option("--limit", type=int, default=500, show_default=True)
@click.option("--llm", "use_llm", is_flag=True, help="Also use the LLM extractor (costs tokens per session).")
@click.option("--force", is_flag=True, help="Re-read sessions already learned from.")
@pass_state
def sessions(state: CliState, projects_dir: Path, since_days: int, limit: int, use_llm: bool, force: bool) -> None:
    """Backfill: learn from your past Claude Code sessions."""
    from mnemosyne.learning.conversation.transcript import load_transcript

    if not projects_dir.exists():
        raise click.ClickException(f"no Claude Code sessions found at {projects_dir}")
    cutoff = time.time() - since_days * 86400
    files = sorted((p for p in projects_dir.glob("*/*.jsonl") if p.stat().st_mtime >= cutoff),
                   key=lambda p: p.stat().st_mtime)[-limit:]
    ledger = _SessionLedger(state)
    pending = [p for p in files if force or not ledger.seen(p)]
    click.echo(f"{len(files)} sessions in range, {len(pending)} new.")
    totals: dict[str, int] = {}
    learned: list[str] = []
    with click.progressbar(pending, label="Learning", file=sys.stderr, show_pos=True) as bar:
        for path in bar:
            try:
                parsed = load_transcript(path)
                report = state.backend.learn_conversation(parsed, cwd=parsed.cwd, use_llm=use_llm)
            except Exception as exc:  # one unreadable session must not stop the backfill
                click.echo(f"\n  skipped {path.name}: {exc}", err=True)
                continue
            for action, count in report.counts.items():
                totals[action] = totals.get(action, 0) + count
            learned += [o["title"] for o in report.learned]
            ledger.mark(path)
    ledger.save()
    summary = ", ".join(f"{n} {a}" for a, n in sorted(totals.items()) if n) or "nothing new"
    click.echo(f"{ok('Done')}: {summary}")
    for title in learned[:25]:
        click.echo(f"  + {title}")
    if len(learned) > 25:
        click.echo(dim(f"  … and {len(learned) - 25} more (mnemosyne list)"))


@learn.command()
@click.argument("text", nargs=-1)
@cwd_option
@pass_state
def text(state: CliState, text: tuple[str, ...], cwd: Optional[Path]) -> None:
    """Learn from free text (argument or stdin), e.g. notes or a pasted chat."""
    from mnemosyne.learning.conversation.transcript import parse_transcript_text

    raw = " ".join(text) if text and text != ("-",) else sys.stdin.read()
    if not raw.strip():
        raise click.ClickException("no text given")
    parsed = parse_transcript_text(raw, session_id=f"text-{int(time.time())}")
    if not any(t.role == "user" for t in parsed.turns):
        parsed = replace(parsed, turns=[replace(t, role="user") for t in parsed.turns])
    print_report(state.backend.learn_conversation(parsed, cwd=resolved_cwd(cwd)), verbose=state.verbose)


class _SessionLedger:
    """Which transcripts (at which size) have been learned from, so a
    backfill only reads new or grown sessions."""

    def __init__(self, state: CliState) -> None:
        self.path = state.settings.home / "state" / "sessions.json"
        try:
            self.entries: dict[str, str] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.entries = {}

    @staticmethod
    def _signature(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_size}:{int(stat.st_mtime)}"

    def seen(self, path: Path) -> bool:
        return self.entries.get(str(path.resolve())) == self._signature(path)

    def mark(self, path: Path, *, save: bool = False) -> None:
        self.entries[str(path.resolve())] = self._signature(path)
        if save:
            self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries, indent=0), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:  # the ledger is an optimisation; learning already happened
            pass
