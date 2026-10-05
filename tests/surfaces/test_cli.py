"""The `mnemosyne` command line, end to end against a real home directory."""
from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from mnemosyne.cli.main import cli


@pytest.fixture
def run(_isolated_home):
    runner = CliRunner()

    def invoke(*args: str, input: str | None = None, ok: bool = True):
        result = runner.invoke(cli, list(args), input=input, catch_exceptions=False)
        if ok:
            assert result.exit_code == 0, result.output
        return result

    return invoke


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "payments"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", "git@github.com:acme/payments.git"], check=True)
    (path / "pyproject.toml").write_text("[project]\nname='payments'\nrequires-python='>=3.12'\n"
                                         "dependencies=['fastapi']\n[tool.ruff]\nline-length = 100\n")
    return path


def test_init_status_and_doctor(run, _isolated_home):
    assert "Ready." in run("init").output
    assert (_isolated_home / "mnemosyne.db").exists()
    assert oct(_isolated_home.stat().st_mode & 0o777) == "0o700"
    status = json.loads(run("status", "--json").output)
    assert status["mode"] == "local" and status["stats"]["active"] == 0
    doctor = run("doctor")
    assert "database schema up to date" in doctor.output


def test_remember_recall_show_feedback_forget(run, repo):
    out = run("remember", "--cwd", str(repo), "Use", "pnpm,", "never", "npm").output
    assert "Remembered: Prefers pnpm over npm" in out and "javascript.package_manager=pnpm" in out
    out = run("remember", "--cwd", str(repo), "--here", "Money is stored as integer cents").output
    assert "repo:github.com/acme/payments" in out
    assert "Already remembered today" in run("remember", "--cwd", str(repo), "--here",
                                             "Money is stored as integer cents").output

    recalled = json.loads(run("recall", "--cwd", str(repo), "--json", "how", "is", "money", "stored").output)
    assert recalled[0]["memory"]["title"] == "Money is stored as integer cents"
    short = recalled[0]["memory"]["id"][:8]

    shown = run("show", short).output
    assert "Money is stored as integer cents" in shown and "Evidence (1)" in shown
    assert "confidence" in run("feedback", short, "helpful").output
    assert "Archived" in run("forget", short).output
    assert all(m["id"][:8] != short for m in json.loads(run("list", "--json").output))
    assert "Deleted" in run("forget", short, "--permanent", "--yes").output


def test_remember_validates_options(run):
    result = run("remember", "--here", "--lang", "python", "x", ok=False)
    assert result.exit_code == 1 and "only one of" in result.output
    result = run("remember", "--key", "python.formatter", "use black", ok=False)
    assert "--key and --value go together" in result.output


def test_unknown_memory_id_is_a_clean_error(run):
    result = run("show", "deadbeef", ok=False)
    assert result.exit_code == 1 and "no memory id starts with" in result.output and "Traceback" not in result.output


def test_learn_repo_context_profile_and_sync(run, repo):
    out = run("learn", "repo", str(repo)).output
    assert "github.com/acme/payments" in out and "learned" in out
    assert "learned" not in run("learn", "repo", str(repo)).output  # nothing new the second time

    context = run("context", "--cwd", str(repo), "add", "an", "endpoint").output
    assert "Developer memory" in context and "FastAPI" in context
    assert "<mnemosyne-context>" in run("context", "--cwd", str(repo), "--wrap").output
    assert json.loads(run("context", "--cwd", str(repo), "-f", "json").output)["sections"]
    assert "line length 100" in run("profile", "--cwd", str(repo)).output.lower()

    out = run("sync", str(repo), "--all").output
    assert "created" in out
    claude_md = (repo / "CLAUDE.md").read_text()
    assert "<!-- mnemosyne:begin" in claude_md and "FastAPI" in claude_md
    assert (repo / ".cursor" / "rules" / "mnemosyne.mdc").exists()
    assert "unchanged" in run("sync", str(repo)).output


def test_learn_session_text_and_backfill(run, tmp_path, _isolated_home):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "sessionId": "s-1", "cwd": str(tmp_path),
         "message": {"role": "user", "content": "From now on, never use print for logging."}},
        {"type": "assistant", "sessionId": "s-1", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Got it."}]}},
    ]) + "\n")
    assert "learned 1 new" in run("learn", "session", str(transcript)).output
    assert run("learn", "session", str(transcript), "--quiet").output == ""

    projects = tmp_path / "projects" / "-home-dev-payments"
    projects.mkdir(parents=True)
    (projects / "a.jsonl").write_text(transcript.read_text().replace("print", "global state"))
    out = run("learn", "sessions", "--projects-dir", str(tmp_path / "projects")).output
    assert "1 new" in out and "Never use global state for logging" in out
    assert "0 new" in run("learn", "sessions", "--projects-dir", str(tmp_path / "projects")).output

    out = run("learn", "text", input="I prefer small, focused pull requests.\n").output
    assert "learned 1 new" in out


def test_export_import_roundtrip(run, tmp_path):
    run("remember", "Keep answers terse")
    bundle = tmp_path / "bundle.json"
    run("export", "-o", str(bundle))
    assert json.loads(bundle.read_text())["format"] == "mnemosyne-export"
    out = run("import", str(bundle)).output
    assert "1 skipped" in out


def test_hook_command_outputs_json_and_never_fails(run):
    run("remember", "Never force-push to shared branches")
    out = run("hook", "session-start", input=json.dumps({"cwd": "/nonexistent"})).output
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert run("hook", "user-prompt", input="garbage").output == ""


def test_install_and_uninstall_claude_code(run, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    out = run("install", "claude-code", "--scope", "local", "--project-dir", str(project)).output
    assert "updated" in out
    settings = json.loads((project / ".claude" / "settings.local.json").read_text())
    assert "UserPromptSubmit" in settings["hooks"]
    assert "mnemosyne" in json.loads((project / ".mcp.json").read_text())["mcpServers"]
    assert "already up to date" in run("install", "claude-code", "--scope", "local", "--project-dir",
                                       str(project)).output
    run("uninstall", "claude-code", "--scope", "local", "--project-dir", str(project))
    assert "hooks" not in json.loads((project / ".claude" / "settings.local.json").read_text())


def test_admin_users_keys_teams(run):
    out = run("admin", "user", "create", "alice", "--admin").output
    assert "API key (shown once): mn_" in out
    run("admin", "user", "create", "bob", "--no-key")
    assert "alice" in run("admin", "user", "list").output
    issued = run("admin", "key", "issue", "bob", "--scope", "read").output
    key_id = issued.split()[0]
    assert "memories:read" in run("admin", "key", "list", "bob").output
    assert "Revoked" in run("admin", "key", "revoke", key_id).output
    run("admin", "team", "create", "payments")
    run("admin", "team", "add", "payments", "bob", "--role", "maintainer")
    assert "1 members" in run("admin", "team", "list").output
    run("admin", "user", "disable", "bob")
    result = run("admin", "key", "issue", "nobody", ok=False)
    assert "no user with id or handle" in result.output


def test_db_commands(run):
    assert "->" in run("db", "upgrade").output
    assert "Already at" in run("db", "upgrade").output
    assert run("db", "current").output.strip()


def test_demo_runs_without_touching_home(run, _isolated_home):
    out = run("demo", "--max-tokens", "400").output
    assert "payments-api" in out and "tokens" in out
    assert not (_isolated_home / "mnemosyne.db").exists()
