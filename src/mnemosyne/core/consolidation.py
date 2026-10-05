"""
Consolidation: deciding what a new observation *means* for memory.

Given a candidate and the existing memories it could relate to, decide:

* **CREATE** — nothing similar is known.
* **SKIP** — this exact observation (same evidence reference) was
  already counted. Makes every extractor safe to re-run.
* **REINFORCE** — the same belief, observed again independently. Raises
  confidence with diminishing returns, upgrades provenance if the new
  source is more authoritative (an inferred convention the developer
  then states explicitly becomes explicit).
* **REFRESH** — an *inferred* belief re-measured (a repository analysed
  again): the fresh measurement replaces the old one rather than
  compounding, so analysing the same repo ten times does not make
  Mnemosyne ten times as sure.
* **SUPERSEDE** — a different value for the same slot from a source at
  least as authoritative: the developer changed their mind, or the
  codebase changed. The old memory is archived (never deleted) and
  linked to its replacement.
* **KEEP_EXISTING** — a different value from a *less* authoritative
  source: something inferred from code does not overrule what the
  developer said. The contradiction is recorded as evidence.

The policy is pure (no I/O) so every rule is unit-tested directly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

from mnemosyne.core.text import fingerprint, normalize_value
from mnemosyne.domain.entities import Memory
from mnemosyne.domain.enums import ORIGIN_AUTHORITY, MemoryOrigin, MemoryType
from mnemosyne.domain.learning import CandidateMemory, IngestAction

_NEGATION = re.compile(
    r"\b(?:never|don'?t|do not|does not|doesn'?t|avoid|avoids|stop|no longer|not use|shouldn'?t|must not|"
    r"mustn'?t|forbid|forbidden|disallow|without)\b",
    re.IGNORECASE,
)

#: Reinforcement learning rate by source: an explicit restatement moves
#: confidence more than one more inference from code.
DEFAULT_REINFORCEMENT_RATES: dict[MemoryOrigin, float] = {
    MemoryOrigin.EXPLICIT: 0.35,
    MemoryOrigin.CONVERSATION: 0.25,
    MemoryOrigin.IMPORTED: 0.15,
    MemoryOrigin.INFERRED: 0.10,
    MemoryOrigin.SYSTEM: 0.10,
}


@dataclass(frozen=True)
class ConsolidationPolicy:
    near_duplicate_threshold: float = 0.92
    max_confidence: float = 0.99
    #: Confidence lost by a non-explicit memory each time weaker evidence
    #: contradicts it.
    contradiction_penalty: float = 0.03
    reinforcement_rates: dict[MemoryOrigin, float] = field(default_factory=lambda: dict(DEFAULT_REINFORCEMENT_RATES))


@dataclass(frozen=True)
class NearDuplicate:
    memory: Memory
    similarity: float


@dataclass
class Decision:
    action: IngestAction
    reason: str
    target: Optional[Memory] = None
    #: Field changes for the target (REINFORCE / REFRESH / KEEP_EXISTING).
    changes: dict[str, Any] = field(default_factory=dict)
    #: Evidence references on the candidate that are new for the target.
    new_refs: list[str] = field(default_factory=list)


def polarity(text: str) -> int:
    """-1 for prohibitions ("never use tabs"), +1 otherwise."""
    return -1 if _NEGATION.search(text or "") else 1


def candidate_fingerprint(candidate: CandidateMemory) -> str:
    return fingerprint(candidate.title.strip(), candidate.content or "")


def reinforce_confidence(current: float, rate: float, ceiling: float) -> float:
    return round(min(ceiling, current + (1.0 - current) * rate), 4)


class Consolidator:
    def __init__(self, policy: Optional[ConsolidationPolicy] = None) -> None:
        self.policy = policy or ConsolidationPolicy()

    # ------------------------------------------------------------------
    def decide(
        self,
        candidate: CandidateMemory,
        *,
        slot_matches: Sequence[Memory] = (),
        fingerprint_matches: Sequence[Memory] = (),
        near_duplicates: Sequence[NearDuplicate] = (),
        known_refs: Optional[dict[Any, set[str]]] = None,
        now: datetime,
    ) -> Decision:
        known_refs = known_refs or {}
        refs = [e.source_ref for e in candidate.evidence]

        if candidate.key:
            current = slot_matches[0] if slot_matches else None
            if current is not None:
                if normalize_value(current.value) == normalize_value(candidate.value):
                    return self._same_belief(candidate, current, refs, known_refs, now)
                return self._conflict(candidate, current, refs, known_refs)
            return Decision(IngestAction.CREATED, "new slot")

        for match in fingerprint_matches:
            return self._same_belief(candidate, match, refs, known_refs, now)

        best = max(near_duplicates, key=lambda d: d.similarity, default=None)
        if best is not None and best.similarity >= self.policy.near_duplicate_threshold:
            existing = best.memory
            same_kind = existing.memory_type == candidate.memory_type or MemoryType.UNKNOWN in (
                existing.memory_type,
                candidate.memory_type,
            )
            if same_kind:
                new_polarity = polarity(f"{candidate.title} {candidate.content}")
                if new_polarity != polarity(f"{existing.title} {existing.content}"):
                    return self._conflict(candidate, existing, refs, known_refs)
                return self._same_belief(candidate, existing, refs, known_refs, now)

        return Decision(IngestAction.CREATED, "nothing similar known")

    # ------------------------------------------------------------------
    def _same_belief(
        self,
        candidate: CandidateMemory,
        existing: Memory,
        refs: list[str],
        known_refs: dict[Any, set[str]],
        now: datetime,
    ) -> Decision:
        seen = known_refs.get(existing.id, set())
        new_refs = [r for r in refs if r not in seen]
        if refs and not new_refs:
            return Decision(IngestAction.SKIPPED, "observation already recorded", target=existing)

        independent = 1 if new_refs or not refs else 0
        changes: dict[str, Any] = {
            "last_reinforced_at": now,
            "reinforcement_count": existing.reinforcement_count + independent,
            "importance_score": max(existing.importance_score, candidate.importance),
        }
        inferred = (MemoryOrigin.INFERRED, MemoryOrigin.SYSTEM)
        if candidate.origin in inferred and existing.origin in inferred:
            # A re-measurement replaces the previous measurement.
            changes.update(
                confidence_score=round(min(self.policy.max_confidence, candidate.confidence), 4),
                title=candidate.title,
                content=candidate.content,
                summary=candidate.summary or existing.summary,
                metadata={**existing.metadata, **candidate.metadata},
            )
            return Decision(IngestAction.REFRESHED, "re-measured", target=existing, changes=changes, new_refs=new_refs)

        rate = self.policy.reinforcement_rates.get(candidate.origin, 0.1)
        ceiling = max(self.policy.max_confidence, existing.confidence_score)
        changes["confidence_score"] = max(
            reinforce_confidence(existing.confidence_score, rate, ceiling), min(candidate.confidence, ceiling)
        )
        if ORIGIN_AUTHORITY[candidate.origin] > ORIGIN_AUTHORITY[existing.origin]:
            # The developer confirmed something Mnemosyne had only inferred:
            # their wording and provenance win.
            changes.update(origin=candidate.origin, title=candidate.title, content=candidate.content)
        return Decision(IngestAction.REINFORCED, "observed again", target=existing, changes=changes, new_refs=new_refs)

    def _conflict(
        self, candidate: CandidateMemory, existing: Memory, refs: list[str], known_refs: dict[Any, set[str]]
    ) -> Decision:
        new_auth = ORIGIN_AUTHORITY[candidate.origin]
        old_auth = ORIGIN_AUTHORITY[existing.origin]
        if new_auth >= old_auth:
            why = "changed mind" if candidate.origin in (MemoryOrigin.EXPLICIT, MemoryOrigin.CONVERSATION) else (
                "newer observation"
            )
            return Decision(IngestAction.SUPERSEDED, why, target=existing)

        seen = known_refs.get(existing.id, set())
        new_refs = [r for r in refs if r not in seen]
        if refs and not new_refs:
            return Decision(IngestAction.SKIPPED, "contradiction already recorded", target=existing)
        changes: dict[str, Any] = {}
        if existing.origin != MemoryOrigin.EXPLICIT:
            changes["confidence_score"] = round(
                max(0.05, existing.confidence_score * (1.0 - self.policy.contradiction_penalty)), 4
            )
        return Decision(
            IngestAction.KEPT_EXISTING,
            f"{existing.origin.value} memory outranks {candidate.origin.value} evidence",
            target=existing,
            changes=changes,
            new_refs=new_refs,
        )
