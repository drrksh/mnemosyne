"""
Backends: one interface for every client surface (CLI, MCP server,
editor hooks), two implementations.

* `LocalBackend` — the developer's own memory database, via the
  in-process `Mnemosyne` container. Zero setup; the default.
* `RemoteBackend` — a Mnemosyne server (team or company deployment)
  over the REST API with an API key. Selected by setting
  `MNEMOSYNE_SERVER_URL` and `MNEMOSYNE_SERVER_API_KEY`.

Work that touches the developer's machine always happens locally, even
in remote mode: repositories are analysed on the client and only the
resulting findings are sent; transcripts are parsed and redacted on the
client before they leave it.

Results are plain dataclasses and JSON-shaped dicts so that both
backends return exactly the same shapes.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, TypeVar
from uuid import UUID

from mnemosyne.context.render import RenderFormat
from mnemosyne.core.redaction import redact
from mnemosyne.domain.learning import CandidateMemory, IngestReport
from mnemosyne.learning.conversation.transcript import Transcript
from mnemosyne.learning.workspace import detect_working_context

if TYPE_CHECKING:  # pragma: no cover
    from mnemosyne.config import Settings
    from mnemosyne.container import Mnemosyne
    from mnemosyne.domain.entities import Memory
    from mnemosyne.domain.principal import Principal
    from mnemosyne.integrations.exporters import Audience
    from mnemosyne.learning.repo.analyzer import RepoAnalysis

logger = logging.getLogger("mnemosyne.client")


@dataclass
class ContextResult:
    text: str
    items: list[dict[str, Any]] = field(default_factory=list)
    estimated_tokens: int = 0
    repo_id: Optional[str] = None
    languages: list[str] = field(default_factory=list)

    @property
    def relevant_count(self) -> int:
        return sum(1 for i in self.items if i.get("reason") == "relevant")


@dataclass
class ReportResult:
    summary: str
    counts: dict[str, int]
    outcomes: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_report(cls, report: IngestReport) -> ReportResult:
        return cls(
            summary=report.summary_line(),
            counts=report.counts,
            outcomes=[
                {"action": o.action.value, "title": o.title, "reason": o.reason,
                 "memory_id": str(o.memory_id) if o.memory_id else None, "key": o.key,
                 "previous_value": o.previous_value, "new_value": o.new_value}
                for o in report.outcomes
            ],
        )

    @property
    def learned(self) -> list[dict[str, Any]]:
        return [o for o in self.outcomes if o["action"] in ("created", "superseded")]


@dataclass
class RepositoryResult:
    repo_id: str
    languages: dict[str, int]
    findings: list[dict[str, Any]]
    duration_ms: float
    report: Optional[ReportResult]
    generalization: Optional[ReportResult]


RENDER_FORMATS: tuple[RenderFormat, ...] = ("markdown", "xml", "json")
AUDIENCES: tuple[Audience, ...] = ("repo", "personal")
_T = TypeVar("_T", bound=str)


def _choice(value: str, allowed: tuple[_T, ...], name: str) -> _T:
    for option in allowed:
        if value == option:
            return option
    from mnemosyne.domain.exceptions import ValidationError

    raise ValidationError(f"{name} must be one of {', '.join(allowed)}")


def _candidate_payload(candidate: CandidateMemory) -> dict[str, Any]:
    return {
        "title": candidate.title, "content": candidate.content, "summary": candidate.summary,
        "memory_type": candidate.memory_type.value,
        "origin": candidate.origin.value if candidate.origin.value != "system" else "inferred",
        "confidence": candidate.confidence, "importance": candidate.importance, "scope": candidate.scope,
        "key": candidate.key, "value": candidate.value, "tags": candidate.tags, "team_id": candidate.team_id,
        "pinned": candidate.pinned, "metadata": candidate.metadata,
        "evidence": [{"kind": e.kind.value, "source_ref": e.source_ref, "excerpt": e.excerpt, "weight": e.weight}
                     for e in candidate.evidence],
    }


def _findings(analysis: RepoAnalysis) -> list[dict[str, Any]]:
    return [{"key": f.key, "value": f.value, "title": f.title, "confidence": f.confidence, "analyzer": f.analyzer}
            for f in analysis.findings]


class Backend(ABC):
    kind: str

    @abstractmethod
    def context(self, *, task: Optional[str], cwd: Optional[str], file_path: Optional[str] = None,
                max_tokens: int = 1500, max_chars: Optional[int] = None, fmt: str = "markdown", wrap: bool = False,
                include_profile: bool = True, record_access: bool = True) -> ContextResult: ...

    @abstractmethod
    def recall(self, query: str, *, cwd: Optional[str], limit: int = 8, explain: bool = False) -> list[dict]: ...

    @abstractmethod
    def ingest(self, candidates: list[CandidateMemory], *, source: str) -> ReportResult: ...

    @abstractmethod
    def learn_conversation(self, transcript: Transcript, *, cwd: Optional[str], use_llm: bool = False) -> ReportResult:
        ...

    @abstractmethod
    def learn_repository(self, path: str, *, author_email: Optional[str] = None, dry_run: bool = False,
                         generalize: bool = True) -> RepositoryResult: ...

    @abstractmethod
    def feedback(self, memory_id: str, signal: str, note: Optional[str] = None) -> dict: ...

    @abstractmethod
    def forget(self, memory_id: str, *, permanent: bool = False) -> None: ...

    @abstractmethod
    def get(self, memory_id: str) -> dict: ...

    @abstractmethod
    def evidence(self, memory_id: str) -> list[dict]: ...

    @abstractmethod
    def history(self, memory_id: str) -> list[dict]: ...

    @abstractmethod
    def list(self, *, memory_types: Optional[list[str]] = None, scopes: Optional[list[str]] = None,
             limit: int = 50, include_archived: bool = False) -> list[dict]: ...

    @abstractmethod
    def export(self) -> dict: ...

    @abstractmethod
    def rules(self, cwd: str, *, audience: str = "repo") -> tuple[str, int]:
        """Rule-file body (markdown) for the repository at `cwd`, and how
        many memories it holds."""

    @abstractmethod
    def whoami(self) -> dict: ...

    @abstractmethod
    def stats(self) -> dict: ...

    @abstractmethod
    def import_bundle(self, bundle: dict) -> ReportResult: ...

    def close(self) -> None:  # pragma: no cover - trivial
        return None


# ----------------------------------------------------------------------
class LocalBackend(Backend):
    kind = "local"

    def __init__(self, container: Mnemosyne) -> None:
        self.container = container
        self._principal: Optional[Principal] = None

    @property
    def principal(self) -> Principal:
        if self._principal is None:
            self._principal = self.container.local_principal()
        return self._principal

    @staticmethod
    def _memory_json(memory: Memory) -> dict:
        from mnemosyne.api.schemas import MemoryResponse

        return MemoryResponse.from_domain(memory).model_dump(mode="json")

    def context(self, *, task: Optional[str], cwd: Optional[str], file_path: Optional[str] = None,
                max_tokens: int = 1500, max_chars: Optional[int] = None, fmt: str = "markdown", wrap: bool = False,
                include_profile: bool = True, record_access: bool = True) -> ContextResult:
        from mnemosyne.services.context_service import ContextRequest

        working = detect_working_context(cwd, file_path=file_path) if cwd else detect_working_context(None)
        pack = self.container.context.compile(
            ContextRequest(task=task, context=working, max_tokens=max_tokens, max_chars=max_chars,
                           format=_choice(fmt, RENDER_FORMATS, "format"), wrap=wrap,
                           include_profile=include_profile, record_access=record_access),
            self.principal,
        )
        return ContextResult(
            text=pack.text,
            items=[{"id": str(i.memory.id), "title": i.memory.title, "reason": i.reason, "section": i.section,
                    "memory_type": i.memory.memory_type.value, "scope": i.memory.scope} for i in pack.items],
            estimated_tokens=pack.estimated_tokens,
            repo_id=pack.context.repo_id,
            languages=list(pack.context.languages),
        )

    def recall(self, query: str, *, cwd: Optional[str], limit: int = 8, explain: bool = False) -> list[dict]:
        from mnemosyne.services.retrieval_service import RecallRequest

        working = detect_working_context(cwd) if cwd else detect_working_context(None)
        result = self.container.retrieval.recall(RecallRequest(query=query, context=working, limit=limit),
                                                 self.principal)
        return [{"memory": self._memory_json(s.memory), "score": round(s.score, 4),
                 "breakdown": s.breakdown.as_dict() if explain else None} for s in result.items]

    def ingest(self, candidates: list[CandidateMemory], *, source: str) -> ReportResult:
        return ReportResult.from_report(self.container.learning.ingest(candidates, principal=self.principal,
                                                                       source_label=source))

    def learn_conversation(self, transcript: Transcript, *, cwd: Optional[str], use_llm: bool = False) -> ReportResult:
        working = detect_working_context(cwd) if cwd else None
        report = self.container.learning.learn_conversation(
            transcript, principal=self.principal, context=working,
            use_llm=use_llm and self.container.settings.llm_extraction,
        )
        return ReportResult.from_report(report)

    def learn_repository(self, path: str, *, author_email: Optional[str] = None, dry_run: bool = False,
                         generalize: bool = True) -> RepositoryResult:
        result = self.container.learning.learn_repository(path, principal=self.principal, author_email=author_email,
                                                          dry_run=dry_run, generalize=generalize)
        return RepositoryResult(
            repo_id=result.analysis.identity.repo_id,
            languages=result.analysis.languages,
            findings=_findings(result.analysis),
            duration_ms=result.analysis.duration_ms,
            report=ReportResult.from_report(result.report) if result.report else None,
            generalization=ReportResult.from_report(result.generalization) if result.generalization else None,
        )

    def feedback(self, memory_id: str, signal: str, note: Optional[str] = None) -> dict:
        from mnemosyne.domain.enums import FeedbackSignal

        memory = self.container.learning.feedback(UUID(memory_id), FeedbackSignal(signal), note=note,
                                                  principal=self.principal)
        return self._memory_json(memory)

    def forget(self, memory_id: str, *, permanent: bool = False) -> None:
        if permanent:
            self.container.memories.hard_delete_memory(UUID(memory_id), principal=self.principal)
        else:
            self.container.memories.archive_memory(UUID(memory_id), principal=self.principal)

    def get(self, memory_id: str) -> dict:
        return self._memory_json(self.container.memories.get_memory(UUID(memory_id), principal=self.principal,
                                                                    include_deleted=True, track_access=False))

    def evidence(self, memory_id: str) -> list[dict]:
        return [{"kind": e.kind.value, "source_ref": e.source_ref, "excerpt": e.excerpt, "weight": e.weight,
                 "observed_at": e.observed_at.isoformat()}
                for e in self.container.memories.list_evidence(UUID(memory_id), principal=self.principal)]

    def history(self, memory_id: str) -> list[dict]:
        return [{"version": v.version_number, "title": v.title, "value": v.value, "status": v.status.value,
                 "changed_fields": v.changed_fields, "change_reason": v.change_reason,
                 "recorded_at": v.recorded_at.isoformat()}
                for v in self.container.memories.list_versions(UUID(memory_id), principal=self.principal)]

    def list(self, *, memory_types: Optional[list[str]] = None, scopes: Optional[list[str]] = None,
             limit: int = 50, include_archived: bool = False) -> list[dict]:
        from mnemosyne.domain.dto import SearchCriteria
        from mnemosyne.domain.enums import MemoryStatus, MemoryType
        from mnemosyne.domain.scope import canonical_scope

        criteria = SearchCriteria(
            memory_types=[MemoryType(t) for t in memory_types] if memory_types else None,
            scopes=[canonical_scope(s) for s in scopes] if scopes else None,
            statuses=[MemoryStatus.ACTIVE, MemoryStatus.ARCHIVED] if include_archived else [MemoryStatus.ACTIVE],
            page_size=max(1, min(limit, 200)),
            sort_by="importance_score",
        )
        return [self._memory_json(m) for m in
                self.container.memories.search_memories(criteria, principal=self.principal).items]

    def export(self) -> dict:
        return self.container.portability.export(principal=self.principal)

    def import_bundle(self, bundle: dict) -> ReportResult:
        return ReportResult.from_report(self.container.portability.import_bundle(bundle, principal=self.principal))

    def rules(self, cwd: str, *, audience: str = "repo") -> tuple[str, int]:
        from mnemosyne.integrations.exporters import build_profile_markdown

        return build_profile_markdown(self.container, detect_working_context(cwd),
                                      audience=_choice(audience, AUDIENCES, "audience"), principal=self.principal)

    def whoami(self) -> dict:
        from mnemosyne.db.session import sqlite_file_path

        url = self.container.settings.database_url
        database = str(sqlite_file_path(url)) if url.startswith("sqlite") else url.split("@")[-1]
        return {"mode": "local", "user_id": self.principal.user_id, "team_ids": sorted(self.principal.team_ids),
                "database": database}

    def stats(self) -> dict:
        return self.container.memories.stats(principal=self.principal)

    def close(self) -> None:
        self.container.close()


# ----------------------------------------------------------------------
class RemoteBackend(Backend):
    kind = "remote"

    def __init__(self, url: str, api_key: str, *, timeout: float = 15.0, client: Any = None) -> None:
        import httpx

        self.url = url.rstrip("/")
        self._client = client or httpx.Client(
            base_url=self.url, timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        from mnemosyne.domain.exceptions import (
            AuthenticationError,
            MnemosyneError,
            NotFoundError,
            PermissionDeniedError,
            RepositoryError,
            ValidationError,
        )

        try:
            response = self._client.request(method, path, **kwargs)
        except Exception as exc:
            raise RepositoryError(f"cannot reach Mnemosyne server at {self.url}: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            error = {401: AuthenticationError, 403: PermissionDeniedError, 404: NotFoundError,
                     422: ValidationError}.get(response.status_code, MnemosyneError)
            raise error(f"server returned {response.status_code}: {detail}")
        return response.json() if response.content else None

    @staticmethod
    def _context_payload(cwd: Optional[str], file_path: Optional[str] = None) -> dict[str, Any]:
        working = detect_working_context(cwd, file_path=file_path) if cwd else detect_working_context(None)
        return {"repo_id": working.repo_id, "languages": list(working.languages), "file_path": file_path}

    def context(self, *, task: Optional[str], cwd: Optional[str], file_path: Optional[str] = None,
                max_tokens: int = 1500, max_chars: Optional[int] = None, fmt: str = "markdown", wrap: bool = False,
                include_profile: bool = True, record_access: bool = True) -> ContextResult:
        body = self._request("POST", "/api/v1/context", json={
            "task": task, "context": self._context_payload(cwd, file_path), "max_tokens": max_tokens,
            "max_chars": max_chars, "format": fmt, "wrap": wrap, "include_profile": include_profile,
            "record_access": record_access,
        })
        return ContextResult(text=body["text"], items=body["items"], estimated_tokens=body["estimated_tokens"],
                             repo_id=body.get("repo_id"), languages=body.get("languages", []))

    def recall(self, query: str, *, cwd: Optional[str], limit: int = 8, explain: bool = False) -> list[dict]:
        body = self._request("POST", "/api/v1/recall", json={
            "query": query, "context": self._context_payload(cwd), "limit": limit, "explain": explain})
        return body["items"]

    def ingest(self, candidates: list[CandidateMemory], *, source: str) -> ReportResult:
        if not candidates:
            return ReportResult.from_report(IngestReport())
        body = self._request("POST", "/api/v1/learn/candidates", json={
            "source": source, "candidates": [_candidate_payload(c) for c in candidates]})
        return ReportResult(body["summary"], body["counts"], body["outcomes"])

    def learn_conversation(self, transcript: Transcript, *, cwd: Optional[str], use_llm: bool = False) -> ReportResult:
        # Redact on the client: the transcript should not leave the machine
        # carrying secrets, whatever the server then does with it.
        messages = [{"role": t.role, "content": redact(t.text).text} for t in transcript.turns]
        body = self._request("POST", "/api/v1/learn/conversation", json={
            "messages": messages, "session_id": transcript.session_id,
            "context": self._context_payload(cwd or transcript.cwd), "use_llm": use_llm,
        })
        return ReportResult(body["summary"], body["counts"], body["outcomes"])

    def learn_repository(self, path: str, *, author_email: Optional[str] = None, dry_run: bool = False,
                         generalize: bool = True) -> RepositoryResult:
        from mnemosyne.learning.repo.analyzer import AnalysisOptions, RepositoryAnalyzer

        analysis = RepositoryAnalyzer(AnalysisOptions(author_email=author_email)).analyze(path)
        report = None if dry_run else self.ingest(analysis.to_candidates(), source="repository")
        general = None
        if generalize and not dry_run:
            body = self._request("POST", "/api/v1/learn/generalize")
            general = ReportResult(body["summary"], body["counts"], body["outcomes"])
        return RepositoryResult(analysis.identity.repo_id, analysis.languages, _findings(analysis),
                                analysis.duration_ms, report, general)

    def feedback(self, memory_id: str, signal: str, note: Optional[str] = None) -> dict:
        return self._request("POST", f"/api/v1/memories/{memory_id}/feedback", json={"signal": signal, "note": note})

    def forget(self, memory_id: str, *, permanent: bool = False) -> None:
        if permanent:
            self._request("DELETE", f"/api/v1/memories/{memory_id}", params={"permanent": "true"})
        else:
            self._request("POST", f"/api/v1/memories/{memory_id}/archive")

    def get(self, memory_id: str) -> dict:
        return self._request("GET", f"/api/v1/memories/{memory_id}")

    def evidence(self, memory_id: str) -> list[dict]:
        return self._request("GET", f"/api/v1/memories/{memory_id}/evidence")

    def history(self, memory_id: str) -> list[dict]:
        versions = self._request("GET", f"/api/v1/memories/{memory_id}/versions")
        return [{"version": v["version_number"], "title": v["title"], "value": v.get("value"), "status": v["status"],
                 "changed_fields": v["changed_fields"], "change_reason": v["change_reason"],
                 "recorded_at": v["recorded_at"]} for v in versions]

    def list(self, *, memory_types: Optional[list[str]] = None, scopes: Optional[list[str]] = None,
             limit: int = 50, include_archived: bool = False) -> list[dict]:
        params: list[tuple[str, str]] = [("page_size", str(max(1, min(limit, 200)))), ("sort_by", "importance_score")]
        params += [("memory_type", t) for t in memory_types or []]
        params += [("scope", s) for s in scopes or []]
        params += [("status", "active")] + ([("status", "archived")] if include_archived else [])
        return self._request("GET", "/api/v1/memories", params=params)["items"]

    def export(self) -> dict:
        return self._request("GET", "/api/v1/me/export")

    def import_bundle(self, bundle: dict) -> ReportResult:
        body = self._request("POST", "/api/v1/me/import", json=bundle)
        return ReportResult(body["summary"], body["counts"], body["outcomes"])

    def rules(self, cwd: str, *, audience: str = "repo") -> tuple[str, int]:
        working = detect_working_context(cwd)
        params: list[tuple[str, str]] = [("audience", audience)]
        if working.repo_id:
            params.append(("repo_id", working.repo_id))
        params += [("language", lang) for lang in working.languages]
        body = self._request("GET", "/api/v1/rules", params=params)
        return body["markdown"], body["items"]

    def whoami(self) -> dict:
        return {"mode": "remote", "server": self.url, **self._request("GET", "/api/v1/me")}

    def stats(self) -> dict:
        return self._request("GET", "/api/v1/me/stats")

    def close(self) -> None:
        self._client.close()


def open_backend(settings: Optional[Settings] = None) -> Backend:
    """The backend the current configuration asks for."""
    from mnemosyne.config import get_settings

    settings = settings or get_settings()
    if settings.server_url:
        if not settings.server_api_key:
            from mnemosyne.domain.exceptions import ConfigurationError

            raise ConfigurationError("MNEMOSYNE_SERVER_URL is set but MNEMOSYNE_SERVER_API_KEY is not")
        return RemoteBackend(settings.server_url, settings.server_api_key.get_secret_value())
    from mnemosyne.container import Mnemosyne

    return LocalBackend(Mnemosyne(settings))
