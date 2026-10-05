"""
Shared CLI plumbing: lazily-built settings and backend, output helpers,
and error handling that turns domain errors into one readable line.

Logging never goes to stdout from the CLI: `mnemosyne hook` answers
Claude Code on stdout and `mnemosyne mcp` speaks JSON-RPC on it, so a
stray log line there would corrupt the protocol.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

import click

if TYPE_CHECKING:  # pragma: no cover
    from mnemosyne.client.backend import Backend, ReportResult
    from mnemosyne.config import Settings
    from mnemosyne.container import Mnemosyne

F = TypeVar("F", bound=Callable[..., Any])


def configure_cli_logging(verbose: bool = False, *, quiet: bool = False) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    if quiet:
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.WARNING)
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)


@dataclass
class CliState:
    verbose: bool = False
    _settings: Optional[Settings] = None
    _backend: Optional[Backend] = None
    _container: Optional[Mnemosyne] = None
    _closers: list[Callable[[], None]] = field(default_factory=list)

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            from mnemosyne.config import load_settings

            self._settings = load_settings()
        return self._settings

    @property
    def backend(self) -> Backend:
        if self._backend is None:
            from mnemosyne.client.backend import open_backend

            if not self.settings.server_url:
                self.settings.ensure_home()
            self._backend = open_backend(self.settings)
            self._closers.append(self._backend.close)
        return self._backend

    @property
    def container(self) -> Mnemosyne:
        """The local database, for commands that only make sense on the
        machine that owns it (administration, migrations, evaluation)."""
        if self._container is None:
            from mnemosyne.container import Mnemosyne

            self.settings.ensure_home()
            self._container = Mnemosyne(self.settings)
            self._closers.append(self._container.close)
        return self._container

    def close(self) -> None:
        while self._closers:
            try:
                self._closers.pop()()
            except Exception:  # pragma: no cover - best effort on exit
                logging.getLogger("mnemosyne.cli").debug("close_failed", exc_info=True)


pass_state = click.make_pass_decorator(CliState, ensure=True)


class MnemosyneGroup(click.Group):
    """A click group that reports domain errors as `error: ...` with exit
    code 1 instead of a traceback."""

    def invoke(self, ctx: click.Context) -> Any:
        from mnemosyne.domain.exceptions import MnemosyneError

        try:
            return super().invoke(ctx)
        except MnemosyneError as exc:
            raise click.ClickException(exc.message) from exc
        except (ValueError, FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            if os.environ.get("MNEMOSYNE_DEBUG"):
                raise
            raise click.ClickException(str(exc)) from exc


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------
def echo_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def dim(text: str) -> str:
    return click.style(text, dim=True)


def bold(text: str) -> str:
    return click.style(text, bold=True)


def ok(text: str) -> str:
    return click.style(text, fg="green")


def warn(text: str) -> str:
    return click.style(text, fg="yellow")


def bad(text: str) -> str:
    return click.style(text, fg="red")


ACTION_STYLE = {
    "created": ("+", "green"),
    "superseded": ("~", "cyan"),
    "reinforced": ("^", "blue"),
    "refreshed": ("^", "blue"),
    "kept_existing": ("=", "yellow"),
    "skipped": ("-", None),
}


def print_report(report: ReportResult, *, verbose: bool = False, header: Optional[str] = None) -> None:
    if header:
        click.echo(bold(header))
    click.echo(report.summary)
    for outcome in report.outcomes:
        action = outcome["action"]
        if action == "skipped" and not verbose:
            continue
        symbol, color = ACTION_STYLE.get(action, ("?", None))
        line = f"  {click.style(symbol, fg=color)} {outcome['title']}"
        if outcome.get("previous_value") and outcome.get("new_value"):
            line += dim(f"  ({outcome['key']}: {outcome['previous_value']} -> {outcome['new_value']})")
        elif verbose and outcome.get("reason"):
            line += dim(f"  ({outcome['reason']})")
        click.echo(line)
    skipped = report.counts.get("skipped", 0)
    if skipped and not verbose:
        click.echo(dim(f"  {skipped} skipped (-v shows why)"))


def short_id(memory_id: str) -> str:
    return str(memory_id)[:8]


def memory_line(memory: dict[str, Any], *, score: Optional[float] = None) -> str:
    parts = [dim(short_id(memory["id"])), f"[{memory['memory_type']}]", memory["title"]]
    tail = []
    if memory.get("scope") and memory["scope"] != "global":
        tail.append(memory["scope"])
    if memory.get("key"):
        tail.append(f"{memory['key']}={memory.get('value')}")
    tail.append(f"conf {memory.get('confidence_score', 0):.2f}")
    if score is not None:
        tail.append(f"score {score:.3f}")
    return " ".join(parts) + "  " + dim(" · ".join(tail))


def resolve_memory_id(backend: Backend, prefix: str) -> str:
    """Accept a full id or the 8-character prefix the CLI prints."""
    from mnemosyne.domain.exceptions import NotFoundError, ValidationError

    prefix = prefix.strip().lower()
    if len(prefix) >= 32:
        return prefix
    if len(prefix) < 4:
        raise ValidationError("memory id prefix must be at least 4 characters")
    matches = [m["id"] for m in backend.list(limit=200, include_archived=True) if str(m["id"]).startswith(prefix)]
    if not matches:
        raise NotFoundError(f"no memory id starts with {prefix!r} (prefixes search the 200 most important "
                            "memories; pass the full id otherwise)")
    if len(matches) > 1:
        raise ValidationError(f"{prefix!r} is ambiguous ({len(matches)} memories); use more characters")
    return str(matches[0])


def cwd_option(func: F) -> F:
    return click.option(
        "--cwd",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Directory to detect the repository and languages from (default: current directory).",
    )(func)


def resolved_cwd(cwd: Optional[Path]) -> str:
    return str((cwd or Path.cwd()).resolve())
