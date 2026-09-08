"""
Enumerated domain vocabularies.

Both enums are plain string enums (rather than a native database ENUM
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
    UNKNOWN = "unknown"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


#: Legal status transitions. ACTIVE <-> ARCHIVED <-> DELETED, but nothing
#: jumps straight from a deleted memory back into ARCHIVED without first
#: passing through the normal restore-to-ACTIVE path.
ALLOWED_STATUS_TRANSITIONS: dict[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.ACTIVE: frozenset({MemoryStatus.ARCHIVED, MemoryStatus.DELETED}),
    MemoryStatus.ARCHIVED: frozenset({MemoryStatus.ACTIVE, MemoryStatus.DELETED}),
    MemoryStatus.DELETED: frozenset({MemoryStatus.ACTIVE}),
}
