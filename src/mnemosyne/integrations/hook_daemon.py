"""
A warm, per-user daemon that answers Claude Code hooks in milliseconds.

Claude Code starts a new process for every hook, and UserPromptSubmit
runs before *every* prompt. Starting Python and importing the database
and retrieval stack costs most of a second, of which the actual work is
a few tens of milliseconds. So the first hook of a session answers the
slow way and starts this daemon in the background; later hooks hand
their event to it over a Unix socket using only the standard library.

* Access control is the filesystem: the socket lives in `<home>/run`
  (mode 0700) and is itself mode 0600, so only the user can connect.
* It is disposable. Any failure to reach it makes the hook fall back to
  in-process handling, and the daemon exits by itself when idle, when
  `<home>/config.env` changes, or when another version is installed
  (the socket name carries the version).
* One request at a time: hooks are short, and a single writer suits
  SQLite.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import signal
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, cast

from mnemosyne import __version__

if TYPE_CHECKING:  # pragma: no cover
    from http.server import BaseHTTPRequestHandler
    from socketserver import UnixStreamServer

    from mnemosyne.client.backend import Backend
    from mnemosyne.config import Settings
    from mnemosyne.integrations.claude_code import HookName

logger = logging.getLogger("mnemosyne.daemon")

SUPPORTED = hasattr(socket, "AF_UNIX") and os.name == "posix"
DEFAULT_IDLE_SECONDS = 30 * 60


#: Unix socket paths are limited to ~104 bytes (macOS) / 108 (Linux).
_MAX_SOCKET_PATH = 100


def run_dir(home: Path) -> Path:
    """Where the socket and lock live: `<home>/run`, or, when that path is
    too long for a Unix socket, a per-user directory under
    $XDG_RUNTIME_DIR or the temp directory."""
    preferred = home / "run"
    if len(os.fsencode(str(preferred / f"hooks-{__version__}.sock"))) <= _MAX_SOCKET_PATH:
        return preferred
    import hashlib
    import tempfile

    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    digest = hashlib.blake2b(str(home.resolve()).encode(), digest_size=6).hexdigest()
    return Path(base) / f"mnemosyne-{os.getuid()}-{digest}"


def socket_path(home: Path) -> Path:
    return run_dir(home) / f"hooks-{__version__}.sock"


def lock_path(home: Path) -> Path:
    return run_dir(home) / f"hooks-{__version__}.lock"


def _private_to_me(path: Path) -> bool:
    """True when `path` belongs to this user and no one else can touch it.
    The client checks the socket and its directory before sending a
    prompt: in a shared temp directory another user could otherwise
    stand up a fake daemon and read what is typed."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return info.st_uid == os.getuid() and info.st_mode & 0o077 == 0


def _prepare_run_dir(home: Path) -> Optional[Path]:
    directory = run_dir(home)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or directory.lstat().st_uid != os.getuid():
        logger.error("daemon_run_dir_not_owned", extra={"path": str(directory)})
        return None
    os.chmod(directory, 0o700)
    return directory


# ----------------------------------------------------------------------
# Client: standard library only, so the hook process stays light.
# ----------------------------------------------------------------------
class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def _request(home: Path, method: str, path: str, body: bytes = b"", *, timeout: float) -> Optional[dict]:
    target = socket_path(home)
    if not SUPPORTED or not target.exists():
        return None
    if not (_private_to_me(target.parent) and _private_to_me(target)):
        return None
    connection = _UnixHTTPConnection(str(target), timeout)
    try:
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = response.read()
        if response.status != 200:
            return None
        data = json.loads(payload)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def call_daemon(hook: str, stdin_text: str, home: Path, *, timeout: float) -> Optional[tuple[str, int]]:
    """Ask the daemon to handle a hook event. None means "do it yourself"."""
    data = _request(home, "POST", f"/hook/{hook}", stdin_text.encode("utf-8"), timeout=timeout)
    if data is None or not isinstance(data.get("stdout"), str):
        return None
    return data["stdout"], int(data.get("exit_code", 0))


def daemon_status(home: Path, *, timeout: float = 1.0) -> Optional[dict]:
    return _request(home, "GET", "/health", timeout=timeout)


def stop_daemon(home: Path, *, timeout: float = 2.0) -> bool:
    status = daemon_status(home)
    if status is None:
        return False
    try:
        os.kill(int(status["pid"]), signal.SIGTERM)
    except (OSError, KeyError, ValueError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and socket_path(home).exists():
        time.sleep(0.05)
    return True


def start_daemon_detached(home: Path, command: list[str]) -> bool:
    """Start the daemon in the background unless one is running or starting."""
    from mnemosyne.integrations.claude_code import spawn_detached

    if not SUPPORTED or os.environ.get("MNEMOSYNE_HOOK_DAEMON", "").lower() in ("0", "false", "no"):
        return False
    if _lock_is_held(home):
        return False
    spawn_detached([*command, "daemon", "run"], log_name="daemon.log")
    return True


def _lock_is_held(home: Path) -> bool:
    import fcntl

    path = lock_path(home)
    if not path.exists():
        return False
    try:
        with open(path, "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


# ----------------------------------------------------------------------
# Server
# ----------------------------------------------------------------------
class _NoCloseBackend:
    """The daemon's backend outlives each hook; run_hook closes what it is
    given, so hand it a view whose close() does nothing."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    def close(self) -> None:
        return None


class HookDaemon:
    def __init__(
        self,
        settings: Settings,
        *,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        backend_factory: Optional[Callable[[], Backend]] = None,
        spawn: Optional[Callable[[list[str]], None]] = None,
    ) -> None:
        self.settings = settings
        self.home = settings.home
        self.idle_seconds = idle_seconds
        self._backend_factory = backend_factory
        self._backend: Optional[Backend] = None
        self._spawn = spawn
        self._last_activity = time.monotonic()
        self._stop = False
        self._config_stamp = self._config_signature()
        self.requests = 0
        self.ready_at: Optional[float] = None

    # -- lifecycle ---------------------------------------------------------
    def _config_signature(self) -> Optional[float]:
        try:
            return (self.home / "config.env").stat().st_mtime
        except OSError:
            return None

    def backend(self) -> Backend:
        if self._backend is None:
            if self._backend_factory is not None:
                self._backend = self._backend_factory()
            else:
                from mnemosyne.client.backend import open_backend

                self._backend = open_backend(self.settings)
        return self._backend

    def _warm_up(self) -> None:
        backend = self.backend()
        try:
            backend.context(task="warm up", cwd=str(self.home), max_tokens=200, record_access=False)
        except Exception:  # warming is an optimisation; requests will surface real errors
            logger.debug("daemon_warm_up_failed", exc_info=True)

    def request_stop(self, *_: Any) -> None:
        self._stop = True

    def should_stop(self) -> bool:
        if self._stop:
            return True
        if time.monotonic() - self._last_activity > self.idle_seconds:
            logger.info("daemon_idle_exit")
            return True
        if self._config_signature() != self._config_stamp:
            logger.info("daemon_config_changed_exit")
            return True
        return False

    def serve(self, *, ready: Optional[Callable[[], None]] = None, install_signals: bool = True) -> int:
        """Run until stopped. Returns 0, or 1 when another daemon owns the
        lock (not an error: there is already one)."""
        import fcntl
        from socketserver import UnixStreamServer

        if _prepare_run_dir(self.home) is None:
            return 1
        lock = open(lock_path(self.home), "a+")  # noqa: SIM115 - held for the daemon's lifetime
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            return 1
        path = socket_path(self.home)
        server: Optional[UnixStreamServer] = None
        try:
            if install_signals:
                signal.signal(signal.SIGTERM, self.request_stop)
                signal.signal(signal.SIGINT, self.request_stop)
            # Warm up before listening, so a hook never waits on a half-ready
            # daemon: until the socket exists, hooks answer in-process.
            self._warm_up()
            path.unlink(missing_ok=True)  # stale: we hold the lock, so nobody is serving it
            previous_umask = os.umask(0o177)
            try:
                server = UnixStreamServer(str(path), _handler_for(self))
            finally:
                os.umask(previous_umask)
            os.chmod(path, 0o600)
            server.timeout = 0.5
            self._last_activity = time.monotonic()
            self.ready_at = time.time()
            logger.info("daemon_ready", extra={"socket": str(path), "pid": os.getpid()})
            if ready:
                ready()
            while not self.should_stop():
                server.handle_request()
            return 0
        finally:
            if server is not None:
                server.server_close()
            path.unlink(missing_ok=True)
            if self._backend is not None:
                self._backend.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    # -- request handling --------------------------------------------------
    def handle_hook(self, hook: str, body: str) -> dict[str, Any]:
        from mnemosyne.integrations.claude_code import HOOK_EVENTS, run_hook

        self._last_activity = time.monotonic()
        self.requests += 1
        if hook not in {name for name, _, _ in HOOK_EVENTS.values()}:
            return {"stdout": "", "exit_code": 0}
        output = run_hook(cast("HookName", hook), body, settings=self.settings,
                          backend_factory=lambda: cast("Backend", _NoCloseBackend(self.backend())),
                          spawn=self._spawn)
        return {"stdout": output.stdout, "exit_code": output.exit_code}

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "pid": os.getpid(), "requests": self.requests,
                "ready_at": self.ready_at, "idle_seconds": round(time.monotonic() - self._last_activity, 1),
                "backend": getattr(self._backend, "kind", None)}


def _handler_for(daemon: HookDaemon) -> type[BaseHTTPRequestHandler]:
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, daemon.health())
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self.path.startswith("/hook/"):
                self._reply(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > daemon.settings.max_request_bytes:
                self._reply(413, {"error": "too large"})
                return
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            self._reply(200, daemon.handle_hook(self.path.removeprefix("/hook/"), body))

        def log_message(self, format: str, *args: Any) -> None:
            return None

        def address_string(self) -> str:
            return "unix"

    return Handler
