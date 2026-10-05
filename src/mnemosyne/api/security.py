"""Deployment safety checks for the HTTP server."""
from __future__ import annotations

import ipaddress

from mnemosyne.config import Settings
from mnemosyne.domain.exceptions import ConfigurationError

_LOOPBACK_NAMES = {"localhost", "ip6-localhost", "ip6-loopback"}


def is_loopback(host: str) -> bool:
    host = host.strip().strip("[]")
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind_safety(settings: Settings, host: str) -> None:
    """Refuse to expose an unauthenticated (local-mode) server to the
    network. Local mode means "whoever can reach this port *is* the
    owner of this memory" — only true on loopback."""
    if settings.auth_mode == "local" and not is_loopback(host) and not settings.allow_insecure_remote:
        raise ConfigurationError(
            f"refusing to bind {host!r} with auth_mode=local (no authentication). "
            "Use MNEMOSYNE_AUTH_MODE=api_key for network deployments, or bind 127.0.0.1."
        )
