"""
WorkingContext: *what the developer is working on right now*.

Retrieval and context compilation are relative to it: inside
`github.com/acme/api`, a memory scoped to that repository is highly
relevant, a Python convention is relevant if the repo is Python, and a
memory scoped to some *other* repository is noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from mnemosyne.domain.scope import Scope, ScopeKind, normalize_language


@dataclass(frozen=True)
class WorkingContext:
    repo_id: Optional[str] = None
    languages: tuple[str, ...] = field(default_factory=tuple)
    file_path: Optional[str] = None
    #: Human-readable name of the repository (for rendering only).
    repo_name: Optional[str] = None

    @classmethod
    def create(
        cls,
        *,
        repo_id: Optional[str] = None,
        languages: Optional[list[str] | tuple[str, ...]] = None,
        file_path: Optional[str] = None,
        repo_name: Optional[str] = None,
    ) -> WorkingContext:
        langs = tuple(dict.fromkeys(normalize_language(lang) for lang in (languages or ()) if lang))
        return cls(repo_id=repo_id or None, languages=langs, file_path=file_path, repo_name=repo_name)

    @property
    def is_empty(self) -> bool:
        return not self.repo_id and not self.languages

    def with_languages(self, languages: list[str] | tuple[str, ...]) -> WorkingContext:
        merged = tuple(dict.fromkeys([*self.languages, *(normalize_language(lang) for lang in languages)]))
        return replace(self, languages=merged)

    def applicable_scopes(self) -> list[str]:
        scopes = ["global"]
        scopes.extend(f"lang:{lang}" for lang in self.languages)
        if self.repo_id:
            scopes.append(f"repo:{self.repo_id}")
        return scopes

    def scope_affinity(self, scope: str) -> Optional[float]:
        """How strongly a memory with `scope` applies here, in [0, 1], or
        None when it does not apply at all (another repo / language).

        With an empty context nothing is known, so every scope applies at
        a neutral affinity — that is what a plain search wants."""
        try:
            parsed = Scope.parse(scope)
        except Exception:
            return None
        if self.is_empty:
            return 0.5
        if parsed.kind == ScopeKind.GLOBAL:
            return 0.6
        if parsed.kind == ScopeKind.LANGUAGE:
            if parsed.value in self.languages:
                return 0.85
            return None if self.languages else 0.4
        if parsed.kind == ScopeKind.REPOSITORY:
            if self.repo_id and parsed.value == self.repo_id:
                return 1.0
            return None
        return None
