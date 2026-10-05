"""
FastAPI application factory.

`create_app()` builds and wires the entire application: configuration,
logging, the database engine/session factory, the domain engine, the
service layer, routers, middleware, and exception handlers. Building
everything behind a factory function (rather than a module-level
`app = FastAPI()`) is what lets tests construct an app wired to an
isolated database instead of the real one.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from mnemosyne import __version__
from mnemosyne.api.error_handlers import register_exception_handlers
from mnemosyne.api.routers.memories import router as memories_router
from mnemosyne.config import Settings, get_settings
from mnemosyne.container import Mnemosyne
from mnemosyne.logging_config import configure_logging, get_request_id, set_request_id

logger = logging.getLogger("mnemosyne.api")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a correlation id to every request and logs start/end with timing."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        set_request_id(request_id)
        start = time.perf_counter()
        logger.info("request_started", extra={"method": request.method, "path": request.url.path})
        try:
            response: Response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed_unhandled", extra={"method": request.method, "path": request.url.path}
            )
            raise
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_finished",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("mnemosyne_starting", extra={"app_env": settings.app_env})
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

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(memories_router)

    @app.get("/health", tags=["health"])
    def health_check() -> dict:
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
            "request_id": get_request_id(),
        }

    return app
