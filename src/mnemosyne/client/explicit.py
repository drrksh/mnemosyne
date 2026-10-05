"""
Explicit memories: what a developer tells Mnemosyne directly, through
`mnemosyne remember` or an assistant's `remember` MCP tool.

A one-sentence statement first goes through the conversation heuristics,
so "use pnpm, not npm" becomes the slot `javascript.package_manager =
pnpm` (and later supersedes "use yarn") instead of free text. Anything
else is stored as written. Either way the memory carries EXPLICIT
authority and goes through ingestion, so remembering the same thing
again reinforces it rather than duplicating it.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from mnemosyne.core.consolidation import ALREADY_RECORDED  # noqa: F401  (re-exported for callers)
from mnemosyne.core.text import normalize_whitespace, short_hash, truncate
from mnemosyne.domain.enums import EvidenceKind, MemoryOrigin, MemoryType
from mnemosyne.domain.learning import CandidateMemory, EvidenceDraft
from mnemosyne.domain.working_context import WorkingContext
from mnemosyne.learning.conversation.heuristics import ConversationExtractor, split_sentences
from mnemosyne.utils.time import utcnow

EXPLICIT_CONFIDENCE = 0.95
EXPLICIT_IMPORTANCE = 0.7


def explicit_candidate(
    text: str,
    *,
    context: Optional[WorkingContext] = None,
    memory_type: Optional[MemoryType] = None,
    scope: Optional[str] = None,
    key: Optional[str] = None,
    value: Optional[str] = None,
    importance: Optional[float] = None,
    pinned: bool = False,
    title: Optional[str] = None,
    source: str = "cli",
) -> CandidateMemory:
    text = normalize_whitespace(text)
    if not text:
        from mnemosyne.domain.exceptions import ValidationError

        raise ValidationError("nothing to remember")
    # One observation per statement per day: repeating it tomorrow reinforces
    # the memory, while an assistant calling `remember` in a loop does not.
    day = utcnow().date().isoformat()
    evidence = [EvidenceDraft(EvidenceKind.EXPLICIT, f"{source}:remember#{short_hash(text.lower(), 16)}@{day}",
                              excerpt=truncate(text, 500))]

    inferred = None
    if memory_type is None and key is None and title is None and len(split_sentences(text)) == 1:
        found = ConversationExtractor().extract_text(text, session_id=source, context=context or WorkingContext())
        inferred = found[0] if len(found) == 1 else None

    if inferred is not None:
        candidate = replace(
            inferred,
            origin=MemoryOrigin.EXPLICIT,
            confidence=max(inferred.confidence, EXPLICIT_CONFIDENCE),
            importance=importance if importance is not None else max(inferred.importance, 0.6),
            evidence=evidence,
            tags=[t for t in inferred.tags if t != "conversation"],
            source=source,
        )
    else:
        first = split_sentences(text)[0] if split_sentences(text) else text
        candidate = CandidateMemory(
            title=truncate(title or first, 120),
            content=text,
            memory_type=memory_type or MemoryType.FACT,
            origin=MemoryOrigin.EXPLICIT,
            confidence=1.0,
            importance=importance if importance is not None else EXPLICIT_IMPORTANCE,
            key=key,
            value=value,
            evidence=evidence,
            source=source,
        )
    if scope:
        candidate = replace(candidate, scope=scope)
    if pinned:
        candidate = replace(candidate, pinned=True)
    return candidate
