"""Unit tests for the domain exception hierarchy."""
from __future__ import annotations

from mnemosyne.domain.exceptions import (
    ConflictError,
    ConfigurationError,
    MnemosyneError,
    NotFoundError,
    RepositoryError,
    ValidationError,
)


def test_all_domain_exceptions_derive_from_base():
    for exc_type in (ValidationError, NotFoundError, ConflictError, RepositoryError, ConfigurationError):
        assert issubclass(exc_type, MnemosyneError)


def test_exception_carries_message_and_details():
    exc = ValidationError("bad input", details={"field": "title"})
    assert exc.message == "bad input"
    assert exc.details == {"field": "title"}
    assert str(exc) == "bad input"


def test_exception_details_default_to_empty_dict():
    exc = NotFoundError("missing")
    assert exc.details == {}
