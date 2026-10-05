"""The warm hook daemon: same answers as in-process hooks, private socket,
single instance, and it gets out of the way by itself."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from mnemosyne.client.explicit import explicit_candidate
from mnemosyne.integrations import hook_daemon
from mnemosyne.integrations.claude_code import run_hook
from mnemosyne.integrations.hook_daemon import HookDaemon, call_daemon, daemon_status, socket_path

pytestmark = pytest.mark.skipif(not hook_daemon.SUPPORTED, reason="needs Unix domain sockets")


class _Running:
    def __init__(self, daemon: HookDaemon) -> None:
        self.daemon = daemon
        self.ready = threading.Event()
        self.result: list[int] = []
        self.thread = threading.Thread(
            target=lambda: self.result.append(daemon.serve(ready=self.ready.set, install_signals=False)), daemon=True
        )

    def __enter__(self) -> _Running:
        self.thread.start()
        assert self.ready.wait(10), "daemon did not become ready"
        return self

    def stop(self) -> int:
        self.daemon.request_stop()
        self.thread.join(5)
        assert not self.thread.is_alive()
        return self.result[0]

    def __exit__(self, *exc: object) -> None:
        if self.thread.is_alive():
            self.stop()


@pytest.fixture
def settings(test_settings, _isolated_home):
    return test_settings.model_copy(update={"home": _isolated_home})


@pytest.fixture
def make_daemon(settings, local_backend):
    def make(**kwargs) -> HookDaemon:
        return HookDaemon(settings, backend_factory=lambda: local_backend, spawn=lambda argv: None, **kwargs)

    return make


def test_daemon_answers_exactly_like_in_process(make_daemon, local_backend, settings):
    local_backend.ingest([explicit_candidate("Never force-push to shared branches")], source="t")
    event = json.dumps({"cwd": "/nonexistent", "source": "startup"})
    expected = run_hook("session-start", event, settings=settings, backend_factory=lambda: _Keep(local_backend))

    with _Running(make_daemon()) as running:
        answered = call_daemon("session-start", event, settings.home, timeout=5)
        assert answered == (expected.stdout, 0) and "force-push" in answered[0]
        status = daemon_status(settings.home)
        assert status["requests"] == 1 and status["pid"] == os.getpid() and status["backend"] == "local"
        assert running.stop() == 0
    assert not socket_path(settings.home).exists()


def test_daemon_learns_from_prompts(make_daemon, local_backend, settings):
    with _Running(make_daemon()):  # (the daemon closes its backend on exit, so check while it runs)
        stdout, _ = call_daemon("user-prompt", json.dumps({"prompt": "Use pnpm, never npm."}), settings.home,
                                timeout=5)
        assert "Prefers pnpm over npm" in stdout
        assert [m["value"] for m in local_backend.list()] == ["pnpm"]


def test_socket_is_private_and_client_refuses_anything_else(make_daemon, settings):
    with _Running(make_daemon()):
        path = socket_path(settings.home)
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        os.chmod(path, 0o666)
        assert call_daemon("session-start", "{}", settings.home, timeout=2) is None
        os.chmod(path, 0o600)
        assert call_daemon("session-start", "{}", settings.home, timeout=2) is not None


def test_only_one_daemon_per_home(make_daemon):
    with _Running(make_daemon()):
        assert make_daemon().serve(install_signals=False) == 1


def test_idle_and_config_changes_end_the_daemon(make_daemon, settings):
    started = time.monotonic()
    assert make_daemon(idle_seconds=0.3).serve(install_signals=False) == 0
    assert time.monotonic() - started < 5

    with _Running(make_daemon()) as running:
        settings.home.mkdir(parents=True, exist_ok=True)
        (settings.home / "config.env").write_text("MNEMOSYNE_HOOK_MAX_CHARS=4000\n")
        running.thread.join(5)
        assert not running.thread.is_alive() and running.result == [0]


def test_unknown_hooks_and_missing_daemon(make_daemon, settings):
    assert call_daemon("session-start", "{}", settings.home, timeout=1) is None
    assert daemon_status(settings.home) is None
    with _Running(make_daemon()):
        assert call_daemon("rm-rf", "{}", settings.home, timeout=2) == ("", 0)


def test_long_homes_fall_back_to_a_short_private_directory(tmp_path):
    from pathlib import Path

    deep = tmp_path / ("x" * 120)
    path = socket_path(deep)
    assert len(str(path)) <= 108 and path.parent.name.startswith(f"mnemosyne-{os.getuid()}-")
    assert socket_path(Path("/home/dev/.mnemosyne")).parent == Path("/home/dev/.mnemosyne/run")


def test_cli_hook_prefers_the_daemon(monkeypatch, _isolated_home):
    from click.testing import CliRunner

    from mnemosyne.cli.main import cli

    calls = []

    def fake_call(hook, stdin_text, home, *, timeout):
        calls.append((hook, stdin_text, timeout))
        return '{"from": "daemon"}', 0

    monkeypatch.setattr(hook_daemon, "call_daemon", fake_call)
    result = CliRunner().invoke(cli, ["hook", "user-prompt"], input='{"prompt": "hi"}')
    assert result.exit_code == 0 and result.output.strip() == '{"from": "daemon"}'
    assert calls == [("user-prompt", '{"prompt": "hi"}', 7.0)]
    assert not (_isolated_home / "mnemosyne.db").exists()  # the fast path never opened the database


class _Keep:
    def __init__(self, backend):
        self._backend = backend

    def __getattr__(self, name):
        return getattr(self._backend, name)

    def close(self):
        return None
