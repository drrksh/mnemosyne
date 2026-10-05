"""
FastAPI dependency-injection wiring.

Expensive, request-independent objects live on the `Mnemosyne`
container built once in `api.app.create_app` and stored on `app.state`.
These dependency functions hand them to route handlers, and resolve the
calling `Principal` — the only place authentication happens.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Request

from mnemosyne.container import Mnemosyne
from mnemosyne.domain.exceptions import AuthenticationError, PermissionDeniedError
from mnemosyne.domain.principal import Permission, Principal
from mnemosyne.services.memory_service import MemoryService
from mnemosyne.services.retrieval_service import RetrievalService


def get_container(request: Request) -> Mnemosyne:
    return request.app.state.container


def get_memory_service(request: Request) -> MemoryService:
    return request.app.state.container.memories


def get_retrieval_service(request: Request) -> RetrievalService:
    return request.app.state.container.retrieval


def _bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    api_key = request.headers.get("X-API-Key", "").strip()
    return api_key or None


def get_principal(request: Request, container: Mnemosyne = Depends(get_container)) -> Principal:
    """Resolve who is calling.

    * `auth_mode=local`: the single local user (the server is bound to
      loopback; see `api.security.check_bind_safety`).
    * `auth_mode=api_key`: a valid `Authorization: Bearer mn_...` (or
      `X-API-Key`) header is required on every request.
    """
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    if container.settings.auth_mode == "local":
        principal = container.local_principal()
    else:
        token = _bearer_token(request)
        if not token:
            raise AuthenticationError("missing API key: send 'Authorization: Bearer <key>'")
        principal = container.identity.authenticate(token)
    request.state.principal = principal
    return principal


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.has(Permission.ADMIN):
        raise PermissionDeniedError("administrator permission required")
    return principal
