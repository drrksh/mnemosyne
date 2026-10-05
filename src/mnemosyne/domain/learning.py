"""
Learning DTOs: what extractors produce and what ingestion reports.

Every extractor — repository analysis, conversation heuristics, an
optional LLM, an IDE plugin calling the API — speaks the same language:
a `CandidateMemory` carrying its own evidence. Ingestion decides what to
do with it (create, reinforce, supersede...) and answers with an
`IngestReport` that a human can read: "learned 4, reinforced 2, changed
your mind about python.formatter (black -> ruff-format)".
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from mnemosyne.domain.enums import EvidenceKind, MemoryOrigin, MemoryType


@dataclass(slots=True)
class EvidenceDraft:
    kind: EvidenceKind
    #: Stable identifier of the observation (`session:<id>#<hash>`,
    #: `repo:<id>@<sha>:<analyzer>`...). Re-ingesting the same reference
    #: is a no-op, which makes every extractor safe to re-run.
    source_ref: str
    excerpt: str = ""
    weight: float = 1.0


@dataclass(slots=True)
class CandidateMemory:
    title: str
    content: str
    memory_type: MemoryType
    origin: MemoryOrigin
    confidence: float
    importance: float = 0.5
    summary: str = ""
    scope: str = "global"
    key: Optional[str] = None
    value: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    evidence: list[EvidenceDraft] = field(default_factory=list)
    team_id: Optional[str] = None
    #: Which extractor produced this ("analyzer:python-style", ...).
    source: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    pinned: bool = False


class IngestAction(str, Enum):
    CREATED = "created"
    REINFORCED = "reinforced"
    REFRESHED = "refreshed"
    SUPERSEDED = "superseded"
    KEPT_EXISTING = "kept_existing"
    SKIPPED = "skipped"


@dataclass(slots=True)
class IngestOutcome:
    action: IngestAction
    title: str
    reason: str
    memory_id: Optional[UUID] = None
    key: Optional[str] = None
    previous_value: Optional[str] = None
    new_value: Optional[str] = None
    replaced_memory_id: Optional[UUID] = None


@dataclass
class IngestReport:
    outcomes: list[IngestOutcome] = field(default_factory=list)

    def add(self, outcome: IngestOutcome) -> None:
        self.outcomes.append(outcome)

    def extend(self, other: IngestReport) -> None:
        self.outcomes.extend(other.outcomes)

    @property
    def counts(self) -> dict[str, int]:
        counter = Counter(o.action.value for o in self.outcomes)
        return {action.value: counter.get(action.value, 0) for action in IngestAction}

    @property
    def changed(self) -> list[IngestOutcome]:
        return [o for o in self.outcomes if o.action == IngestAction.SUPERSEDED]

    def summary_line(self) -> str:
        c = self.counts
        parts = []
        if c["created"]:
            parts.append(f"learned {c['created']} new")
        if c["reinforced"] + c["refreshed"]:
            parts.append(f"reinforced {c['reinforced'] + c['refreshed']}")
        if c["superseded"]:
            changes = ", ".join(
                f"{o.key or o.title}: {o.previous_value} -> {o.new_value}" for o in self.changed[:3]
            )
            parts.append(f"updated {c['superseded']} ({changes})")
        if c["kept_existing"]:
            parts.append(f"kept {c['kept_existing']} existing over weaker evidence")
        if c["skipped"]:
            parts.append(f"skipped {c['skipped']}")
        return "; ".join(parts) if parts else "nothing new"
