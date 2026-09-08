"""
Single source of truth for "current time" across the codebase.

Centralizing this makes it trivial to control time in tests and keeps
every timestamp in the system timezone-aware UTC — never naive
datetimes, which are a classic source of subtle bugs when one database
silently accepts a naive datetime and another does not.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
