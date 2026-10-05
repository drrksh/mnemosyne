"""
FastAPI application factory.

`create_app()` builds and wires the entire application around one
`Mnemosyne` container: routers, middleware (request correlation,
metrics, request-size limit, CORS), exception mapping, and health /
readiness / metrics endpoints. Building everything behind a factory
function (rather than a module-level `app = FastAPI()`) is what lets
tests construct an app wired to an isolated database.

Run in production with:  uvicorn --factory mnemosyne.api.app:create_app
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from mnemosyne import __version__
from mnemosyne.api.dependencies import get_principal
from mnemosyne.api.error_handlers import register_exception_handlers
from mnemosyne.api.observability import Metrics, RateLimiter
from mnemosyne.api.routers.account import admin_router
from mnemosyne.api.routers.account import router as account_router
from mnemosyne.api.routers.context import router as context_router
from mnemosyne.api.routers.learning import router as learning_router
from mnemosyne.api.routers.memories import router as memories_router
from mnemosyne.config import Settings, get_settings
from mnemosyne.container import Mnemosyne
from mnemosyne.db import migrate
from mnemosyne.domain.exceptions import PermissionDeniedError
from mnemosyne.domain.principal import Principal
from mnemosyne.logging_config import configure_logging, get_request_id, set_request_id

logger = logging.getLogger("mnemosyne.api")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Correlation id, structured start/finish logs, metrics, and a cap on
    request size."""

    def __init__(self, app, *, metrics: Metrics, max_request_bytes: int) -> None:
        super().__init__(app)
        self._metrics = metrics
        self._max_bytes = max_request_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))[:64]
        set_request_id(request_id)
        start = time.perf_counter()
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > self._max_bytes:
            return JSONResponse(
                status_code=413,
                content={"error": "payload_too_large", "detail": f"request body exceeds {self._max_bytes} bytes",
                         "request_id": request_id},
                headers={"X-Request-ID": request_id},
            )
        logger.info("request_started", extra={"method": request.method, "path": request.url.path})
        try:
            response: Response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed_unhandled", extra={"method": request.method, "path": request.url.path}
            )
            raise
        elapsed = time.perf_counter() - start
        route = request.scope.get("route")
        self._metrics.observe(request.method, getattr(route, "path", "unmatched"), response.status_code, elapsed)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_finished",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(elapsed * 1000, 2),
            },
        )
        return response


def create_app(settings: Optional[Settings] = None, *, container: Optional[Mnemosyne] = None) -> FastAPI:
    if container is None:
        settings = settings or get_settings()
        configure_logging(settings)
        container = Mnemosyne(settings)
    else:
        settings = container.settings
        configure_logging(settings)
    engine = container.db_engine
    session_factory = container.session_factory
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("mnemosyne_starting", extra={"app_env": settings.app_env, "auth_mode": settings.auth_mode})
        yield
        engine.dispose()
        logger.info("mnemosyne_stopped")

    app = FastAPI(
        title="Mnemosyne",
        description=(
            "A persistent, adaptive engineering memory for AI coding assistants: it learns how a "
            "developer works and hands the right context to any model, so nobody has to re-explain "
            "themselves."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.container = container
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.memory_service = container.memories
    app.state.metrics = metrics
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute if settings.auth_mode == "api_key" else 0)

    app.add_middleware(RequestContextMiddleware, metrics=metrics, max_request_bytes=settings.max_request_bytes)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
        )
    register_exception_handlers(app)
    for router in (memories_router, context_router, learning_router, account_router, admin_router):
        app.include_router(router)

    @app.get("/health", tags=["health"])
    def health_check() -> dict:
        """Liveness: the process is up and the database answers."""
        try:
            with session_factory() as session:
                session.execute(text("SELECT 1"))
            db_ok = True
        except Exception:  # pragma: no cover - defensive, exercised via DB-down scenarios in prod
            logger.exception("health_check_db_failed")
            db_ok = False
        return {
            "status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "unavailable",
            "version": __version__,
            "request_id": get_request_id(),
        }

    @app.get("/ready", tags=["health"])
    def readiness() -> JSONResponse:
        """Readiness: the schema is at the expected migration revision."""
        try:
            current, head = migrate.current_revision(engine), migrate.HEAD_REVISION
        except Exception:  # pragma: no cover - defensive
            logger.exception("readiness_check_failed")
            return JSONResponse(status_code=503, content={"ready": False, "reason": "database unavailable"})
        ready = current == head
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "schema_revision": current, "expected_revision": head},
        )

    def metrics_access(request: Request) -> None:
        if settings.auth_mode == "api_key" and not settings.metrics_public:
            principal: Principal = get_principal(request, container)
            if not principal.is_admin:
                raise PermissionDeniedError("metrics require an administrator key (or MNEMOSYNE_METRICS_PUBLIC=true)")

    @app.get("/metrics", tags=["health"], response_class=PlainTextResponse, dependencies=[Depends(metrics_access)])
    def prometheus_metrics() -> str:
        return metrics.render()

    return app
