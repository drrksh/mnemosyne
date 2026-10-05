"""
Enumerated domain vocabularies.

All enums are plain string enums (rather than a native database ENUM
type) on purpose: adding a new MemoryType is a one-line code change
plus a normal data migration, instead of the `ALTER TYPE ... ADD VALUE`
dance PostgreSQL requires for native enums (and that SQLite cannot
express at all). See DECISIONS.md, "Memory type representation".
"""
from __future__ import annotations

from enum import Enum


class MemoryType(str, Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    FAILURE = "failure"
    PROCEDURE = "procedure"
    ARCHITECTURE = "architecture"
    GOAL = "goal"
    CONVERSATION_SUMMARY = "conversation_summary"
    RULE = "rule"
    OBSERVATION = "observation"
    RELATIONSHIP = "relationship"
    # Developer-memory vocabulary. These describe *how a person builds
    # software*: the conventions they follow, the patterns they reach
    # for, what they avoid, which tools they use, and what they know.
    CONVENTION = "convention"
    PATTERN = "pattern"
    ANTI_PATTERN = "anti_pattern"
    TOOLING = "tooling"
    SKILL = "skill"
    UNKNOWN = "unknown"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class MemoryOrigin(str, Enum):
    """Where a memory came from. Drives conflict resolution: a statement
    the developer made explicitly outranks something Mnemosyne inferred
    from their code, never the other way around."""

    EXPLICIT = "explicit"
    CONVERSATION = "conversation"
    IMPORTED = "imported"
    INFERRED = "inferred"
    SYSTEM = "system"


#: Higher wins when two memories claim the same slot. EXPLICIT and
#: CONVERSATION are both the developer's own words; EXPLICIT ranks higher
#: because it was a deliberate "remember this", not an extracted aside.
ORIGIN_AUTHORITY: dict[MemoryOrigin, int] = {
    MemoryOrigin.EXPLICIT: 4,
    MemoryOrigin.CONVERSATION: 3,
    MemoryOrigin.IMPORTED: 2,
    MemoryOrigin.INFERRED: 1,
    MemoryOrigin.SYSTEM: 1,
}


class EvidenceKind(str, Enum):
    EXPLICIT = "explicit"
    CONVERSATION = "conversation"
    CODE = "code"
    COMMIT = "commit"
    CONFIG = "config"
    FEEDBACK = "feedback"
    IMPORT = "import"
    GENERALIZATION = "generalization"


class FeedbackSignal(str, Enum):
    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"
    CONFIRM = "confirm"
    WRONG = "wrong"
    OUTDATED = "outdated"


class TeamRole(str, Enum):
    MEMBER = "member"
    MAINTAINER = "maintainer"


#: Legal status transitions. ACTIVE <-> ARCHIVED <-> DELETED, but nothing
#: jumps straight from a deleted memory back into ARCHIVED without first
#: passing through the normal restore-to-ACTIVE path.
ALLOWED_STATUS_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.ACTIVE: frozenset({MemoryStatus.ARCHIVED, MemoryStatus.DELETED}),
    MemoryStatus.ARCHIVED: frozenset({MemoryStatus.ACTIVE, MemoryStatus.DELETED}),
    MemoryStatus.DELETED: frozenset({MemoryStatus.ACTIVE}),
}
