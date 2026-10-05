"""
Scope: *where* a memory applies.

A developer's knowledge is layered. "Use 4-space indentation" might be
true everywhere they write Python, "this service talks to Postgres via
SQLAlchemy" is true only inside one repository, and "keep answers terse"
is true everywhere. Scope makes that layering explicit so the context
compiler can (a) leave out memories that don't apply to the current
working directory and (b) let the most specific memory win when two
memories claim the same slot (repo beats language beats global).

Scopes are stored as canonical strings (`global`, `lang:python`,
`repo:github.com/acme/api`) so they index well and read clearly in the
database, logs, and API payloads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from mnemosyne.domain.exceptions import ValidationError


class ScopeKind(str, Enum):
    GLOBAL = "global"
    LANGUAGE = "lang"
    REPOSITORY = "repo"


#: Higher = more specific. Used to resolve slot conflicts across scopes.
SCOPE_SPECIFICITY: dict[ScopeKind, int] = {
    ScopeKind.GLOBAL: 0,
    ScopeKind.LANGUAGE: 1,
    ScopeKind.REPOSITORY: 2,
}

LANGUAGE_ALIASES: dict[str, str] = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "node": "javascript",
    "nodejs": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "jsx": "javascript",
    "golang": "go",
    "rs": "rust",
    "rb": "ruby",
    "kt": "kotlin",
    "c++": "cpp",
    "cxx": "cpp",
    "c#": "csharp",
    "cs": "csharp",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
}

_LANGUAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+#._-]{0,39}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:/@+-]{0,253}$")


def normalize_language(language: str) -> str:
    lang = (language or "").strip().lower()
    return LANGUAGE_ALIASES.get(lang, lang)


@dataclass(frozen=True, slots=True)
class Scope:
    kind: ScopeKind
    value: str = ""

    # ------------------------------------------------------------------
    @classmethod
    def global_(cls) -> Scope:
        return cls(ScopeKind.GLOBAL, "")

    @classmethod
    def language(cls, language: str) -> Scope:
        lang = normalize_language(language)
        if not _LANGUAGE_RE.match(lang):
            raise ValidationError(f"invalid language scope: {language!r}")
        return cls(ScopeKind.LANGUAGE, lang)

    @classmethod
    def repository(cls, repo_id: str) -> Scope:
        repo = (repo_id or "").strip().strip("/")
        if not _REPO_RE.match(repo):
            raise ValidationError(f"invalid repository scope: {repo_id!r}")
        return cls(ScopeKind.REPOSITORY, repo)

    @classmethod
    def parse(cls, raw: str | Scope | None) -> Scope:
        if isinstance(raw, Scope):
            return raw
        text = (raw or "global").strip()
        if text in ("", "global", "*"):
            return cls.global_()
        prefix, sep, rest = text.partition(":")
        if not sep:
            raise ValidationError(
                f"invalid scope {raw!r}: expected 'global', 'lang:<language>' or 'repo:<repo-id>'"
            )
        prefix = prefix.strip().lower()
        if prefix in ("lang", "language"):
            return cls.language(rest)
        if prefix in ("repo", "repository"):
            return cls.repository(rest)
        raise ValidationError(f"unknown scope kind {prefix!r} in {raw!r}")

    # ------------------------------------------------------------------
    @property
    def specificity(self) -> int:
        return SCOPE_SPECIFICITY[self.kind]

    @property
    def is_global(self) -> bool:
        return self.kind == ScopeKind.GLOBAL

    def __str__(self) -> str:
        if self.kind == ScopeKind.GLOBAL:
            return "global"
        return f"{self.kind.value}:{self.value}"


def canonical_scope(raw: str | Scope | None) -> str:
    """Validate and canonicalize a scope string (`Lang:PY` -> `lang:python`)."""
    return str(Scope.parse(raw))
