"""
FastAPI dependency-injection wiring.

Expensive, request-independent objects (DB engine, session factory,
MemoryEngine, MemoryService) are built once during application startup
(see `api.app.create_app`) and stored on `app.state`. These dependency
functions simply hand them to route handlers — this is the only place
route handlers should reach for application-level state.
"""
from __future__ import annotations

from fastapi import Request

from mnemosyne.services.memory_service import MemoryService


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.memory_service
