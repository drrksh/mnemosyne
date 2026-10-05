"""
Data portability: export a principal's memory as a versioned JSON
bundle, and import one back.

Export is the right to data portability; it is also how a developer
moves their memory to a new machine or from a local install to a team
server. Import goes through normal ingestion, so it deduplicates against
what is already there, preserves provenance, and is idempotent: evidence
references travel with the bundle, so importing the same file twice
changes nothing.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from mnemosyne import __version__
from mnemosyne.db.unit_of_work import UnitOfWork
from mnemosyne.domain.enums import EvidenceKind, MemoryOrigin, MemoryStatus, MemoryType
from mnemosyne.domain.exceptions import ValidationError
from mnemosyne.domain.learning import CandidateMemory, EvidenceDraft, IngestReport
from mnemosyne.domain.principal import LOCAL_PRINCIPAL, Permission, Principal
from mnemosyne.services import audit
from mnemosyne.services.learning_service import LearningService
from mnemosyne.utils.time import utcnow

FORMAT = "mnemosyne-export"
FORMAT_VERSION = 1

UowFactory = Callable[[], UnitOfWork]


class PortabilityService:
    def __init__(
        self, uow_factory: UowFactory, learning: LearningService, *, default_principal: Principal = LOCAL_PRINCIPAL
    ) -> None:
        self._uow_factory = uow_factory
        self._learning = learning
        self._default_principal = default_principal

    def export(self, *, principal: Optional[Principal] = None, include_archived: bool = False) -> dict[str, Any]:
        who = principal or self._default_principal
        statuses = (MemoryStatus.ACTIVE, MemoryStatus.ARCHIVED) if include_archived else (MemoryStatus.ACTIVE,)
        with self._uow_factory() as uow:
            memories = uow.memories.list_for_index(who.visibility, statuses)
            records = []
            for memory in sorted(memories, key=lambda m: m.created_at):
                evidence = uow.evidence.list_for_memory(memory.id, limit=200)
                records.append({
                    "id": str(memory.id),
                    "title": memory.title,
                    "summary": memory.summary,
                    "content": memory.content,
                    "memory_type": memory.memory_type.value,
                    "scope": memory.scope,
                    "key": memory.key,
                    "value": memory.value,
                    "origin": memory.origin.value,
                    "confidence": memory.confidence_score,
                    "importance": memory.importance_score,
                    "tags": list(memory.tags),
                    "pinned": memory.pinned,
                    "status": memory.status.value,
                    "team_id": memory.team_id,
                    "source": memory.source,
                    "metadata": memory.metadata,
                    "reinforcement_count": memory.reinforcement_count,
                    "created_at": memory.created_at.isoformat(),
                    "updated_at": memory.updated_at.isoformat(),
                    "evidence": [
                        {"kind": e.kind.value, "source_ref": e.source_ref, "excerpt": e.excerpt, "weight": e.weight,
                         "observed_at": e.observed_at.isoformat()}
                        for e in evidence
                    ],
                })
            audit.record(uow, who, "memory.export", target_type="user", target_id=who.user_id, count=len(records))
            uow.commit()
        return {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "generator": f"mnemosyne {__version__}",
            "exported_at": utcnow().isoformat(),
            "owner": who.user_id,
            "memories": records,
        }

    def import_bundle(
        self, bundle: dict[str, Any], *, principal: Optional[Principal] = None, into_team: Optional[str] = None
    ) -> IngestReport:
        who = principal or self._default_principal
        if not who.has(Permission.WRITE):
            from mnemosyne.domain.exceptions import PermissionDeniedError

            raise PermissionDeniedError("this credential lacks the 'memories:write' permission")
        if bundle.get("format") != FORMAT:
            raise ValidationError("not a Mnemosyne export bundle")
        if int(bundle.get("format_version", 0)) > FORMAT_VERSION:
            raise ValidationError(
                f"bundle format {bundle.get('format_version')} is newer than this version understands"
            )
        candidates = []
        for record in bundle.get("memories", []):
            if record.get("status", "active") != "active":
                continue
            try:
                origin = MemoryOrigin(record.get("origin", "imported"))
                memory_type = MemoryType(record.get("memory_type", "unknown"))
            except ValueError as exc:
                raise ValidationError(f"invalid record {record.get('id')}: {exc}") from exc
            evidence = [
                EvidenceDraft(EvidenceKind(e.get("kind", "import")), str(e["source_ref"]), str(e.get("excerpt", "")),
                              float(e.get("weight", 1.0)))
                for e in record.get("evidence", []) if e.get("source_ref")
            ] or [EvidenceDraft(EvidenceKind.IMPORT, f"import:{record.get('id')}", "imported from bundle")]
            candidates.append(CandidateMemory(
                title=str(record.get("title", "")),
                content=str(record.get("content", "")),
                summary=str(record.get("summary", "")),
                memory_type=memory_type,
                origin=origin,
                confidence=float(record.get("confidence", 0.8)),
                importance=float(record.get("importance", 0.5)),
                scope=str(record.get("scope", "global")),
                key=record.get("key"),
                value=record.get("value"),
                tags=list(record.get("tags", [])),
                pinned=bool(record.get("pinned", False)),
                team_id=into_team,
                source=record.get("source") or "import",
                metadata={**(record.get("metadata") or {}), "imported_from": record.get("id")},
                evidence=evidence,
            ))
        return self._learning.ingest(candidates, principal=who, source_label="import")
