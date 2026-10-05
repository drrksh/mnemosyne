"""
Claude Code integration: hook handlers and a one-command installer.

Hooks make Mnemosyne automatic — the developer never has to ask for
their own context:

* **SessionStart** — inject the developer's profile for this repository
  (rules, preferences, conventions, how to run things).
* **UserPromptSubmit** — learn from the prompt in real time ("no, use
  pnpm"), and inject the memories relevant to what was just asked, but
  only when there are any.
* **PreCompact / SessionEnd** — learn from the whole transcript in a
  *detached* background process, so the hook returns immediately
  (Claude Code gives SessionEnd hooks about 1.5 seconds).

Hook handlers never fail the developer's session: any error is logged to
`<home>/logs/hooks.log` and the hook exits 0 with no output. Injected
context is capped below Claude Code's 10,000-character limit and wrapped
in `<mnemosyne-context>` so it is never re-learned from the transcript.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

if TYPE_CHECKING:  # pragma: no cover
    from mnemosyne.client.backend import Backend
    from mnemosyne.config import Settings

logger = logging.getLogger("mnemosyne.hooks")

HookName = Literal["session-start", "user-prompt", "pre-compact", "session-end"]

#: Claude Code event -> (our hook name, matcher, timeout seconds)
HOOK_EVENTS: dict[str, tuple[HookName, Optional[str], int]] = {
    "SessionStart": ("session-start", "startup|resume|clear|compact", 15),
    "UserPromptSubmit": ("user-prompt", None, 10),
    "PreCompact": ("pre-compact", None, 10),
    "SessionEnd": ("session-end", None, 5),
}
EVENT_FOR_HOOK = {hook: event for event, (hook, _, _) in HOOK_EVENTS.items()}
MARKER = " hook "  # identifies our entries in settings.json (with "mnemosyne")
PROMPT_CONTEXT_TOKENS = 700


@dataclass
class HookOutput:
    stdout: str = ""
    exit_code: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


def _hook_logger(settings: Settings) -> logging.Logger:
    hook_log = logging.getLogger("mnemosyne.hooks.file")
    target = str((settings.logs_dir / "hooks.log").absolute())
    if any(getattr(h, "baseFilename", None) == target for h in hook_log.handlers):
        return hook_log
    for old in list(hook_log.handlers):
        hook_log.removeHandler(old)
        old.close()
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(target, maxBytes=2_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    except OSError:
        handler = logging.NullHandler()
    hook_log.addHandler(handler)
    hook_log.setLevel(logging.INFO)
    hook_log.propagate = False
    return hook_log


def _additional_context(event: str, text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


# ----------------------------------------------------------------------
# Hook handlers
# ----------------------------------------------------------------------
def handle_session_start(payload: dict[str, Any], backend: Backend, settings: Settings) -> HookOutput:
    cwd = payload.get("cwd") or os.getcwd()
    result = backend.context(task=None, cwd=cwd, max_tokens=settings.hook_max_tokens,
                             max_chars=settings.hook_max_chars, wrap=True, include_profile=True)
    if not result.items:
        return HookOutput(detail={"items": 0})
    return HookOutput(stdout=_additional_context("SessionStart", result.text),
                      detail={"items": len(result.items), "tokens": result.estimated_tokens})


def handle_user_prompt(payload: dict[str, Any], backend: Backend, settings: Settings) -> HookOutput:
    from mnemosyne.learning.conversation.heuristics import ConversationExtractor
    from mnemosyne.learning.conversation.transcript import clean_user_text
    from mnemosyne.learning.workspace import detect_working_context

    prompt = clean_user_text(str(payload.get("prompt") or ""))
    if not prompt.strip():
        return HookOutput()
    cwd = payload.get("cwd") or os.getcwd()
    session = str(payload.get("session_id") or "unknown")
    parts: list[str] = []
    detail: dict[str, Any] = {}

    candidates = ConversationExtractor().extract_text(prompt, session_id=session,
                                                      context=detect_working_context(cwd))
    if candidates:
        report = backend.ingest(candidates, source="hook:user-prompt")
        learned = [o["title"] for o in report.learned]
        detail["learned"] = learned
        if learned:
            parts.append("<mnemosyne-context>\nMnemosyne remembered for future sessions: "
                         + "; ".join(learned) + ".\n</mnemosyne-context>")

    if len(prompt.split()) >= 3:
        budget = settings.hook_max_chars - sum(len(p) for p in parts) - 50
        result = backend.context(task=prompt, cwd=cwd, max_tokens=PROMPT_CONTEXT_TOKENS, max_chars=max(500, budget),
                                 wrap=True, include_profile=False)
        detail["relevant"] = result.relevant_count
        if result.relevant_count:
            parts.insert(0, result.text)
    if not parts:
        return HookOutput(detail=detail)
    return HookOutput(stdout=_additional_context("UserPromptSubmit", "\n".join(parts)), detail=detail)


def handle_transcript(payload: dict[str, Any], settings: Settings,
                      spawn: Optional[Callable[[list[str]], None]] = None) -> HookOutput:
    """PreCompact / SessionEnd: hand the transcript to a detached learner."""
    path = payload.get("transcript_path")
    if not path or not Path(path).expanduser().exists():
        return HookOutput(detail={"skipped": "no transcript"})
    argv = [*self_command(), "learn", "session", str(path), "--quiet"]
    if payload.get("session_id"):
        argv += ["--session-id", str(payload["session_id"])]
    if payload.get("cwd"):
        argv += ["--cwd", str(payload["cwd"])]
    if settings.llm_extraction:
        argv.append("--llm")
    (spawn or spawn_detached)(argv)
    return HookOutput(detail={"spawned": True})


def mnemosyne_home() -> Path:
    """The home directory, resolved without loading Settings (hooks must
    stay cheap); matches `config.default_home()`."""
    return Path(os.environ.get("MNEMOSYNE_HOME") or Path.home() / ".mnemosyne").expanduser()


def spawn_detached(argv: list[str], *, log_name: str = "learner.log") -> None:
    log_dir = mnemosyne_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / log_name, "ab") as log:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
            env={**os.environ, "MNEMOSYNE_LOG_FORMAT": "console"},
        )


def run_hook(
    hook: HookName,
    stdin_text: str,
    *,
    settings: Settings,
    backend_factory: Callable[[], Backend],
    spawn: Optional[Callable[[list[str]], None]] = None,
) -> HookOutput:
    """Entry point used by `mnemosyne hook <name>`. Never raises."""
    hook_log = _hook_logger(settings)
    started = time.perf_counter()
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        if hook in ("pre-compact", "session-end"):
            output = handle_transcript(payload, settings, spawn)
        else:
            backend = backend_factory()
            try:
                handler = handle_session_start if hook == "session-start" else handle_user_prompt
                output = handler(payload, backend, settings)
            finally:
                backend.close()
        hook_log.info("%s ok %.0fms %s", hook, (time.perf_counter() - started) * 1000, output.detail)
        return output
    except Exception:  # never break the developer's session
        hook_log.exception("%s failed", hook)
        return HookOutput()


# ----------------------------------------------------------------------
# Installer
# ----------------------------------------------------------------------
InstallScope = Literal["user", "project", "local"]


def self_command() -> list[str]:
    """How to invoke this installation of Mnemosyne, robust to PATH
    differences between the shell and Claude Code's hook environment."""
    found = shutil.which("mnemosyne")
    if found:
        return [str(Path(found).resolve())]
    return [sys.executable, "-m", "mnemosyne.cli"]


def command_string(*args: str) -> str:
    return " ".join(shlex.quote(part) for part in [*self_command(), *args])


def settings_path(scope: InstallScope, project_dir: Optional[Path] = None) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    root = (project_dir or Path.cwd()).resolve()
    return root / ".claude" / ("settings.json" if scope == "project" else "settings.local.json")


def _is_ours(hook: dict[str, Any]) -> bool:
    command = str(hook.get("command", ""))
    return "mnemosyne" in command and MARKER in command


def _strip_ours(hooks_config: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for event, groups in hooks_config.items():
        kept_groups = []
        for group in groups if isinstance(groups, list) else []:
            remaining = [h for h in group.get("hooks", []) if not _is_ours(h)]
            if remaining:
                kept_groups.append({**group, "hooks": remaining})
        if kept_groups:
            cleaned[event] = kept_groups
    return cleaned


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)  # refuse to touch a file we cannot parse
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _write_json(path: Path, data: dict[str, Any], *, backup: bool) -> Optional[Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if backup and path.exists():
        backup_path = path.with_name(f"{path.name}.mnemosyne-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup_path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return backup_path


@dataclass
class InstallResult:
    path: Path
    changed: bool
    backup: Optional[Path]
    events: list[str]


def install_hooks(path: Path, *, events: Optional[list[str]] = None, dry_run: bool = False) -> InstallResult:
    """Add (or refresh) Mnemosyne's hooks in a Claude Code settings file,
    leaving every other setting and hook untouched."""
    data = _load_json(path)
    hooks_config = _strip_ours(dict(data.get("hooks", {})))
    chosen = events or list(HOOK_EVENTS)
    for event in chosen:
        hook, matcher, timeout = HOOK_EVENTS[event]
        group: dict[str, Any] = {"hooks": [{"type": "command", "command": command_string("hook", hook),
                                            "timeout": timeout}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks_config.setdefault(event, []).append(group)
    new_data = {**data, "hooks": hooks_config}
    changed = new_data != data
    backup = _write_json(path, new_data, backup=True) if changed and not dry_run else None
    return InstallResult(path, changed, backup, chosen)


def uninstall_hooks(path: Path, *, dry_run: bool = False) -> InstallResult:
    data = _load_json(path)
    if "hooks" not in data:
        return InstallResult(path, False, None, [])
    cleaned = _strip_ours(data["hooks"])
    new_data = {k: v for k, v in data.items() if k != "hooks"}
    if cleaned:
        new_data["hooks"] = cleaned
    changed = new_data != data
    backup = _write_json(path, new_data, backup=True) if changed and not dry_run else None
    return InstallResult(path, changed, backup, [])


def install_project_mcp(project_dir: Path, *, dry_run: bool = False) -> InstallResult:
    """Register the MCP server in a project's `.mcp.json` (shared with the
    team through version control)."""
    path = project_dir.resolve() / ".mcp.json"
    data = _load_json(path)
    servers = dict(data.get("mcpServers", {}))
    command = self_command()
    servers["mnemosyne"] = {"command": command[0], "args": [*command[1:], "mcp"]}
    new_data = {**data, "mcpServers": servers}
    changed = new_data != data
    backup = _write_json(path, new_data, backup=True) if changed and not dry_run else None
    return InstallResult(path, changed, backup, [])


def user_mcp_command() -> list[str]:
    """`claude mcp add` invocation that registers Mnemosyne for every project."""
    return ["claude", "mcp", "add", "--scope", "user", "mnemosyne", "--", *self_command(), "mcp"]


def install_user_mcp(*, dry_run: bool = False) -> tuple[bool, str]:
    argv = user_mcp_command()
    printable = " ".join(shlex.quote(a) for a in argv)
    if dry_run or shutil.which("claude") is None:
        return False, printable
    result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    if result.returncode != 0 and "already exists" not in (result.stderr + result.stdout):
        return False, printable
    return True, printable


def install_cursor_mcp(scope: Literal["user", "project"], project_dir: Optional[Path] = None, *,
                       dry_run: bool = False) -> InstallResult:
    base = Path.home() if scope == "user" else (project_dir or Path.cwd()).resolve()
    path = base / ".cursor" / "mcp.json"
    data = _load_json(path)
    servers = dict(data.get("mcpServers", {}))
    command = self_command()
    servers["mnemosyne"] = {"command": command[0], "args": [*command[1:], "mcp"]}
    new_data = {**data, "mcpServers": servers}
    changed = new_data != data
    backup = _write_json(path, new_data, backup=True) if changed and not dry_run else None
    return InstallResult(path, changed, backup, [])
