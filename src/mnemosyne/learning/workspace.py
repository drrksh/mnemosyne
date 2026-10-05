"""
Workspace detection: which repository is this, and what is it written in?

Used on every hook invocation and MCP call, so it must be cheap: it reads
`.git` metadata directly rather than spawning `git` when it can, and
detects languages from manifest files rather than walking the tree.

Repository identity is the *normalized remote URL* (`github.com/acme/api`)
so the same repository cloned into different directories — or on a
teammate's machine — maps to the same memories. A repository without a
remote falls back to `local/<directory-name>-<path-hash>`.
"""
from __future__ import annotations

import configparser
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mnemosyne.domain.working_context import WorkingContext

EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".sql": "sql",
    ".dart": "dart",
    ".ex": "elixir", ".exs": "elixir",
    ".hs": "haskell",
    ".lua": "lua",
    ".tf": "terraform",
    ".vue": "javascript",
    ".svelte": "javascript",
}

#: Manifest file -> languages it implies (cheap language detection).
MANIFEST_LANGUAGES: dict[str, tuple[str, ...]] = {
    "pyproject.toml": ("python",),
    "setup.py": ("python",),
    "setup.cfg": ("python",),
    "requirements.txt": ("python",),
    "Pipfile": ("python",),
    "package.json": ("javascript",),
    "tsconfig.json": ("typescript", "javascript"),
    "deno.json": ("typescript",),
    "go.mod": ("go",),
    "Cargo.toml": ("rust",),
    "Gemfile": ("ruby",),
    "pom.xml": ("java",),
    "build.gradle": ("java",),
    "build.gradle.kts": ("kotlin",),
    "composer.json": ("php",),
    "mix.exs": ("elixir",),
    "pubspec.yaml": ("dart",),
    "Package.swift": ("swift",),
}

_SCP_LIKE = re.compile(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[^/].*)$")


@dataclass(frozen=True)
class RepoIdentity:
    root: Path
    repo_id: str
    name: str
    remote_url: Optional[str] = None
    head_sha: Optional[str] = None
    branch: Optional[str] = None


def find_repo_root(path: Path) -> Optional[Path]:
    current = path.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def normalize_remote(url: str) -> str:
    """`git@github.com:Acme/API.git`, `https://user@github.com/acme/api`,
    `ssh://git@github.com:22/acme/api.git` -> `github.com/acme/api`."""
    url = url.strip()
    match = _SCP_LIKE.match(url)
    if match and "://" not in url:
        host, path = match.group("host"), match.group("path")
    else:
        without_scheme = url.split("://", 1)[-1]
        without_user = without_scheme.split("@", 1)[-1] if "@" in without_scheme.split("/", 1)[0] else without_scheme
        host, _, path = without_user.partition("/")
        host = host.split(":", 1)[0]
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host.lower()}/{path.lower()}".strip("/")


def _git_dir(root: Path) -> Path:
    git = root / ".git"
    if git.is_file():  # worktree or submodule: "gitdir: <path>"
        text = git.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip())
            return target if target.is_absolute() else (root / target).resolve()
    return git


def _read_remote(git_dir: Path) -> Optional[str]:
    config_path = git_dir / "config"
    if not config_path.exists():
        common = git_dir / "commondir"
        if common.exists():
            config_path = (git_dir / common.read_text().strip()).resolve() / "config"
    if not config_path.exists():
        return None
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(config_path, encoding="utf-8")
    except configparser.Error:
        return None
    remotes = [s for s in parser.sections() if s.startswith("remote ")]
    preferred = 'remote "origin"' if 'remote "origin"' in remotes else (remotes[0] if remotes else None)
    return parser.get(preferred, "url", fallback=None) if preferred else None


def _read_head(git_dir: Path) -> tuple[Optional[str], Optional[str]]:
    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        branch = ref.removeprefix("refs/heads/")
        for base in (git_dir, git_dir / ".." if (git_dir / "commondir").exists() else git_dir):
            ref_file = base / ref
            if ref_file.exists():
                return ref_file.read_text().strip(), branch
        packed = git_dir / "packed-refs"
        if packed.exists():
            for line in packed.read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0], branch
        return None, branch
    return head or None, None


def detect_repo(path: Path | str) -> Optional[RepoIdentity]:
    root = find_repo_root(Path(path))
    if root is None:
        return None
    git_dir = _git_dir(root)
    remote = _read_remote(git_dir)
    head_sha, branch = _read_head(git_dir)
    if remote:
        repo_id = normalize_remote(remote)
    else:
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", root.name) or "repo"
        repo_id = f"local/{safe_name}-{digest}"
    return RepoIdentity(root=root, repo_id=repo_id, name=root.name, remote_url=remote, head_sha=head_sha, branch=branch)


def manifest_languages(root: Path) -> list[str]:
    found: dict[str, None] = {}
    try:
        names = set(os.listdir(root))
    except OSError:
        return []
    for manifest, languages in MANIFEST_LANGUAGES.items():
        if manifest in names:
            for language in languages:
                found.setdefault(language, None)
    if "package.json" in names and "typescript" not in found:
        try:
            if '"typescript"' in (root / "package.json").read_text(encoding="utf-8", errors="replace"):
                found.setdefault("typescript", None)
        except OSError:
            pass
    return list(found)


def language_for_path(path: str | Path) -> Optional[str]:
    return EXTENSION_LANGUAGES.get(Path(path).suffix.lower())


def detect_working_context(
    cwd: Path | str | None,
    *,
    file_path: Optional[str] = None,
    extra_languages: Optional[list[str]] = None,
) -> WorkingContext:
    """Best-effort working context for a directory (and optional file)."""
    languages: list[str] = []
    if file_path:
        language = language_for_path(file_path)
        if language:
            languages.append(language)
    repo = detect_repo(cwd) if cwd else None
    if repo is not None:
        languages.extend(manifest_languages(repo.root))
    elif cwd:
        languages.extend(manifest_languages(Path(cwd)))
    languages.extend(extra_languages or [])
    if "typescript" in languages and "javascript" not in languages:
        languages.append("javascript")  # TS projects live in the JS ecosystem (package managers, linters)
    return WorkingContext.create(
        repo_id=repo.repo_id if repo else None,
        languages=languages,
        file_path=file_path,
        repo_name=repo.name if repo else None,
    )


def run_git(root: Path, *args: str, timeout: float = 20.0) -> Optional[str]:
    """Run a read-only git command; None if git is unavailable or fails."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None
