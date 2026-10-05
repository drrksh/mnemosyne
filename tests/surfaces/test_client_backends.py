"""LocalBackend and RemoteBackend must behave identically: every test runs
against both (the remote one through the real HTTP API)."""
from __future__ import annotations

import subprocess

import pytest

from mnemosyne.client.explicit import explicit_candidate
from mnemosyne.domain.enums import MemoryType
from mnemosyne.domain.exceptions import NotFoundError
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.learning.conversation.transcript import parse_messages


def _git_repo(path, remote="https://github.com/acme/payments.git"):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    (path / "pyproject.toml").write_text("[project]\nname='payments'\n[tool.ruff]\nline-length = 100\n"
                                         "[tool.pytest.ini_options]\n")
    return path


def test_remember_then_recall_and_context(backend, tmp_path):
    repo = _git_repo(tmp_path / "payments")
    context = WorkingContext.create(repo_id="github.com/acme/payments", languages=["python"])
    report = backend.ingest([
        explicit_candidate("Use pnpm, never npm", context=context),
        explicit_candidate("Money is stored as integer cents", context=context,
                           scope="repo:github.com/acme/payments", memory_type=MemoryType.FACT),
    ], source="test")
    assert report.counts["created"] == 2

    items = backend.recall("how is money represented", cwd=str(repo))
    assert items and items[0]["memory"]["title"] == "Money is stored as integer cents"

    result = backend.context(task="add a refund endpoint", cwd=str(repo), wrap=True)
    assert result.text.startswith("<mnemosyne-context>")
    assert result.repo_id == "github.com/acme/payments"
    assert any(i["title"] == "Money is stored as integer cents" for i in result.items)


def test_slot_supersession_is_reported(backend):
    backend.ingest([explicit_candidate("use yarn instead of npm")], source="test")
    report = backend.ingest([explicit_candidate("Use pnpm, never npm")], source="test")
    outcome = report.outcomes[0]
    assert outcome["action"] == "superseded"
    assert (outcome["previous_value"], outcome["new_value"]) == ("yarn", "pnpm")
    assert [o["title"] for o in report.learned] == ["Prefers pnpm over npm"]


def test_learn_conversation_redacts_and_learns(backend):
    transcript = parse_messages([
        {"role": "user", "content": "From now on, always run the linter before committing. "
                                    "My token is ghp_abcdefghijklmnopqrstuvwxyz0123456789."},
        {"role": "assistant", "content": "Understood."},
    ], session_id="sess-1")
    report = backend.learn_conversation(transcript, cwd=None)
    assert report.counts.get("created", 0) >= 1
    stored = backend.list(limit=50)
    assert all("ghp_" not in (m["content"] or "") + m["title"] for m in stored)


def test_feedback_forget_get_evidence_history(backend):
    report = backend.ingest([explicit_candidate("Prefer early returns over nested ifs")], source="test")
    memory_id = report.outcomes[0]["memory_id"]

    assert backend.feedback(memory_id, "helpful")["id"] == memory_id
    assert {e["kind"] for e in backend.evidence(memory_id)} >= {"explicit", "feedback"}
    assert backend.history(memory_id) == []  # feedback adjusts belief; it is not an edit

    backend.forget(memory_id)
    assert backend.get(memory_id)["status"] == "archived"
    assert [v["status"] for v in backend.history(memory_id)] == ["active"]
    assert memory_id not in {m["id"] for m in backend.list()}
    assert memory_id in {m["id"] for m in backend.list(include_archived=True)}

    backend.forget(memory_id, permanent=True)
    with pytest.raises(NotFoundError):
        backend.get(memory_id)


def test_learn_repository_dry_run_and_real(backend, tmp_path):
    repo = _git_repo(tmp_path / "svc")
    dry = backend.learn_repository(str(repo), dry_run=True)
    assert dry.report is None and dry.findings
    assert backend.list() == []

    real = backend.learn_repository(str(repo))
    assert real.repo_id == "github.com/acme/payments"
    assert real.report is not None and real.report.counts["created"] >= 1
    again = backend.learn_repository(str(repo))
    assert again.report is not None and again.report.counts.get("created", 0) == 0  # idempotent


def test_rules_whoami_stats_export_import(backend, tmp_path):
    repo = _git_repo(tmp_path / "payments")
    backend.ingest([explicit_candidate("Money is stored as integer cents", scope="repo:github.com/acme/payments"),
                    explicit_candidate("Keep answers terse")], source="test")

    markdown, count = backend.rules(str(repo), audience="repo")
    assert count == 1 and "integer cents" in markdown and "terse" not in markdown
    _, personal = backend.rules(str(repo), audience="personal")
    assert personal == 2

    assert backend.whoami()["user_id"] == "local"
    stats = backend.stats()
    assert stats["active"] == 2 and stats["by_origin"] == {"explicit": 2}

    bundle = backend.export()
    assert len(bundle["memories"]) == 2
    assert backend.import_bundle(bundle).counts.get("created", 0) == 0


def test_open_backend_selects_remote_when_server_configured(test_settings):
    from pydantic import SecretStr

    from mnemosyne.client.backend import open_backend
    from mnemosyne.domain.exceptions import ConfigurationError

    remote = open_backend(test_settings.model_copy(update={"server_url": "https://mnemosyne.example.com",
                                                          "server_api_key": SecretStr("mn_x_y")}))
    assert remote.kind == "remote"
    remote.close()
    with pytest.raises(ConfigurationError):
        open_backend(test_settings.model_copy(update={"server_url": "https://mnemosyne.example.com"}))


def test_remote_backend_maps_http_errors(api_key_app):
    from fastapi.testclient import TestClient

    from mnemosyne.client.backend import RemoteBackend
    from mnemosyne.domain.exceptions import AuthenticationError

    backend = RemoteBackend("http://testserver", "mn_bad_key", client=TestClient(api_key_app))
    with pytest.raises(AuthenticationError):
        backend.whoami()
