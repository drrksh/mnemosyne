"""
Maps domain exceptions to HTTP responses.

Internal details (stack traces, SQL errors, file paths) are never sent
to the client — they are logged server-side with the request's
correlation id, and the client gets a small, stable JSON error shape:
`{"error": ..., "detail": ..., "request_id": ...}`.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from mnemosyne.domain.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    MnemosyneError,
    NotFoundError,
    PermissionDeniedError,
    RepositoryError,
    ValidationError,
)
from mnemosyne.api.observability import RateLimitedError
from mnemosyne.logging_config import get_request_id

logger = logging.getLogger("mnemosyne.api.errors")

# Order matters only in that it must be consistent; lookup below uses
# isinstance, not exact-type matching, so subclasses added later are
# handled correctly without needing a new entry here.
_STATUS_BY_EXCEPTION: list[tuple[type[MnemosyneError], int]] = [
    (ValidationError, 422),
    (AuthenticationError, 401),
    (PermissionDeniedError, 403),
    (NotFoundError, 404),
    (ConflictError, 409),
    (ConfigurationError, 500),
    (RepositoryError, 503),
]


def _status_for(exc: MnemosyneError) -> int:
    for exc_type, status_code in _STATUS_BY_EXCEPTION:
        if isinstance(exc, exc_type):
            return status_code
    return 500


def _error_response(status_code: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "detail": detail, "request_id": get_request_id()},
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_pydantic_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(422, "validation_error", str(exc.errors()))

    @app.exception_handler(RateLimitedError)
    async def handle_rate_limited(request: Request, exc: RateLimitedError) -> JSONResponse:
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is not None:
            metrics.rate_limited += 1
        response = _error_response(429, "rate_limited", exc.message)
        response.headers["Retry-After"] = str(max(1, int(exc.retry_after + 0.999)))
        return response

    @app.exception_handler(MnemosyneError)
    async def handle_domain_error(request: Request, exc: MnemosyneError) -> JSONResponse:
        status_code = _status_for(exc)
        if status_code >= 500:
            logger.exception("domain_error", extra={"error_type": type(exc).__name__})
        response = _error_response(status_code, type(exc).__name__, exc.message)
        if status_code == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception")
        return _error_response(500, "internal_error", "An unexpected error occurred.")
