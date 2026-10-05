"""
Domain exception hierarchy.

Every exception the engine, services, or repositories raise on purpose
descends from MnemosyneError, so the API layer can map specific
subclasses to specific HTTP statuses. Anything that is NOT a
MnemosyneError is, by definition, a bug or an infrastructure surprise —
the API layer treats those as opaque 500s and logs them loudly rather
than trying to interpret them.
"""
from __future__ import annotations

from typing import Any, Optional


class MnemosyneError(Exception):
    """Base class for all deliberately-raised Mnemosyne errors."""

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(MnemosyneError):
    """Input failed a domain validation rule."""


class NotFoundError(MnemosyneError):
    """A requested entity does not exist (or is not visible in this context)."""


class ConflictError(MnemosyneError):
    """A write conflicted with existing state (duplicate id, stale version)."""


class RepositoryError(MnemosyneError):
    """The storage layer failed for reasons outside the caller's control."""


class ConfigurationError(MnemosyneError):
    """The application is misconfigured."""


class AuthenticationError(MnemosyneError):
    """No valid credential was presented."""


class PermissionDeniedError(MnemosyneError):
    """The caller is authenticated but not allowed to perform this operation."""
